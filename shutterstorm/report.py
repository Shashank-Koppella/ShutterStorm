"""report.md + a single self-contained report.html (no external assets).

Everything is rendered from ``findings.json`` so ``python -m shutterstorm report --run ID``
can regenerate the report later without touching the sandbox.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .probes import JOURNEYS

MANUAL_FINDINGS = [
    {"id": "M-01", "fault": "ml_pause (docker pause immich_machine_learning)",
     "observation": "Smart search spinner hangs indefinitely with no error. Timeline works. After unpause the stuck "
                    "request ends in an error; new searches work instantly."},
    {"id": "M-02", "fault": "ml_stop (docker stop immich_machine_learning)",
     "observation": "Immediate toast 'Failed to search smart (Immich Server Error)' plus a misleading 'No results, try "
                    "a synonym' page. Timeline works. Recovers instantly after start."},
    {"id": "M-03", "fault": "redis_pause (docker pause immich_redis)",
     "observation": "Web upload reaches 100% but the dialog stays at 'Processed 0/1, Uploaded 0' with no error. After "
                    "refresh the asset exists with an 'Error loading image' placeholder (no thumbnail); the original "
                    "opens. After unpause the thumbnail is generated immediately. No data loss."},
]
PATTERN = ("Pattern: a hung dependency is handled much worse than a down one - no timeouts are surfaced to users.")
CELL_LABEL = {"ok": "ok", "slow": "slow", "fast-fail": "fast-fail", "hung": "HUNG", "n/a": "-"}


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def badge(source: str | None) -> str:
    source = source or "fallback"
    return f'<span class="badge src-{_e(source)}" title="where this text came from">AI: {_e(source)}</span>'


def render_reports(run_dir: Path) -> tuple[Path, Path]:
    data = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    md_path, html_path = run_dir / "report.md", run_dir / "report.html"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    html_path.write_text(render_html(data), encoding="utf-8")
    return md_path, html_path


# ====================================================================== markdown
def render_markdown(data: dict) -> str:
    rep = data.get("report") or {}
    exps = {e["label"]: e for e in data["experiments"]}
    out = []
    if data.get("simulated"):
        out.append("> **SIMULATED RUN** - fake Docker + fake Immich. These are not real findings.\n")
    out.append(f"# ShutterStorm findings report - run `{data['run_id']}`\n")
    out.append(f"Status: **{data['status']}**{' - ' + data['error'] if data.get('error') else ''}  ")
    out.append(f"Overall resilience score: **{data.get('overall_score')} / 100**  ")
    out.append(f"LLM mode: `{data.get('llm_mode')}`; sources: {data.get('llm_stats', {})}\n")
    out.append(f"## Executive summary  _(AI: {rep.get('source', 'n/a')})_\n\n{rep.get('executive_summary', '')}\n\n_{PATTERN}_\n")
    out.append("> AI-written text is checked only for numbers and fault names, not for every statement. "
               "Scores, ranking and heatmap are measured. Fixes are suggestions only (never applied).\n")

    plan = data.get("plan") or {}
    if plan.get("items"):
        out.append(f"## Experiment plan  _(by: {plan.get('source')})_\n\n{plan.get('strategy', '')}\n")
        out.append("| # | Experiment | Failure class | Priority | Why |\n|---|---|---|---|---|")
        for i in plan["items"]:
            out.append(f"| {i['rank']} | {i['fault']} | {i.get('failure_class', '')} | {i.get('priority', '-')} | "
                       f"{i['reason']} |")
        if plan.get("skipped"):
            out.append("\nNot selected: " + "; ".join(f"{x['fault']} ({x['why']})" for x in plan["skipped"]))
    out.append(f"\n**Safety thresholds** (automatic rollback): {data.get('thresholds', 'n/a')}  ")
    stopped = [e for e in data["experiments"] if e.get("auto_stopped")]
    out.append(f"Auto-stopped experiments: {len(stopped)} of {len(data['experiments'])}"
               + "".join(f"\n- **{e['label']}** AUTO-STOPPED after {e['auto_stopped']['after_s']}s: "
                         f"{e['auto_stopped']['reason']}" for e in stopped) + "\n")

    out.append("## Ranked findings\n")
    out.append("| # | Finding | Severity | Score | Status | Fix |\n|---|---|---|---|---|---|")
    for i, f in enumerate(rep.get("ranked_findings", []), 1):
        e = exps.get(f["id"], {})
        out.append(f"| {i} | **{f.get('title', f['id'])}**<br>{f.get('evidence', '')} | {f['severity']} "
                   f"({f.get('severity_value')}) | {e.get('scoring', {}).get('score')} | {f.get('status')} | {f.get('fix', '')} |")

    out.append("\n## Fault x journey heatmap\n")
    out.append("| Fault | " + " | ".join(JOURNEYS) + " | S_e |\n|---|" + "---|" * (len(JOURNEYS) + 1))
    for e in data["experiments"]:
        out.append(f"| {e['label']} | " + " | ".join(CELL_LABEL[e["heatmap"][j]] for j in JOURNEYS)
                   + f" | {e['scoring']['score']} |")
    out.append("\nJourneys: " + "; ".join(f"{j} = {d}" for j, d in JOURNEYS.items()))

    comp = data.get("comparison") or {}
    if comp.get("rows"):
        out.append(f"\n## Before / after (vs run `{comp['before_run']}`)\n")
        out.append(f"Overall: {comp['overall_before']} -> {comp['overall_after']}. {comp['rule']}\n")
        out.append("| Fault | Score before | Score after | Delta | Broken before | Broken after | Verdict |\n"
                   "|---|---|---|---|---|---|---|")
        for r in comp["rows"]:
            out.append(f"| {r['fault']} | {r['score_before']} | {r['score_after']} | {r['delta']:+} | "
                       f"{', '.join(r['broken_before']) or '-'} | {', '.join(r['broken_after']) or '-'} | {r['verdict']} |")
    elif comp.get("error"):
        out.append(f"\n_Comparison failed: {comp['error']}_")

    out.append("\n## Score components\n")
    out.append("| Fault | A | L | C | I | R | B | S_e | severity | recovery |\n|---|---|---|---|---|---|---|---|---|---|")
    for e in data["experiments"]:
        c = e["scoring"]["components"]
        out.append(f"| {e['label']} | " + " | ".join(str(c[k]) for k in "ALCIRB")
                   + f" | {e['scoring']['score']} | {e['scoring']['severity']} | {e['recovery_s']}s |")

    out.append("\n## Experiment details\n")
    for e in data["experiments"]:
        a = e.get("analysis", {})
        out.append(f"### {e['label']}  _(AI: {a.get('source')})_\n")
        out.append(f"- **Symptom:** {a.get('user_visible_symptom')}\n- **Summary:** {a.get('summary')}\n"
                   f"- **Root-cause hypothesis:** {a.get('root_cause_hypothesis')}\n"
                   f"- **Severity rationale:** {a.get('severity_rationale')}\n"
                   f"- **Integrity:** {json.dumps({k: v for k, v in e['integrity'].items() if k != 'details'})}\n"
                   f"- **Predicted:** {e['prediction']['predicted_impacted']} ({e['prediction']['source']}); "
                   f"**actual:** {e['actual_impacted']}"
                   + (f"\n- **Parts (combined failure):** {' + '.join(e['parts'])}" if len(e.get('parts', [])) > 1 else "")
                   + (f"\n- **Traffic:** {e['load']['sent']} requests from {e['load']['workers']} clients "
                      f"({e['load']['requests_per_s']}/s), {e['load']['errors']} errors, {e['load']['timeouts']} timeouts"
                      if e.get("load") else "")
                   + (f"\n- **AUTO-STOPPED** after {e['auto_stopped']['after_s']}s: {e['auto_stopped']['reason']}"
                      if e.get("auto_stopped") else ""))
        sug = e.get("suggestion")
        if sug:
            out.append(f"- **Suggested fix** _(source: {sug['source']}; not applied, not tested)_:")
            if sug["kind"] == "config":
                out.append(f"  - Why: {sug.get('rationale')}")
                out.append(f"  - Uses allowed settings only: {'yes' if sug['valid'] else 'no: ' + '; '.join(sug['validation_errors'])}")
                out.append("    ```\n" + "\n".join("    " + line for line in sug.get("diff", "").splitlines())
                           + "\n    ```")
            else:
                out.append(f"  - No config-level fix: {sug['recommendation']}")
            out.append(f"  - Code-level fix (template): {sug.get('template_code_fix', '')}")
        elif e.get("repair"):
            out.append(f"- _(older run: repair loop result {e['repair'].get('status')}; see findings.json)_")
        out.append("")

    sc = data.get("scorecard") or {}
    out.append(f"## Prediction scorecard\n\nprecision {sc.get('precision')}, recall {sc.get('recall')} "
               f"(tp {sc.get('tp')}, fp {sc.get('fp')}, fn {sc.get('fn')})\n")
    for m in sc.get("mismatches", []):
        out.append(f"- {m['fault']}: unexpected impact {m['unexpected_impact']}, predicted but fine {m['predicted_but_fine']}")

    out.append("\n## Recommendations\n")
    out += [f"- {r}" for r in rep.get("recommendations", [])]

    cl = data.get("cleanup") or {}
    out.append(f"\n## Cleanup verification: {'PASS' if cl.get('ok') else 'FAIL'}\n")
    out += [f"- [{'x' if c['ok'] else ' '}] {c['name']} - {c['detail']}" for c in cl.get("checks", [])]

    topo = data.get("topology") or {}
    if topo.get("mermaid"):
        out.append("\n## Dependency map\n\n```mermaid\n" + topo["mermaid"] + "\n```\n")
        out.append("Undeclared in compose: " + "; ".join(topo.get("undeclared_dependencies", [])))
    out.append("\n## Manual findings (observed by hand in the web UI)\n")
    out += [f"- **{m['id']}** {m['fault']}: {m['observation']}" for m in MANUAL_FINDINGS]
    return "\n".join(out) + "\n"


# ====================================================================== html
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--muted:#5f6368;--line:#e2e2de;--accent:#0b6e8a;
--ok:#d7f0dc;--ok-fg:#1e5b2b;--slow:#fff1c2;--slow-fg:#6b5200;--ff:#ffd9b8;--ff-fg:#7a3a00;--hung:#f6c1c1;--hung-fg:#7d1010;--na:#eee;--na-fg:#777}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141517;--card:#1d1f22;--fg:#e8e8e6;--muted:#a0a4a8;--line:#33363a;--accent:#5cc3dd;
--ok:#1f3d26;--ok-fg:#a6e3b3;--slow:#3d3412;--slow-fg:#f1d77a;--ff:#45290f;--ff-fg:#ffbf87;--hung:#4a1717;--hung-fg:#ffb0b0;--na:#2a2c2f;--na-fg:#888}}
:root[data-theme="dark"]{--bg:#141517;--card:#1d1f22;--fg:#e8e8e6;--muted:#a0a4a8;--line:#33363a;--accent:#5cc3dd;
--ok:#1f3d26;--ok-fg:#a6e3b3;--slow:#3d3412;--slow-fg:#f1d77a;--ff:#45290f;--ff-fg:#ffbf87;--hung:#4a1717;--hung-fg:#ffb0b0;--na:#2a2c2f;--na-fg:#888}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:32px 0 10px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.hero{display:flex;gap:24px;align-items:center;flex-wrap:wrap}.score{font-size:52px;font-weight:700;color:var(--accent);line-height:1}
.sim{background:#b00020;color:#fff;padding:10px 16px;font-weight:700;text-align:center}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}td.c{text-align:center;font-weight:600;white-space:nowrap}
.ok{background:var(--ok);color:var(--ok-fg)}.slow{background:var(--slow);color:var(--slow-fg)}.fast-fail{background:var(--ff);color:var(--ff-fg)}
.hung{background:var(--hung);color:var(--hung-fg)}.n\\/a{background:var(--na);color:var(--na-fg)}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:middle}
.src-live{border-color:#2e7d32;color:#2e7d32}.src-cache{border-color:#1565c0;color:#1565c0}
.sev-critical,.sev-high{color:var(--hung-fg);font-weight:700}.sev-medium{color:var(--ff-fg);font-weight:600}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;font-size:12.5px}
.pass{color:var(--ok-fg)}.fail{color:var(--hung-fg)}.pattern{border-left:4px solid var(--accent);padding-left:12px}
.stopped{display:inline-block;background:var(--hung);color:var(--hung-fg);font-weight:700;font-size:12px;padding:1px 8px;border-radius:9px}
.up{color:var(--ok-fg);font-weight:700}.down{color:var(--hung-fg);font-weight:700}
details summary{cursor:pointer;font-weight:600}
"""


