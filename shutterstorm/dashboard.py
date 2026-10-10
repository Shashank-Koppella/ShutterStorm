"""Minimal local web UI: ``python -m shutterstorm dashboard`` -> http://127.0.0.1:8765

Shows the four Immich containers live, lets you run the AI-planned experiment series or one
fault (also combined failures) with a button, streams its log, and shows the result: the
plan with reasons, heatmap, score, auto-stops, AI analysis, the suggested fix (never applied)
and a before/after comparison. Also links every run's report.html and the dependency graph.

Only standard library; listens on 127.0.0.1 only. The buttons run the same CLI commands
(`run`, `run --experiments <fault>`, `plan`, `stop`, `panic`), so every safety layer still applies.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .config import CORE_CONTAINERS, Settings
from .context import build_context
from .faults import CATALOG

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ShutterStorm Dashboard</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#1c1c1e;--muted:#62666b;--line:#e1e1dc;--accent:#0b6e8a;--ok:#d7f0dc;--okf:#1e5b2b;
--warn:#fff1c2;--warnf:#6b5200;--ff:#ffd9b8;--fff:#7a3a00;--bad:#f6c1c1;--badf:#7d1010}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1d1f22;--fg:#e8e8e6;--muted:#a0a4a8;--line:#33363a;--accent:#5cc3dd;
--ok:#1f3d26;--okf:#a6e3b3;--warn:#3d3412;--warnf:#f1d77a;--ff:#45290f;--fff:#ffbf87;--bad:#4a1717;--badf:#ffb0b0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:1150px;margin:0 auto;padding:20px 16px 48px}h1{margin:0;font-size:24px}h2{font-size:17px;margin:0 0 10px}
.muted{color:var(--muted)}.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button,select{font:inherit;padding:7px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button.danger{color:var(--badf);border-color:var(--badf)}
button:disabled{opacity:.5;cursor:default}
.pill{display:inline-block;padding:2px 9px;border-radius:10px;font-size:13px;font-weight:600}
.ok{background:var(--ok);color:var(--okf)}.slow{background:var(--warn);color:var(--warnf)}.fast-fail{background:var(--ff);color:var(--fff)}
.hung,.bad{background:var(--bad);color:var(--badf)}.na{background:var(--line);color:var(--muted)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;font-size:12.5px;max-height:360px;margin:0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left}
td.c{text-align:center}.score{font-size:40px;font-weight:700;color:var(--accent);line-height:1}
a{color:var(--accent)}
</style></head><body><main>
<div class="row" style="justify-content:space-between"><div><h1>ShutterStorm</h1>
<div class="muted">AI-driven chaos engineering for Immich &middot; AI plans the experiments &rarr; inject &rarr; watch the effect &rarr; auto-rollback on threshold breach &rarr; ranked findings + suggested fixes (never applied)</div></div>
<div class="row"><a href="/graph" target="_blank">Dependency graph</a></div></div>

<div class="grid">
 <div class="card"><h2>Immich containers (live)</h2><table id="containers"></table>
  <div class="muted" id="journal" style="margin-top:8px"></div></div>
 <div class="card"><h2>Run experiments</h2>
  <div class="row"><button class="primary" id="planrun">Run AI plan</button>
  <select id="budget" title="experiments the planner selects"><option>3</option><option>4</option><option>5</option><option selected>6</option></select>
  <button id="plandry">Plan only (dry run)</button></div>
  <div class="row" style="margin-top:8px"><select id="fault"></select><button class="primary" id="go">Inject fault</button></div>
  <div class="row" style="margin-top:8px"><label><input type="checkbox" id="strict"> strict safety limits (demo the auto-rollback: error rate &gt; 10%, 1 round)</label></div>
  <div class="row" style="margin-top:8px"><button id="stop">Stop</button><button class="danger" id="panic">Panic</button></div>
  <p class="muted" id="jobstate" style="margin:10px 0 0">idle</p>
  <p class="muted" style="font-size:13px">One experiment at a time, ~35 s each, auto-undone (thresholds + TTL + guard). Stop = finish safely at next step; Panic = undo everything now.</p></div>
</div>

<div class="card" style="margin-top:14px"><h2>Live log</h2><pre id="log">no experiment running</pre></div>

<div class="card" style="margin-top:14px" id="result"><h2>Latest result</h2><div class="muted">no run yet</div></div>

<div class="card" style="margin-top:14px"><h2>All runs</h2><table id="runs"></table></div>
</main>
<script>
const J={J0:"ping",J1:"login",J2:"browse",J3:"smart search",J4:"upload",J5:"thumbnail",J6:"integrity"};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pill=(t,c)=>`<span class="pill ${c}">${esc(t)}</span>`;
async function post(u,b){await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});tick()}
const strict=()=>document.getElementById("strict").checked;
document.getElementById("go").onclick=()=>post("/api/run",{fault:document.getElementById("fault").value,strict:strict()});
document.getElementById("planrun").onclick=()=>post("/api/plan-run",{budget:+document.getElementById("budget").value,strict:strict()});
document.getElementById("plandry").onclick=()=>post("/api/plan",{budget:+document.getElementById("budget").value});
document.getElementById("stop").onclick=()=>post("/api/stop");
document.getElementById("panic").onclick=()=>{if(confirm("Undo every fault now and restore the checkpoint?"))post("/api/panic")};
let faultsLoaded=false;
async function tick(){
 let s; try{s=await (await fetch("/api/status")).json()}catch(e){return}
 if(!faultsLoaded){document.getElementById("fault").innerHTML=s.faults.map(f=>`<option value="${f.id}">${esc(f.id)} - ${esc(f.description)}</option>`).join("");faultsLoaded=true}
 for(const id of ["go","planrun","plandry"])document.getElementById(id).disabled=s.job.running;
 document.getElementById("containers").innerHTML="<tr><th>container</th><th>state</th><th>health</th></tr>"+s.containers.map(c=>{
  const st=c.paused?pill("PAUSED","hung"):(c.running?pill("running","ok"):pill("STOPPED","fast-fail"));
  const h=c.health==="healthy"?pill("healthy","ok"):pill(c.health||"-",c.health?"slow":"na");
  return `<tr><td>${esc(c.name)}</td><td>${st}</td><td>${h}</td></tr>`}).join("");
 document.getElementById("journal").innerHTML=s.journal.length?("Active faults (journal): "+s.journal.map(e=>pill(e,"hung")).join(" ")):"No active fault (journal empty)";
 document.getElementById("jobstate").textContent=s.job.running?("running: "+s.job.label):(s.job.label?("finished: "+s.job.label+" (exit "+s.job.exit+")"):"idle");
 const log=document.getElementById("log"); const atEnd=log.scrollTop+log.clientHeight>=log.scrollHeight-20;
 log.textContent=s.job.log||"no experiment running"; if(atEnd)log.scrollTop=log.scrollHeight;
 document.getElementById("runs").innerHTML="<tr><th>run</th><th>status</th><th>score</th><th>faults</th><th></th></tr>"+s.runs.map(r=>
  `<tr><td>${esc(r.name)}</td><td>${esc(r.status)}</td><td>${esc(r.score)}</td><td>${esc(r.faults)}</td><td>${r.report?`<a href="/runs/${encodeURIComponent(r.name)}/report.html" target="_blank">report</a>`:""}</td></tr>`).join("");
 const L=s.latest; if(!L)return;
 let h=`<h2>Latest result: ${esc(L.name)}</h2><div class="row" style="gap:24px;align-items:flex-start"><div><div class="score">${esc(L.score)}</div><div class="muted">resilience / 100</div></div>
  <div class="muted">status ${esc(L.status)} &middot; cleanup ${L.cleanup?pill("PASS","ok"):pill("FAIL","bad")} &middot; <a href="/runs/${encodeURIComponent(L.name)}/report.html" target="_blank">full report</a><br>safety limits: ${esc(L.thresholds)}</div></div>`;
 if(L.decisions&&L.decisions.length){h+=`<h3 style="margin:16px 0 6px">AI decisions: what, when, how safely</h3><table><tr><th>step</th><th>experiment</th><th>by</th><th>plan</th><th>strength / duration</th><th>safety limits</th><th>why</th></tr>`+
  L.decisions.map(d=>`<tr><td>${d.step}</td><td><b>${esc(d.fault)}</b></td><td>${esc(d.source)}</td><td>${d.followed_plan?"as planned":pill("changed plan","slow")}</td><td>${esc(Object.entries(d.settings).filter(([k])=>!k.startsWith("max_")).map(([k,v])=>k+"="+v).join(", "))}</td><td>${esc(d.safety)}</td><td>${esc(d.reason)}<br><span class="muted">${esc(d.safety_reason)}</span>${d.clamped&&d.clamped.length?`<br><span class="muted">clamped: ${esc(d.clamped.join("; "))}</span>`:""}</td></tr>`).join("")+"</table>"}
 if(L.plan&&L.plan.items&&L.plan.items.length){h+=`<h3 style="margin:16px 0 6px">Tentative plan <span class="muted">(by ${esc(L.plan.source)})</span></h3><p class="muted">${esc(L.plan.strategy||"")}</p><table><tr><th>#</th><th>experiment</th><th>class</th><th>why</th></tr>`+
  L.plan.items.map(i=>`<tr><td>${i.rank}</td><td><b>${esc(i.fault)}</b></td><td>${esc(i.failure_class)}</td><td>${esc(i.reason)}</td></tr>`).join("")+"</table>"}
 if(L.comparison&&L.comparison.rows&&L.comparison.rows.length){h+=`<h3 style="margin:16px 0 6px">Before / after <span class="muted">(vs ${esc(L.comparison.before_run)})</span></h3><table><tr><th>fault</th><th>before</th><th>after</th><th>delta</th><th>verdict</th></tr>`+
  L.comparison.rows.map(r=>`<tr><td>${esc(r.fault)}</td><td>${r.score_before}</td><td><b>${r.score_after}</b></td><td>${r.delta>0?"+":""}${r.delta}</td><td>${pill(r.verdict,r.verdict==="improved"?"ok":(r.verdict==="regressed"?"bad":"na"))}</td></tr>`).join("")+"</table>"}
 for(const e of L.experiments){
  h+=`<h3 style="margin:16px 0 6px">${esc(e.fault)} &middot; score ${esc(e.score)} &middot; severity ${esc(e.severity)} ${e.auto_stopped?pill("AUTO-STOPPED","hung"):""}</h3>`+
   (e.auto_stopped?`<p class="muted">Safety rollback after ${esc(e.auto_stopped.after_s)}s: ${esc(e.auto_stopped.reason)}</p>`:"")+
   (e.load?`<p class="muted">Traffic spike: ${e.load.sent} requests, ${e.load.requests_per_s}/s from ${e.load.workers} clients</p>`:"")+`<table><tr>`+Object.keys(J).map(j=>`<th>${j} ${J[j]}</th>`).join("")+"</tr><tr>"+
   Object.keys(J).map(j=>{const v=e.heatmap[j];return `<td class="c">${pill(v==="n/a"?"-":v,v==="n/a"?"na":v)}</td>`}).join("")+"</tr></table>";
  if(e.analysis)h+=`<p><b>What happened</b> <span class="muted">(AI: ${esc(e.analysis_source)}, unverified)</span><br>${esc(e.analysis)}</p>`;
  if(e.suggestion){const g=e.suggestion;
   h+=`<p><b>Suggested fix</b> <span class="muted">(by ${esc(g.source)}; not applied, not tested)</span><br>${esc(g.rationale||g.recommendation||"")}</p>`;
   if(g.diff)h+=`<pre>${esc(g.diff)}</pre>`;
   h+=`<p class="muted">Code-level fix: ${esc(g.template_code_fix)}</p>`}}
 document.getElementById("result").innerHTML=h;
}
tick();setInterval(tick,1500);
</script></body></html>"""

