"""
Reads `probe_scan_summary.py`'s records and asks which of the three
hypotheses the drops actually fit.

    python evals/analyse_scan_summary.py results/scan-summary-probe-n20.json

Three mechanisms were on the table after the single 2026-08-17 failure, and
they want three different fixes:

  **position** -- the model summarises the first N entries and stops, so
  whatever sits at the end of the tool's output is at risk. Fixed by ordering
  or by chunking.
  **entry count** -- the summary degrades once the list is longer than some
  length, regardless of what is in it. Fixed by lowering `limit` or by
  paginating.
  **fault type** -- particular entries are dropped because of what they say,
  not where they sit. Fixed, if at all, in the projection.

They are separable in the data: position is the index within the tool result,
entry count is a per-run property, fault type is per-entry. This prints all
three marginals plus the adjacency of multi-drop runs, and takes no view
beyond the intervals.

Separate from the probe on purpose -- the probe costs model time, and the
questions worth asking of its output changed the first time it was read.
"""

import json
import sys
from collections import Counter, defaultdict

from summarise import wilson


def rate(count, total):
    if not total:
        return "     -"
    low, high = wilson(count, total)
    return f"{count:3}/{total:<3} {count / total * 100:5.1f}% [{low:.0f}-{high:.0f}]"


def main(paths):
    records = []
    for path in paths:
        with open(path) as fh:
            records += json.load(fh)

    if not records:
        print("no records", file=sys.stderr)
        return 2

    # A run that exhausted MAX_ROUNDS wrote no summary, so every entry looks
    # dropped. Pooling it would add one phantom drop to every position and
    # every fault class at once -- flat noise across exactly the marginals
    # this is trying to read. Counted and reported, then set aside.
    gave_up = [r for r in records
               if r.get("gave_up") or r["answer"].startswith("Gave up after")]
    records = [r for r in records if r not in gave_up]

    runs = len(records)
    complete = sum(r["complete"] for r in records)
    low, high = wilson(complete, runs)
    print(f"runs: {runs}   complete summaries: {complete}/{runs} "
          f"({complete / runs * 100:.0f}% [{low:.0f}-{high:.0f}])")
    if gave_up:
        print(f"excluded: {len(gave_up)} run(s) that gave up after MAX_ROUNDS "
              "and wrote no summary")

    entries = sum(r["returned_count"] for r in records)
    dropped = sum(r["dropped_count"] for r in records)
    print(f"entries presented: {entries}   dropped: {dropped} "
          f"({dropped / entries * 100:.1f}%)\n")

    # Position. Both ends are printed: "the model stops after N" and "the
    # model loses the tail" are the same claim, but "the model loses the
    # first entry" is a different one and would be invisible from one end.
    by_position = defaultdict(lambda: [0, 0])
    by_from_end = defaultdict(lambda: [0, 0])
    by_fault = defaultdict(lambda: [0, 0])
    by_status = defaultdict(lambda: [0, 0])
    by_count = defaultdict(lambda: [0, 0])

    for record in records:
        total = record["returned_count"]
        dropped_keys = {e["key"] for e in record["dropped"]}
        for entry in record["returned"]:
            hit = entry["key"] in dropped_keys
            for bucket, key in (
                (by_position, entry["position"]),
                (by_from_end, total - 1 - entry["position"]),
                (by_fault, entry["fault"]),
                (by_status, entry["status"]),
            ):
                bucket[key][1] += 1
                bucket[key][0] += hit
        by_count[total][1] += 1
        by_count[total][0] += not record["complete"]

    print("dropped by position in the tool result")
    for position in sorted(by_position):
        hits, total = by_position[position]
        print(f"  {position:<3} {rate(hits, total)}")

    print("\ndropped by position from the end (0 = last entry)")
    for position in sorted(by_from_end):
        hits, total = by_from_end[position]
        print(f"  {position:<3} {rate(hits, total)}")

    print("\ndropped by fault class")
    for fault, (hits, total) in sorted(by_fault.items(),
                                       key=lambda kv: -kv[1][0] / kv[1][1]):
        print(f"  {fault:<24} {rate(hits, total)}")

    print("\ndropped by status")
    for status, (hits, total) in sorted(by_status.items(),
                                        key=lambda kv: -kv[1][0] / kv[1][1]):
        print(f"  {status:<24} {rate(hits, total)}")

    print("\nincomplete runs by how many entries the tool returned")
    for count in sorted(by_count):
        hits, total = by_count[count]
        print(f"  {count:<3} entries  {rate(hits, total)} of runs incomplete")

    # Which workloads, by name. Position, fault and status are all confounded
    # with the workload on a fixed fixture set -- never-ready is the only
    # Running entry and also the last one -- so the name is worth printing
    # rather than inferred from the marginals above.
    print("\ndropped by workload")
    presented = Counter()
    lost = Counter()
    for record in records:
        dropped_keys = {e["key"] for e in record["dropped"]}
        for entry in record["returned"]:
            presented[entry["key"]] += 1
            lost[entry["key"]] += entry["key"] in dropped_keys
    for key in sorted(presented, key=lambda k: -lost[k] / presented[k]):
        print(f"  {key:<24} {rate(lost[key], presented[key])}")

    # Adjacency, which is the reading the single 2026-08-17 failure invited:
    # its two drops sat next to each other in the tool's output. Worth
    # printing rather than eyeballing, and worth comparing against how often
    # two drops in a run would be adjacent by chance.
    multi = [r for r in records if r["dropped_count"] > 1]
    if multi:
        adjacent = 0
        for record in multi:
            positions = sorted(e["position"] for e in record["dropped"])
            if any(b - a == 1 for a, b in zip(positions, positions[1:])):
                adjacent += 1
        print(f"\nruns dropping more than one entry: {len(multi)}   "
              f"with two of the drops adjacent: {adjacent}")
    else:
        print("\nno run dropped more than one entry")

    # Everything else that could explain a bad summary rather than the
    # summary itself: a run that never got a list, one that was sent back, or
    # one that read a different tool's output.
    print("\nrun shape")
    print(f"  runs with no scan_cluster listing: "
          f"{sum(not r['returned_count'] for r in records)}")
    print(f"  runs nudged: {sum(bool(r['nudges']) for r in records)}")
    print(f"  runs calling more than scan_cluster: "
          f"{sum(len(set(r['tools'])) > 1 for r in records)}")
    slept = [r for r in records if (r.get('timing') or {}).get('slept_ms', 0) > 1000]
    print(f"  runs the host slept in: {len(slept)}")

    # Only meaningful in the shuffled arm, where the two are no longer the
    # same fact. Printed always, so a reader of one output can see which arm
    # produced it.
    shuffled = sum(bool(r.get("shuffled")) for r in records)
    print(f"  runs with scan_cluster's order permuted: {shuffled}/{runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["results/scan-summary-probe-n20.json"]))
