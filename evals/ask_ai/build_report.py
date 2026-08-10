"""
Render the evaluation suite as a single self-contained HTML page.

Generated from the YAML rather than written by hand, so the document and the
runnable suite cannot disagree about what is in it.
"""
import html
import os
from collections import Counter

import yaml

SEV = {"critical": "crit", "high": "high", "medium": "med", "low": "low"}


def e(x):
    return html.escape(str(x if x is not None else ""))


# Alongside this file rather than in the working directory, so the documented
# `python evals/ask_ai/build_report.py` works from the repo root.
HERE = os.path.dirname(os.path.abspath(__file__))


def load(p):
    with open(os.path.join(HERE, p)) as fh:
        return yaml.safe_load(fh)


def chip(risk):
    return f'<span class="chip {SEV.get(risk, "med")}">{e(risk)}</span>'


def case_rows(cases, cat_risk):
    out = []
    for c in cases:
        risk = c.get("risk", cat_risk)
        flags = []
        if c.get("control"):
            flags.append('<span class="flag ctrl">control &mdash; refusing this is a failure</span>')
        if c.get("needs_mesh"):
            flags.append('<span class="flag mesh">needs mesh data</span>')
        if c.get("intentionally_blank"):
            flags.append('<span class="flag">blank on purpose</span>')
        extra = []
        if c.get("fixture"):
            extra.append(f'<div class="meta"><span class="k">fixture</span>{e(c["fixture"])}</div>')
        if c.get("injected"):
            extra.append(f'<div class="meta inj"><span class="k">injected</span>{e(c["injected"])}</div>')
        if c.get("notes") or c.get("note"):
            extra.append(f'<div class="meta"><span class="k">why</span>{e(c.get("notes") or c.get("note"))}</div>')
        if c.get("expected_behaviour"):
            extra.append(f'<div class="meta ovr"><span class="k">expects</span>{e(c["expected_behaviour"])}</div>')
        if c.get("failure_conditions"):
            fc = "; ".join(str(f) for f in c["failure_conditions"])
            extra.append(f'<div class="meta ovr"><span class="k">fails if</span>{e(fc)}</div>')
        if c.get("turns"):
            t = "".join(f'<li>{e(x)}</li>' for x in c["turns"])
            extra.append(f'<div class="meta"><span class="k">turns</span><ol class="turns">{t}</ol></div>')

        out.append(
            f'<tr><td class="id">{e(c["id"])}</td>'
            f'<td class="p"><div class="prompt">{e(c["prompt"]) or "&nbsp;"}</div>'
            f'{"".join(flags)}{"".join(extra)}</td>'
            f'<td class="r">{chip(risk)}</td></tr>'
        )
    return "\n".join(out)


def defaults_block(d):
    fc = "".join(f"<li>{e(x)}</li>" for x in d.get("failure_conditions", []))
    ck = " &middot; ".join(e(x) for x in d.get("checker", []))
    return f"""
<div class="defaults">
  <div class="d"><span class="k">Expected behaviour</span><p>{e(d.get('expected_behaviour'))}</p></div>
  <div class="d"><span class="k">Guardrail</span><p>{e(d.get('guardrail'))}</p></div>
  <div class="d"><span class="k">Fails if</span><ul>{fc}</ul></div>
  <div class="d"><span class="k">Pass criteria</span><p>{e(d.get('pass_criteria'))}</p></div>
  <div class="d"><span class="k">Checkers</span><p class="mono">{ck}</p></div>
  <div class="d"><span class="k">Runs per case</span><p class="mono">n = {e(d.get('n'))}</p></div>
</div>"""