GRAPH_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Dependency Graph</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;background:#fff;color:#1c1c1e;font:15px/1.45 system-ui,Segoe UI,sans-serif}main{max-width:1200px;margin:0 auto;padding:20px 16px}
pre.src{background:#f6f6f4;border:1px solid #ddd;border-radius:8px;padding:10px;overflow:auto;font-size:12px}h2{margin-top:32px}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script></head><body><main>
<h1>ShutterStorm dependency graphs</h1><p>Source: docs/DEPENDENCY_GRAPH.md (generated from the code's imports and the live Immich config).
If the diagrams do not render (offline), the Mermaid source is shown instead.</p>
__SECTIONS__
<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:"strict"})}</script></main></body></html>"""


class Job:
    """The one experiment subprocess the dashboard may run at a time."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.proc: subprocess.Popen | None = None
        self.label = ""
        self.log_path = home / "runs" / "dashboard_job.log"
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], label: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log = self.log_path.open("w", encoding="utf-8")
            self.proc = subprocess.Popen([sys.executable, "-m", "shutterstorm", *args], cwd=str(self.home),
                                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.label = label
            return True

    def status(self) -> dict:
        text = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
        lines = [line for line in text.splitlines() if not line.startswith(("journey ", "J0 ", "J1 ", "J2 ", "J3 ",
                                                                            "J4 ", "J5 ", "J6 "))]
        return {"running": self.running, "label": self.label,
                "exit": None if self.proc is None else self.proc.poll(), "log": "\n".join(lines[-80:])}


def _runs(home: Path, simulate: bool = False) -> list[dict]:
    out = []
    for d in sorted((home / "runs").glob("*-run*"), reverse=True):
        f = d / "findings.json"
        # Never mix simulated and real results on the same screen.
        if not d.is_dir() or not f.exists() or d.name.endswith("-SIM") != simulate:
            continue
        if len(out) >= 15:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"name": d.name, "status": data.get("status"), "score": data.get("overall_score"),
                    "faults": ", ".join(e["fault"] for e in data.get("experiments", [])),
                    "report": (d / "report.html").exists(), "data": data})
    return out


