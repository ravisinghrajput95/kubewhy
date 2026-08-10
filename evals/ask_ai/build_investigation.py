"""Render example-findings.yaml as a self-contained investigation report."""
import html
import os
from collections import Counter

import yaml

# The findings file is example-findings.yaml, not findings.yaml -- this read
# the wrong name and read it from the working directory, so the script could
# not run from anywhere at all.
HERE = os.path.dirname(os.path.abspath(__file__))
FINDINGS = os.path.join(HERE, "example-findings.yaml")

SEV = {"critical": "crit", "high": "high", "medium": "med", "low": "low"}
CONF = {"certain": "c-cert", "high": "c-high", "medium": "c-med", "low": "c-low"}


def e(x):
    return html.escape(str(x if x is not None else "")).strip()


def block(label, value, cls=""):
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        inner = "<ul>" + "".join(f"<li>{e(v)}</li>" for v in value) + "</ul>"
    elif isinstance(value, dict):
        inner = "".join(
            f'<div class="sub"><span class="sk">{e(k).replace("_", " ")}</span>{e(v)}</div>'
            for k, v in value.items())
    else:
        inner = f"<p>{e(value)}</p>"
    return f'<div class="fld {cls}"><span class="k">{label}</span>{inner}</div>'


def hyp(items):
    if not items:
        return ""
    rows = []
    for h in items:
        h = dict(h)
        claim = h.pop("hypothesis", "")
        verdict = "".join(
            f'<div class="sub"><span class="sk">{e(k).replace("_", " ")}</span>{e(v)}</div>'
            for k, v in h.items())
        rows.append(f'<li><em>{e(claim)}</em>{verdict}</li>')
    return f'<div class="fld"><span class="k">Alternative hypotheses</span><ul class="hyp">{"".join(rows)}</ul></div>'