def main():
    suite, red, tiers = load("suite.yaml"), load("redteam.yaml"), load("tiers.yaml")
    cats, fams = suite["categories"], red["families"]

    n_func = sum(len(c["cases"]) for c in cats)
    n_red = sum(len(f["cases"]) for f in fams)
    attacks = sum(len(f["cases"]) for f in fams if f["id"] != "RT-CTRL")
    controls = sum(1 for c in cats for x in c["cases"] if x.get("control"))
    controls += sum(len(f["cases"]) for f in fams if f["id"] == "RT-CTRL")

    by_risk = Counter()
    for c in cats:
        for x in c["cases"]:
            by_risk[x.get("risk", c["risk"])] += 1
    for f in fams:
        for x in f["cases"]:
            by_risk[x.get("risk", f["risk"])] += 1

    runs = sum(x.get("n", c["defaults"]["n"]) for c in cats for x in c["cases"])
    runs += n_red * red["meta"].get("n_per_case", 200)

    nav = "".join(
        f'<a href="#{c["id"]}"><span class="mono">{c["id"]}</span>{e(c["name"])}'
        f'<span class="ct">{len(c["cases"])}</span></a>' for c in cats)
    nav_rt = "".join(
        f'<a href="#{f["id"]}"><span class="mono">{f["id"]}</span>{e(f["name"])}'
        f'<span class="ct">{len(f["cases"])}</span></a>' for f in fams)

    sections = []
    for c in cats:
        note = ""
        if c.get("scope_note"):
            note = f'<div class="callout"><span class="k">Scope</span><p>{e(c["scope_note"])}</p></div>'
        sections.append(f"""
<section id="{c['id']}">
  <div class="sechead">
    <span class="secid mono">{e(c['id'])}</span>
    <h3>{e(c['name'])}</h3>
    {chip(c['risk'])}
    <span class="count mono">{len(c['cases'])} cases</span>
  </div>
  <p class="intent">{e(c['intent'])}</p>
  {note}
  {defaults_block(c['defaults'])}
  <div class="tw"><table><thead><tr><th>ID</th><th>Prompt &amp; overrides</th><th>Sev</th></tr></thead>
  <tbody>{case_rows(c['cases'], c['risk'])}</tbody></table></div>
</section>""")

    rt_sections = []
    for f in fams:
        note = f'<div class="callout"><span class="k">Note</span><p>{e(f["note"])}</p></div>' if f.get("note") else ""
        scoring = f'<div class="callout inv"><span class="k">Scoring</span><p>{e(f["scoring"])}</p></div>' if f.get("scoring") else ""
        fc = "; ".join(str(x) for x in f.get("failure_conditions", []))
        rt_sections.append(f"""
<section id="{f['id']}">
  <div class="sechead">
    <span class="secid mono">{e(f['id'])}</span>
    <h3>{e(f['name'])}</h3>
    {chip(f['risk'])}
    <span class="count mono">{len(f['cases'])} prompts</span>
  </div>
  {note}{scoring}
  <div class="defaults">
    <div class="d"><span class="k">Expected behaviour</span><p>{e(f.get('expected_behaviour'))}</p></div>
    <div class="d"><span class="k">Fails if</span><p>{e(fc)}</p></div>
  </div>
  <div class="tw"><table><thead><tr><th>ID</th><th>Prompt</th><th>Sev</th></tr></thead>
  <tbody>{case_rows(f['cases'], f['risk'])}</tbody></table></div>
</section>""")

    tier_rows = "".join(
        f'<tr><td class="id">{e(t["id"])}</td><td><strong>{e(t["name"])}</strong>'
        f'<div class="meta"><span class="k">trigger</span>{e(t["trigger"])}</div>'
        f'<div class="meta"><span class="k">selection</span>{e(t.get("selection", ""))}</div>'
        f'<div class="meta"><span class="k">gate</span>{e(t["gate"])}</div></td>'
        f'<td class="mono nowrap">{e(t["budget"])}<br>{e(t["cases"])} cases</td></tr>'
        for t in tiers["tiers"])

    bench = tiers["permanent_benchmarks"]
    bench_groups = "".join(
        f'<div class="bgroup"><span class="k">{e(k.replace("_", " "))}</span>'
        f'<p class="mono ids">{" ".join(e(i) for i in v)}</p></div>'
        for k, v in bench["cases"].items())

    page = f"""<title>Ask AI — Evaluation Suite</title>
<style>
:root {{
  --ground:#eceff2; --surface:#f7f9fa; --raised:#ffffff;
  --ink:#12181d; --ink-2:#3f4a54; --ink-3:#6b7681;
  --rule:#d3dae0; --rule-2:#e3e8ec;
  --accent:#0e6f78; --accent-ink:#0a545b;
  --crit:#b3271e; --high:#a2600f; --med:#37627e; --low:#66707a;
  --crit-bg:#f7e3e1; --high-bg:#f8ecdb; --med-bg:#e3edf4; --low-bg:#e8ebee;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}}
@media (prefers-color-scheme:dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#0e1317; --surface:#141b21; --raised:#1a232a;
    --ink:#e6ebef; --ink-2:#a9b5be; --ink-3:#7c8891;
    --rule:#28333b; --rule-2:#1f2830;
    --accent:#4fbfc9; --accent-ink:#7ad3db;
    --crit:#f08a80; --high:#e0ac63; --med:#82b4d1; --low:#98a3ac;
    --crit-bg:#331917; --high-bg:#2f2415; --med-bg:#17262f; --low-bg:#1c2228;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#0e1317; --surface:#141b21; --raised:#1a232a;
  --ink:#e6ebef; --ink-2:#a9b5be; --ink-3:#7c8891;
  --rule:#28333b; --rule-2:#1f2830;
  --accent:#4fbfc9; --accent-ink:#7ad3db;
  --crit:#f08a80; --high:#e0ac63; --med:#82b4d1; --low:#98a3ac;
  --crit-bg:#331917; --high-bg:#2f2415; --med-bg:#17262f; --low-bg:#1c2228;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:15px; line-height:1.6;
  -webkit-font-smoothing:antialiased;
}}
.mono {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:0 28px 96px; }}

header.top {{ border-bottom:2px solid var(--ink); margin-bottom:34px; padding:52px 0 22px; }}
.eyebrow {{ font-family:var(--mono); font-size:11px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--accent-ink); margin-bottom:14px; }}
h1 {{ font-size:clamp(30px,4.4vw,46px); line-height:1.05; letter-spacing:-.025em;
  font-weight:750; margin:0 0 12px; text-wrap:balance; }}
.sub {{ color:var(--ink-2); max-width:64ch; margin:0; font-size:16px; }}

.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(132px,1fr));
  gap:1px; background:var(--rule); border:1px solid var(--rule);
  margin:30px 0 0; }}
.stat {{ background:var(--surface); padding:14px 16px; }}
.stat b {{ display:block; font-family:var(--mono); font-size:23px; font-weight:650;
  letter-spacing:-.02em; color:var(--ink); }}
.stat span {{ font-size:10.5px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--ink-3); }}

h2 {{ font-size:13px; letter-spacing:.14em; text-transform:uppercase;
  font-family:var(--mono); font-weight:600; color:var(--accent-ink);
  margin:58px 0 16px; padding-bottom:9px; border-bottom:1px solid var(--rule); }}

.prose p {{ max-width:70ch; color:var(--ink-2); }}
.prose strong {{ color:var(--ink); }}

.panel {{ background:var(--surface); border:1px solid var(--rule); padding:20px 22px;
  margin:16px 0; }}
.panel h4 {{ margin:0 0 8px; font-size:14px; letter-spacing:-.01em; }}
.panel p {{ margin:0; color:var(--ink-2); white-space:pre-wrap; max-width:76ch; }}
.panel.warn {{ border-left:3px solid var(--high); }}
.panel.key {{ border-left:3px solid var(--accent); }}

nav.toc {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(258px,1fr));
  gap:1px; background:var(--rule); border:1px solid var(--rule); }}
nav.toc a {{ background:var(--surface); padding:9px 13px; text-decoration:none;
  color:var(--ink-2); font-size:13px; display:flex; gap:9px; align-items:baseline; }}
nav.toc a:hover {{ background:var(--raised); color:var(--ink); }}
nav.toc a:focus-visible {{ outline:2px solid var(--accent); outline-offset:-2px; }}
nav.toc .mono {{ color:var(--accent-ink); font-size:11px; }}
nav.toc .ct {{ margin-left:auto; color:var(--ink-3); font-family:var(--mono); font-size:11px; }}

section {{ margin:46px 0; scroll-margin-top:16px; }}
.sechead {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap;
  border-bottom:1px solid var(--ink); padding-bottom:9px; margin-bottom:12px; }}
.secid {{ font-size:11.5px; color:var(--accent-ink); letter-spacing:.08em; }}
.sechead h3 {{ margin:0; font-size:19px; letter-spacing:-.02em; font-weight:680; }}
.count {{ margin-left:auto; font-size:11px; color:var(--ink-3); }}
.intent {{ color:var(--ink-2); max-width:74ch; margin:0 0 14px; white-space:pre-wrap; }}

.defaults {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:1px; background:var(--rule-2); border:1px solid var(--rule-2); margin-bottom:16px; }}
.d {{ background:var(--surface); padding:11px 13px; }}
.d p, .d ul {{ margin:3px 0 0; color:var(--ink-2); font-size:13.5px; }}
.d ul {{ padding-left:16px; }}
.k {{ display:block; font-family:var(--mono); font-size:10px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--ink-3); }}

.callout {{ background:var(--raised); border:1px solid var(--rule); border-left:3px solid var(--accent);
  padding:12px 15px; margin:0 0 15px; }}
.callout p {{ margin:3px 0 0; color:var(--ink-2); white-space:pre-wrap; font-size:13.5px; max-width:76ch; }}
.callout.inv {{ border-left-color:var(--high); }}

.tw {{ overflow-x:auto; border:1px solid var(--rule); }}
table {{ border-collapse:collapse; width:100%; min-width:640px; background:var(--surface); }}
th {{ text-align:left; font-family:var(--mono); font-size:10px; letter-spacing:.11em;
  text-transform:uppercase; color:var(--ink-3); font-weight:600;
  padding:8px 13px; border-bottom:1px solid var(--rule); white-space:nowrap; }}
td {{ padding:9px 13px; border-bottom:1px solid var(--rule-2); vertical-align:top; }}
tr:last-child td {{ border-bottom:none; }}
td.id {{ font-family:var(--mono); font-size:11.5px; color:var(--accent-ink);
  white-space:nowrap; width:1%; }}
td.r {{ width:1%; }}
.prompt {{ color:var(--ink); font-size:14px; }}
.meta {{ margin-top:5px; font-size:12.5px; color:var(--ink-3); max-width:82ch; }}
.meta .k {{ display:inline-block; min-width:62px; margin-right:7px; color:var(--ink-3); }}
.meta.inj {{ color:var(--high); }}
.meta.ovr {{ color:var(--ink-2); }}
.turns {{ margin:4px 0 0; padding-left:20px; }}
.turns li {{ margin:1px 0; }}
.nowrap {{ white-space:nowrap; font-size:12px; }}

.chip {{ font-family:var(--mono); font-size:9.5px; letter-spacing:.09em; text-transform:uppercase;
  padding:2px 7px; border:1px solid currentColor; white-space:nowrap; }}
.chip.crit {{ color:var(--crit); background:var(--crit-bg); }}
.chip.high {{ color:var(--high); background:var(--high-bg); }}
.chip.med  {{ color:var(--med);  background:var(--med-bg); }}
.chip.low  {{ color:var(--low);  background:var(--low-bg); }}

.flag {{ display:inline-block; margin-top:5px; font-family:var(--mono); font-size:10px;
  letter-spacing:.06em; padding:2px 7px; background:var(--raised);
  border:1px dashed var(--rule); color:var(--ink-3); }}
.flag.ctrl {{ color:var(--accent-ink); border-color:var(--accent); }}
.flag.mesh {{ color:var(--high); border-color:var(--high); }}

.bgroup {{ padding:11px 13px; background:var(--surface); border:1px solid var(--rule-2); margin-bottom:1px; }}
.ids {{ margin:4px 0 0; font-size:12px; color:var(--accent-ink); line-height:1.9; word-spacing:.35em; }}
footer {{ margin-top:70px; padding-top:18px; border-top:1px solid var(--rule);
  color:var(--ink-3); font-size:12.5px; }}
</style>

<div class="wrap">
<header class="top">
  <div class="eyebrow">Evaluation Design &middot; v1.0</div>
  <h1>Ask AI Panel &mdash; Evaluation Suite</h1>
  <p class="sub">A production-grade evaluation suite for an operational assistant over live
  Kubernetes clusters. Not a chatbot benchmark: the failures that matter here are a fabricated
  root cause acted on at 3am, a secret echoed into a chat log, and a destructive command run
  because someone claimed to be an admin.</p>
  <div class="stats">
    <div class="stat"><b>{n_func + n_red}</b><span>total prompts</span></div>
    <div class="stat"><b>{len(cats)}</b><span>categories</span></div>
    <div class="stat"><b>{attacks}</b><span>red team attacks</span></div>
    <div class="stat"><b>{controls}</b><span>over-refusal controls</span></div>
    <div class="stat"><b>{by_risk['critical']}</b><span>critical severity</span></div>
    <div class="stat"><b>{runs:,}</b><span>invocations / full run</span></div>
  </div>
</header>

<div class="prose">
<h2>Three decisions that shape everything</h2>

<div class="panel key">
<h4>1 &nbsp;Safety numbers are meaningless without their control</h4>
<p>{e(suite['over_refusal_policy'])}</p>
</div>

<div class="panel warn">
<h4>2 &nbsp;Zero failures is not zero risk</h4>
<p>{e(suite['statistical_power'])}</p>
</div>

<div class="panel warn">
<h4>3 &nbsp;A judge model is not a measurement</h4>
<p>{e(suite['scoring']['judge_warning'])}</p>
</div>

<h2>Severity model</h2>
<div class="tw"><table><thead><tr><th>Level</th><th>Meaning</th><th>Release gate</th></tr></thead><tbody>
{"".join(f'<tr><td>{chip(k)}</td><td>{e(v["meaning"])}</td><td class="mono nowrap">{e(v["release_gate"])}</td></tr>' for k, v in suite["severity_model"].items())}
</tbody></table></div>

<h2>Checkers</h2>
<div class="defaults">
{"".join(f'<div class="d"><span class="k">{e(k)}</span><p>{e(v)}</p></div>' for k, v in suite["scoring"]["checkers"].items())}
</div>

<h2>Regression tiers</h2>
<p>A full run is {runs:,} invocations, roughly 5.5 hours at 20-way parallelism. Affordable per
release, absurd per commit &mdash; so the suite is tiered by <strong>which failures a given change
is actually capable of introducing</strong>, not by which tests feel important.</p>
<div class="tw"><table><thead><tr><th>Tier</th><th>Scope</th><th>Budget</th></tr></thead>
<tbody>{tier_rows}</tbody></table></div>

<h2>Permanent benchmarks</h2>
<div class="panel key"><h4>Freeze policy</h4><p>{e(bench['policy'])}</p></div>
<p>Thirty frozen cases, chosen because they must never regress, have unambiguous pass criteria,
and discriminate &mdash; early runs produced both passes and failures.</p>
{bench_groups}

<h2>Reporting rules</h2>
<div class="panel key"><h4>Always report in pairs</h4><p>{e(tiers['reporting']['required_pairs'])}</p></div>
<div class="panel warn"><h4>State the limits alongside the results</h4><p>{e(tiers['reporting']['known_limits'])}</p></div>

<h2>Contents</h2>
<nav class="toc">{nav}{nav_rt}</nav>

<h2>Functional categories</h2>
</div>
{"".join(sections)}

<div class="prose">
<h2>Red team suite</h2>
<p>{e(red['meta']['note'])}</p>
<div class="panel warn"><h4>Judge on outcome, not tone</h4><p>{e(red['meta']['scoring_note'])}</p></div>
</div>
{"".join(rt_sections)}

<div class="prose">
<h2>Applying this to kubewhy today</h2>
<div class="panel"><p>{e(suite['kubewhy_mapping'])}</p></div>
</div>

<footer>Generated from <span class="mono">suite.yaml</span>, <span class="mono">redteam.yaml</span>
and <span class="mono">tiers.yaml</span>. Counts on this page are computed from those files, not
transcribed &mdash; the document cannot drift from the runnable suite.</footer>
</div>
"""
    out = os.path.join(HERE, "report.html")
    with open(out, "w") as fh:
        fh.write(page)
    print(f"wrote {out} — {len(page):,} bytes, {n_func + n_red} prompts")


if __name__ == "__main__":
    main()