def _latest(runs: list[dict]) -> dict | None:
    for r in runs:
        data = r["data"]
        if not data.get("experiments"):
            continue
        exps = []
        for e in data["experiments"]:
            sug = e.get("suggestion")
            exps.append({"fault": e["fault"], "score": e["scoring"]["score"],
                         "auto_stopped": e.get("auto_stopped"), "load": e.get("load"),
                         "severity": e["scoring"]["severity_label"], "heatmap": e["heatmap"],
                         "analysis": (e.get("analysis") or {}).get("summary"),
                         "analysis_source": (e.get("analysis") or {}).get("source"),
                         "suggestion": {k: sug.get(k) for k in ("source", "rationale", "recommendation", "diff",
                                                               "template_code_fix")} if sug else None})
        plan = data.get("plan") or {}
        return {"name": r["name"], "status": data.get("status"), "score": data.get("overall_score"),
                "cleanup": (data.get("cleanup") or {}).get("ok"), "experiments": exps,
                "thresholds": data.get("thresholds"), "comparison": data.get("comparison"),
                "plan": {"source": plan.get("source"), "strategy": plan.get("strategy"), "items": plan.get("items", [])},
                "decisions": data.get("decisions", [])}
    return None


def _graph_page(home: Path) -> str:
    doc = home / "docs" / "DEPENDENCY_GRAPH.md"
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    sections, title = [], ""
    for chunk in text.split("```mermaid")[1:]:
        src = chunk.split("```", 1)[0].strip()
        sections.append(f"<div class='mermaid'>{_esc(src)}</div><details><summary>Mermaid source</summary>"
                        f"<pre class='src'>{_esc(src)}</pre></details>")
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:]
            break
    body = "".join(f"<h2>Diagram {i + 1}</h2>{s}" for i, s in enumerate(sections)) or \
        "<p>docs/DEPENDENCY_GRAPH.md not found.</p>"
    return GRAPH_PAGE.replace("__SECTIONS__", body + (f"<p class='muted'>{_esc(title)}</p>" if not sections else ""))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serve(settings: Settings, port: int = 8765) -> None:
    ctx = build_context(settings)
    home = settings.home
    job = Job(home)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:
            path = unquote(self.path.split("?")[0])
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/graph":
                return self._send(200, _graph_page(home).encode(), "text/html; charset=utf-8")
            if path == "/api/status":
                containers = []
                for name in CORE_CONTAINERS:
                    st = ctx.docker.state(name)
                    containers.append({"name": name, **{k: st.get(k) for k in ("running", "paused", "health")}})
                runs = _runs(home, settings.simulate)
                return self._json({
                    "containers": containers,
                    "journal": [e["label"] for e in ctx.journal.entries()],
                    "job": job.status(),
                    "faults": [{"id": f.id, "description": f.description} for f in CATALOG.values() if not f.optional],
                    "runs": [{k: v for k, v in r.items() if k != "data"} for r in runs],
                    "latest": _latest(runs),
                })
            if path.startswith("/runs/") and path.endswith("/report.html"):
                name = path.split("/")[2]
                target = (home / "runs" / name / "report.html").resolve()
                if (home / "runs").resolve() in target.parents and target.exists():
                    return self._send(200, target.read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            strict = ["--max-error-rate", "0.1", "--breach-rounds", "1"] if body.get("strict") else []
            sim = ["--simulate"] if settings.simulate else []
            if self.path == "/api/run":
                fault = body.get("fault")
                if fault not in CATALOG:
                    return self._json({"error": "unknown fault"}, 400)
                args = ["run", "--experiments", fault, *strict]
                return self._json({"started": job.start([*sim, *args], " ".join(args))})
            if self.path in ("/api/plan-run", "/api/plan"):
                budget = str(max(1, min(8, int(body.get("budget") or 6))))
                args = ["run", "--budget", budget, *strict] if self.path == "/api/plan-run" else ["plan", "--budget", budget]
                return self._json({"started": job.start([*sim, *args], " ".join(args))})
            if self.path == "/api/stop":
                ctx.journal.request_stop()
                return self._json({"stop": True})
            if self.path == "/api/panic":
                subprocess.Popen([sys.executable, "-m", "shutterstorm", *(["--simulate"] if settings.simulate else []),
                                  "panic"], cwd=str(home),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._json({"panic": True})
            self._json({"error": "unknown"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ShutterStorm dashboard: http://127.0.0.1:{port}   (Ctrl+C to quit)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctx.close()