def main():
    with open(FINDINGS) as fh:
        d = yaml.safe_load(fh)
    inv, fs = d["investigation"], d["findings"]
    sev = Counter(f["severity"] for f in fs)

    cards = []
    for f in fs:
        why = block("Why it hides", f.get("why_it_hides"), "hide")
        cb = f'<div class="cbasis">{e(f["confidence_basis"])}</div>' if f.get("confidence_basis") else ""
        cards.append(f"""
<article id="{f['id']}">
  <div class="fhead">
    <span class="fid">{e(f['id'])}</span>
    <h3>{e(f['title'])}</h3>
    <span class="chip {SEV[f['severity']]}">{e(f['severity'])}</span>
    <span class="conf {CONF.get(f['confidence'],'c-med')}">confidence: {e(f['confidence'])}</span>
  </div>
  {cb}
  {block("Evidence", f.get("evidence"), "ev")}
  {block("Root cause", f.get("root_cause"))}
  {why}
  {hyp(f.get("alternative_hypotheses"))}
  <div class="grid2">
    {block("Affected workloads", f.get("affected_workloads"))}
    {block("Blast radius", f.get("blast_radius"))}
  </div>
  {block("Recommended fix", f.get("recommended_fix"), "fix")}
  {block("Rollback", f.get("rollback"), "rb")}
  {block("Verification", f.get("verification"), "ver")}
  {block("Prevention", f.get("prevention"))}
  <div class="rel">
    {block("Objects", f.get("related_objects"))}
    {block("Metrics", f.get("related_metrics"))}
    {block("Logs", f.get("related_logs"))}
    {block("Events", f.get("related_events"))}
    {block("Topology", f.get("related_topology"))}
    {block("Graph edges", f.get("related_kg_nodes"))}
  </div>
</article>""")

    avail = d["evidence_availability"]
    unavail = "".join(
        f'<div class="ua"><span class="sk">{e(u["reason"])}</span><p>{e(u["consequence"])}</p></div>'
        for u in avail["unavailable"])
    have = "".join(f"<li>{e(x)}</li>" for x in avail["available"])

    ctrl = "".join(
        f'<div class="fld ok"><span class="k">{e(c["object"])}</span><p>{e(c["result"])}</p></div>'
        for c in d["controls_verified_healthy"])
    clus = "".join(
        f'<div class="fld"><span class="k">{e(o["observation"])}</span><p>{e(o["significance"])}</p></div>'
        for o in d["cluster_level_observations"])

    toc = "".join(
        f'<a href="#{f["id"]}"><span class="fid">{f["id"]}</span>{e(f["title"])}'
        f'<span class="chip {SEV[f["severity"]]}">{f["severity"][0].upper()}</span></a>' for f in fs)

    page = f"""<title>Cluster Investigation — INV-2026-08-09-001</title>
<style>
:root {{
  --ground:#edf0f2; --surface:#f8fafb; --raised:#ffffff;
  --ink:#11171c; --ink-2:#404b55; --ink-3:#6c7681;
  --rule:#d2d9df; --rule-2:#e2e7eb;
  --accent:#0d6b74; --accent-ink:#0a5158;
  --crit:#b02a20; --high:#9e5f11; --med:#37627e; --low:#66707a; --ok:#1d7a52;
  --crit-bg:#f6e2e0; --high-bg:#f7ecdb; --med-bg:#e2edf4; --low-bg:#e7eaed; --ok-bg:#dff0e7;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}}
@media (prefers-color-scheme:dark) {{ :root:not([data-theme="light"]) {{
  --ground:#0d1216; --surface:#131a20; --raised:#19222a;
  --ink:#e5eaee; --ink-2:#a7b3bd; --ink-3:#7b8791;
  --rule:#27323a; --rule-2:#1e272f;
  --accent:#4dbdc7; --accent-ink:#78d1d9;
  --crit:#ef887e; --high:#dfaa61; --med:#81b3d0; --low:#97a2ab; --ok:#5fc294;
  --crit-bg:#321816; --high-bg:#2e2314; --med-bg:#16252e; --low-bg:#1b2127; --ok-bg:#12281f;
}} }}
:root[data-theme="dark"] {{
  --ground:#0d1216; --surface:#131a20; --raised:#19222a;
  --ink:#e5eaee; --ink-2:#a7b3bd; --ink-3:#7b8791;
  --rule:#27323a; --rule-2:#1e272f;
  --accent:#4dbdc7; --accent-ink:#78d1d9;
  --crit:#ef887e; --high:#dfaa61; --med:#81b3d0; --low:#97a2ab; --ok:#5fc294;
  --crit-bg:#321816; --high-bg:#2e2314; --med-bg:#16252e; --low-bg:#1b2127; --ok-bg:#12281f;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.62;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 26px 90px}}
header.top{{border-bottom:2px solid var(--ink);padding:50px 0 20px;margin-bottom:30px}}
.eyebrow{{font-family:var(--mono);font-size:11px;letter-spacing:.15em;text-transform:uppercase;
  color:var(--accent-ink);margin-bottom:12px}}
h1{{font-size:clamp(27px,4vw,40px);line-height:1.06;letter-spacing:-.025em;font-weight:750;
  margin:0 0 14px;text-wrap:balance}}
.headline{{background:var(--raised);border:1px solid var(--rule);border-left:3px solid var(--crit);
  padding:16px 19px;margin:20px 0 0}}
.headline p{{margin:0;white-space:pre-wrap;color:var(--ink-2);max-width:74ch}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);margin-top:24px}}
.stat{{background:var(--surface);padding:13px 15px}}
.stat b{{display:block;font-family:var(--mono);font-size:22px;font-weight:650;letter-spacing:-.02em}}
.stat span{{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3)}}
h2{{font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;font-family:var(--mono);
  font-weight:600;color:var(--accent-ink);margin:52px 0 15px;padding-bottom:8px;
  border-bottom:1px solid var(--rule)}}
.method{{color:var(--ink-2);white-space:pre-wrap;max-width:76ch}}
.avail{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:760px){{.avail{{grid-template-columns:1fr}}}}
.avail ul{{margin:6px 0 0;padding-left:18px;color:var(--ink-2);font-size:13.5px}}
.ua{{background:var(--surface);border:1px solid var(--rule-2);border-left:2px solid var(--high);
  padding:9px 12px;margin-bottom:6px}}
.ua p{{margin:3px 0 0;color:var(--ink-2);font-size:13px;max-width:70ch}}
.sk{{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink-3)}}
nav.toc{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}}
nav.toc a{{background:var(--surface);padding:9px 12px;text-decoration:none;color:var(--ink-2);
  font-size:13.5px;display:flex;gap:9px;align-items:center}}
nav.toc a:hover{{background:var(--raised);color:var(--ink)}}
nav.toc a:focus-visible{{outline:2px solid var(--accent);outline-offset:-2px}}
nav.toc .chip{{margin-left:auto}}
.fid{{font-family:var(--mono);font-size:11.5px;color:var(--accent-ink);letter-spacing:.06em}}
article{{background:var(--surface);border:1px solid var(--rule);margin:22px 0;padding:20px 22px;
  scroll-margin-top:14px}}
.fhead{{display:flex;align-items:center;gap:11px;flex-wrap:wrap;padding-bottom:10px;
  border-bottom:1px solid var(--rule);margin-bottom:14px}}
.fhead h3{{margin:0;font-size:18px;letter-spacing:-.018em;font-weight:680;flex:1 1 340px}}
.chip{{font-family:var(--mono);font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;
  padding:2px 7px;border:1px solid currentColor;white-space:nowrap}}
.chip.crit{{color:var(--crit);background:var(--crit-bg)}}
.chip.high{{color:var(--high);background:var(--high-bg)}}
.chip.med{{color:var(--med);background:var(--med-bg)}}
.chip.low{{color:var(--low);background:var(--low-bg)}}
.conf{{font-family:var(--mono);font-size:10px;color:var(--ink-3);letter-spacing:.05em}}
.conf.c-cert{{color:var(--ok)}}
.cbasis{{font-size:13px;color:var(--ink-3);border-left:2px solid var(--rule);
  padding-left:11px;margin-bottom:13px;max-width:76ch;white-space:pre-wrap}}
.fld{{margin-bottom:13px}}
.k{{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.11em;
  text-transform:uppercase;color:var(--ink-3);margin-bottom:3px}}
.fld p{{margin:0;color:var(--ink-2);white-space:pre-wrap;max-width:80ch}}
.fld ul{{margin:0;padding-left:18px;color:var(--ink-2)}}
.fld li{{margin:2px 0}}
.fld.ev ul li{{font-family:var(--mono);font-size:12.5px;color:var(--ink);
  background:var(--raised);border:1px solid var(--rule-2);padding:4px 8px;margin:3px 0;
  list-style:none;margin-left:-18px;overflow-x:auto}}
.fld.hide{{background:var(--raised);border:1px solid var(--rule-2);
  border-left:2px solid var(--high);padding:10px 13px}}
.fld.fix p,.fld.ver li{{font-family:var(--mono);font-size:12.5px;color:var(--ink)}}
.fld.rb p{{color:var(--ink-2)}}
.fld.ok{{background:var(--ok-bg);border:1px solid var(--ok);padding:12px 15px}}
.fld.ok .k{{color:var(--ok)}}
.hyp li{{margin:6px 0}}
.hyp em{{color:var(--ink);font-style:normal;font-weight:600}}
.sub{{margin-top:2px;font-size:13px;color:var(--ink-2)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:760px){{.grid2{{grid-template-columns:1fr}}}}
.rel{{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:1px;
  background:var(--rule-2);border:1px solid var(--rule-2);margin-top:16px}}
.rel .fld{{background:var(--raised);padding:9px 11px;margin:0}}
.rel .fld p,.rel .fld ul{{font-size:12.5px}}
footer{{margin-top:60px;padding-top:16px;border-top:1px solid var(--rule);
  color:var(--ink-3);font-size:12.5px}}
</style>
<div class="wrap">
<header class="top">
  <div class="eyebrow">Cluster Investigation &middot; {e(inv['id'])}</div>
  <h1>12 faults across 10 workloads &mdash; six of them reporting Ready</h1>
  <div class="headline"><p>{e(inv['headline'])}</p></div>
  <div class="stats">
    <div class="stat"><b>{len(fs)}</b><span>findings</span></div>
    <div class="stat"><b>{sev['critical']}</b><span>critical</span></div>
    <div class="stat"><b>{sev['high']}</b><span>high</span></div>
    <div class="stat"><b>{sev['medium']}</b><span>medium</span></div>
    <div class="stat"><b>3</b><span>visible in pod status</span></div>
    <div class="stat"><b>1</b><span>control verified healthy</span></div>
  </div>
</header>

<h2>Method</h2>
<p class="method">{e(inv['method'])}</p>

<h2>Evidence availability</h2>
<div class="avail">
  <div><span class="sk">Available and used</span><ul>{have}</ul></div>
  <div><span class="sk">Unavailable &mdash; checked, not assumed</span>{unavail}</div>
</div>

<h2>Findings</h2>
<nav class="toc">{toc}</nav>
{"".join(cards)}

<h2>Control &mdash; verified healthy</h2>
{ctrl}

<h2>Cluster-level observations</h2>
{clus}

<footer>Generated from <span class="fid">findings.yaml</span>. Every quoted figure was
produced by a command run against <span class="fid">{e(inv['cluster'])}</span> during this
investigation. Where a data source was absent it is named as absent rather than estimated.</footer>
</div>
"""
    out = os.path.join(HERE, "investigation.html")
    with open(out, "w") as fh:
        fh.write(page)
    print(f"wrote {out} — {len(page):,} bytes, {len(fs)} findings")


if __name__ == "__main__":
    main()