def _cell(value: str) -> str:
    return f'<td class="c {_e(value)}">{_e(CELL_LABEL.get(value, value))}</td>'


def render_html(data: dict) -> str:
    rep = data.get("report") or {}
    exps = {e["label"]: e for e in data["experiments"]}
    sim = '<div class="sim">SIMULATED RUN: fake Docker + fake Immich. These are not real findings.</div>' \
        if data.get("simulated") else ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("started", time.time())))
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ShutterStorm Report</title>
<style>{CSS}</style></head><body>{sim}<main>
<h1>ShutterStorm resilience report</h1>
<div class="muted">Run {_e(data['run_id'])} &middot; {when} &middot; status <b>{_e(data['status'])}</b>
{(' &middot; ' + _e(data['error'])) if data.get('error') else ''} &middot; LLM mode {_e(data.get('llm_mode'))}
&middot; sources {_e(data.get('llm_stats', {}))}</div>
<div class="card hero"><div><div class="score">{_e(data.get('overall_score'))}</div><div class="muted">overall resilience / 100</div></div>
<div style="flex:1;min-width:260px"><b>Executive summary</b>{badge(rep.get('source'))}<p>{_e(rep.get('executive_summary'))}</p>
<p class="pattern">{_e(PATTERN)}</p><p class="muted">AI-written text (badges live/cache) is checked only for
numbers and fault names, not for every statement. Scores, ranking and heatmap are measured, not AI-generated.
Fixes are suggestions only: ShutterStorm never applies them.</p></div></div>"""]

    plan = data.get("plan") or {}
    if plan.get("items"):
        parts.append(f"<h2>Experiment plan</h2><div class='card scroll'><p><b>Chosen by:</b> {_e(plan.get('source'))}"
                     f"{badge(plan.get('source')) if plan.get('source') in ('live', 'cache', 'fallback') else ''}"
                     f"<br><span class='muted'>{_e(plan.get('strategy'))}</span></p><table><tr><th>#</th>"
                     "<th>Experiment</th><th>Failure class</th><th>Priority</th><th>Why (planner)</th></tr>"
                     + "".join(f"<tr><td>{i['rank']}</td><td><b>{_e(i['fault'])}</b></td><td>{_e(i.get('failure_class'))}</td>"
                               f"<td>{_e(i.get('priority', '-'))}</td><td>{_e(i['reason'])}</td></tr>" for i in plan["items"])
                     + "</table>")
        if plan.get("skipped"):
            parts.append("<details><summary class='muted'>Not selected</summary><ul>"
                         + "".join(f"<li>{_e(x['fault'])} (priority {_e(x.get('priority'))}): {_e(x['why'])}</li>"
                                   for x in plan["skipped"]) + "</ul></details>")
        parts.append("<p class='muted'>The planner may only choose faults from the fixed catalog; injection, "
                     "undo and safety limits are deterministic code.</p></div>")
    stopped = [e for e in data["experiments"] if e.get("auto_stopped")]
    parts.append(f"<h2>Safety controls</h2><div class='card'><p><b>Automatic rollback</b> when "
                 f"{_e(data.get('thresholds', 'n/a'))}. Plus: write-ahead journal, {_e('TTL watchdog')}, dead-man guard "
                 f"process, STOP / panic kill switches.</p><p>Auto-stopped: <b>{len(stopped)}</b> of "
                 f"{len(data['experiments'])} experiments</p>"
                 + "".join(f"<p><span class='stopped'>AUTO-STOPPED</span> <b>{_e(e['label'])}</b> after "
                           f"{_e(e['auto_stopped']['after_s'])}s: {_e(e['auto_stopped']['reason'])}</p>" for e in stopped)
                 + "</div>")

    comp = data.get("comparison") or {}
    if comp.get("rows"):
        def delta(r):
            cls = "up" if r["delta"] > 0 else ("down" if r["delta"] < 0 else "")
            return f"<td class='{cls}'>{r['delta']:+}</td>"
        parts.append(f"<h2>Before / after</h2><div class='card scroll'><p>Compared with run <b>{_e(comp['before_run'])}</b>: "
                     f"overall {_e(comp['overall_before'])} &rarr; <b>{_e(comp['overall_after'])}</b></p><table><tr>"
                     "<th>Fault</th><th>Score before</th><th>Score after</th><th>&Delta;</th><th>Heatmap before</th>"
                     "<th>Heatmap after</th><th>Recovery</th><th>Verdict</th></tr>"
                     + "".join(f"<tr><td>{_e(r['fault'])}</td><td>{r['score_before']}</td><td><b>{r['score_after']}</b></td>"
                               + delta(r)
                               + f"<td>{_e(', '.join(r['broken_before']) or 'all ok')}</td>"
                                 f"<td>{_e(', '.join(r['broken_after']) or 'all ok')}</td>"
                                 f"<td>{_e(r['recovery_before_s'])}s &rarr; {_e(r['recovery_after_s'])}s</td>"
                                 f"<td><b>{_e(r['verdict'])}</b></td></tr>" for r in comp["rows"])
                     + f"</table><p class='muted'>{_e(comp['rule'])}</p></div>")
    elif comp.get("error"):
        parts.append(f"<div class='card fail'>Comparison failed: {_e(comp['error'])}</div>")

    parts.append("<h2>Ranked findings</h2><div class='card scroll'><table><tr><th>#</th><th>Finding</th><th>Severity</th>"
                 "<th>S_e</th><th>Evidence</th><th>Fix</th><th>Status</th></tr>")
    for i, f in enumerate(rep.get("ranked_findings", []), 1):
        e = exps.get(f["id"], {})
        parts.append(f"<tr><td>{i}</td><td><b>{_e(f.get('title'))}</b></td><td class='sev-{_e(f['severity'])}'>"
                     f"{_e(f['severity'])}<br><span class='muted'>{_e(f.get('severity_value'))}</span></td>"
                     f"<td>{_e(e.get('scoring', {}).get('score'))}</td><td>{_e(f.get('evidence'))}</td>"
                     f"<td>{_e(f.get('fix'))}</td><td>{_e(f.get('status'))}</td></tr>")
    parts.append(f"</table><div class='muted' style='margin-top:6px'>Ranking and severity come from the scoring "
                 f"formula; text {badge(rep.get('source'))}</div></div>")

    parts.append("<h2>Fault &times; journey heatmap</h2><div class='card scroll'><table><tr><th>Fault</th>"
                 + "".join(f"<th title='{_e(d)}'>{j}</th>" for j, d in JOURNEYS.items())
                 + "<th>S_e</th><th>Recovery</th></tr>")
    for e in data["experiments"]:
        tag = " <span class='stopped'>AUTO-STOPPED</span>" if e.get("auto_stopped") else ""
        parts.append(f"<tr><td>{_e(e['label'])}{tag}</td>" + "".join(_cell(e["heatmap"][j]) for j in JOURNEYS)
                     + f"<td>{_e(e['scoring']['score'])}</td><td>{_e(e['recovery_s'])}s</td></tr>")
    parts.append("</table><div class='muted'>" + " &middot; ".join(f"{j} {_e(d)}" for j, d in JOURNEYS.items())
                 + "<br>ok / slow (&gt;2&times; baseline p95) / fast-fail (clear error) / HUNG (no answer within the probe timeout)</div></div>")

    parts.append("<h2>Score components</h2><div class='card scroll'><table><tr><th>Fault</th><th>A avail</th><th>L latency</th>"
                 "<th>C contain</th><th>I integrity</th><th>R recovery</th><th>B bounded</th><th>S_e</th><th>crit &times; likelihood</th>"
                 "<th>severity</th></tr>")
    for e in data["experiments"]:
        s = e["scoring"]
        parts.append(f"<tr><td>{_e(e['label'])}</td>" + "".join(f"<td>{s['components'][k]}</td>" for k in "ALCIRB")
                     + f"<td><b>{s['score']}</b></td><td>{s['criticality']} &times; {s['likelihood']}</td>"
                       f"<td class='sev-{s['severity_label']}'>{s['severity']} ({s['severity_label']})</td></tr>")
    parts.append("</table><div class='muted'>S_e = 100 &times; (0.30A + 0.15L + 0.20C + 0.15I + 0.10R + 0.10B); "
                 "severity = (100 &minus; S_e)/100 &times; criticality &times; likelihood</div></div>")

    parts.append("<h2>Experiments</h2>")
    for e in data["experiments"]:
        a = e.get("analysis", {})
        integ = {k: v for k, v in e["integrity"].items() if k != "details"}
        parts.append(f"<div class='card'><details><summary>{_e(e['label'])} &middot; score {e['scoring']['score']} "
                     f"&middot; {_e(a.get('user_visible_symptom', ''))[:120]}</summary>"
                     f"<p><b>Analysis</b>{badge(a.get('source'))}<br>{_e(a.get('summary'))}</p>"
                     f"<p><b>Root-cause hypothesis:</b> {_e(a.get('root_cause_hypothesis'))}</p>"
                     f"<p><b>Severity rationale:</b> {_e(a.get('severity_rationale'))}</p>"
                     f"<p><b>Prediction</b>{badge(e['prediction'].get('source'))}: {_e(e['prediction']['predicted_impacted'])} "
                     f"&rarr; actual {_e(e['actual_impacted'])}<br><span class='muted'>{_e(e['prediction'].get('reasoning'))}</span></p>"
                     f"<p><b>Integrity:</b> {_e(integ)}</p>")
        if len(e.get("parts", [])) > 1:
            parts.append(f"<p><b>Combined failure:</b> {_e(' + '.join(e['parts']))} applied together</p>")
        if e.get("load"):
            ld = e["load"]
            parts.append(f"<p><b>Traffic spike:</b> {ld['sent']} requests from {ld['workers']} concurrent clients "
                         f"({ld['requests_per_s']}/s); {ld['errors']} errors, {ld['timeouts']} timeouts</p>")
        if e.get("auto_stopped"):
            parts.append(f"<p><span class='stopped'>AUTO-STOPPED</span> after {_e(e['auto_stopped']['after_s'])}s "
                         f"(round {_e(e['auto_stopped']['round'])}): {_e(e['auto_stopped']['reason'])}</p>")
        sug = e.get("suggestion")
        if sug:
            parts.append(f"<p><b>Suggested fix</b>{badge(sug['source'])} <span class='muted'>(not applied, not tested)</span></p>")
            if sug["kind"] == "config":
                ok = "yes" if sug["valid"] else "no: " + "; ".join(sug["validation_errors"])
                parts.append(f"<p>{_e(sug.get('rationale'))}<br><span class='muted'>Uses allowed settings only: {_e(ok)}</span></p>"
                             f"<pre>{_e(sug.get('diff'))}</pre>")
                if sug.get("manual_steps"):
                    parts.append("<details><summary class='muted'>How a human could apply it</summary><ol>"
                                 + "".join(f"<li><pre>{_e(st)}</pre></li>" for st in sug["manual_steps"]) + "</ol></details>")
            else:
                parts.append(f"<p>No config-level fix: {_e(sug['recommendation'])}</p>")
            parts.append(f"<p class='muted'>Code-level fix (template): {_e(sug.get('template_code_fix'))}</p>")
        elif e.get("repair"):
            parts.append(f"<p class='muted'>Older run: repair loop result {_e(e['repair'].get('status'))} (see findings.json).</p>")
        logs = e.get("log_tails", {})
        for name, lines in logs.items():
            parts.append(f"<details><summary class='muted'>log tail: {_e(name)}</summary><pre>{_e(chr(10).join(lines[-40:]))}</pre></details>")
        parts.append("</details></div>")

    sc = data.get("scorecard") or {}
    parts.append(f"<h2>Prediction scorecard</h2><div class='card'>Precision <b>{_e(sc.get('precision'))}</b>, recall "
                 f"<b>{_e(sc.get('recall'))}</b> (tp {_e(sc.get('tp'))}, fp {_e(sc.get('fp'))}, fn {_e(sc.get('fn'))}). "
                 "Mismatches are findings: the system behaved differently from what the dependency map implies.<ul>"
                 + "".join(f"<li>{_e(m['fault'])}: unexpected impact {_e(m['unexpected_impact'])}, predicted but fine "
                           f"{_e(m['predicted_but_fine'])}</li>" for m in sc.get("mismatches", [])) + "</ul></div>")

    parts.append(f"<h2>Recommendations</h2><div class='card'>{badge(rep.get('source'))}<ul>"
                 + "".join(f"<li>{_e(r)}</li>" for r in rep.get("recommendations", [])) + "</ul></div>")

    cl = data.get("cleanup") or {}
    parts.append(f"<h2>Cleanup verification: <span class='{'pass' if cl.get('ok') else 'fail'}'>"
                 f"{'PASS' if cl.get('ok') else 'FAIL'}</span></h2><div class='card scroll'><table>"
                 + "".join(f"<tr><td class='{'pass' if c['ok'] else 'fail'}'>{'&#10003;' if c['ok'] else '&#10007;'}</td>"
                           f"<td>{_e(c['name'])}</td><td class='muted'>{_e(c['detail'])}</td></tr>" for c in cl.get("checks", []))
                 + f"</table>{_e(cl.get('note', ''))}</div>")

    topo = data.get("topology") or {}
    if topo.get("edges"):
        parts.append("<h2>Dependency map</h2><div class='card scroll'><table><tr><th>From</th><th>To</th><th>Source</th><th>Detail</th></tr>"
                     + "".join(f"<tr><td>{_e(x['from'])}</td><td>{_e(x['to'])}</td><td>{_e(x['source'])}</td><td>{_e(x['label'])}</td></tr>"
                               for x in topo["edges"])
                     + f"</table><p><b>Not declared in compose:</b> {_e('; '.join(topo.get('undeclared_dependencies', [])))}</p>"
                     f"<details><summary class='muted'>Mermaid source</summary><pre>{_e(topo.get('mermaid', ''))}</pre></details></div>")

    parts.append("<h2>Manual findings (by hand, web UI)</h2><div class='card'><ul>"
                 + "".join(f"<li><b>{m['id']}</b> {_e(m['fault'])}: {_e(m['observation'])}</li>" for m in MANUAL_FINDINGS)
                 + "</ul></div></main></body></html>")
    return "\n".join(parts)
