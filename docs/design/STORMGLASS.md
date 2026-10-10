# Stormglass — ShutterStorm frontend reference

## Direction

A precise, calm resilience workbench. Make the difference between infrastructure health and user experience immediately visible. The signature composition is a narrow navigation rail, an editorial run headline, a compact outcome strip, and a large fault × journey matrix. A contextual evidence panel explains a selected fault. Avoid photographic backgrounds, neon effects, ornamental charts, and generic infrastructure KPIs.

The companion `showcase.html` is an interactive design reference, not a connected dashboard. Open it in a browser. It includes Results, Experiment plan, and Safety record screens, selectable findings, light/dark appearance, and a compact layout option. All displayed results are a historical example from `docs/FINDINGS_REPORT.md`, run `20261010-211247-run`; they are not current telemetry. Plan and safety controls in the showcase only change local presentation.

## Visual system

| Token | Light | Dark | Use |
|---|---|---|---|
| Canvas | #F3F5F4 | #101716 | App background |
| Surface | #FFFFFF | #18211F | Main panels |
| Subtle surface | #EAF0ED | #202D28 | Selected rows, secondary sections |
| Primary text | #172B25 | #E7EFEA | Titles and body |
| Secondary text | #56685F | #ACBDB3 | Labels and metadata |
| Divider | #D4DED8 | #35473D | Quiet structural separation |
| Action | #246746 | #A7E7BD | Primary control / active navigation |
| Action text | #FFFFFF | #11291B | On action fill |
| Healthy | #246746 | #A7E7BD | Successful user journeys |
| Hung / danger | #A12F3F | #FFABAF | Timeouts and emergency rollback |
| Slow / caution | #855615 | #F0CA85 | Slow responses |
| Fast failure | #8B461D | #FFC094 | Returned errors |

Use system sans-serif (Segoe UI on Windows), with a system monospace stack for IDs, timings, and logs. Display title 30/36 px, page section 18/26 px, body 14/21 px, metadata 12/18 px, outcome value 34/38 px. Weights 400 and 500. Use tabular numerals for metrics. Spacing scale: 4, 8, 12, 16, 24, 32. Panel radius 14 px; controls 8 px; badges 6 px. One-pixel borders, almost no shadow. Icons: Lucide, 16–18 px, accompanied by action text.

## Screen hierarchy and behavior

### Results (default showcase)

1. Persistent brand/navigation rail; environment and data provenance are visible.
2. Breadcrumb, run status, date, title: “Find the failure before your users do.” In production use an operational title such as “Run results”.
3. Outcome strip: resilience 81.5/100, six experiments, one auto-rollback. Never label a score as uptime, success probability, or a production SLO.
4. Fault × journey matrix. Preserve seven columns J0–J6 and their full labels. Cells say OK, Slow, Fail, or Hung: color never carries meaning alone. Selecting a fault updates the evidence panel below. Rank by backend severity, not score alone.
5. Detail: measured effect → safety response → proposed remediation. Keep “not applied / not tested” next to every fix. Distinguish AI analysis (unverified) from measured evidence. The showcase uses reviewed report prose rather than claiming its text is live AI output.

### Experiment plan

Show budget, ordered selected faults, failure class, and the planner's reason. Surface planner provenance (live, replay, template) from actual data. Show the scope and rollback settings adjacent to the start action. Primary workflow: generate plan → inspect → run. The current backend's dry plan is only available in logs; structured preview requires a small backend extension. Do not pretend a previous completed run's plan is the next execution plan.

Manual injection belongs in a secondary section with a catalog dropdown. Stop means stop at the next safe step. Panic means undo all active faults immediately. Keep both reachable during an active job. Panic remains available while ordinary start controls are disabled. Use a clear confirmation for panic consistent with the current dashboard; after submission display “Rollback requested”, not “Restored”, until verification arrives.

### Safety record

Separate three concepts: threshold-triggered rollback, restoration in progress, and verified cleanup. Show the documented database event (80% errors versus a 75% limit; stopped at 16 s of 35 s; recovery 0.6 s). Cleanup PASS and 18/18 checks apply only to the historical fixture; production must render evidence or “Not available”. Healthy container state alone is not proof of cleanup.

### Dependency graph

Add a fourth navigation item, Dependency graph. Show Browser / API → Immich server → PostgreSQL, Redis and Machine learning. Use the saved run topology, not an invented health map. On desktop use a left-to-right diagram; on mobile stack entry point and server above the three dependent services. Keep native selectable node buttons and readable labels at both sizes.

Group edges by service pair to reduce clutter, but retain source-specific evidence in the selected-service panel: Compose declaration, runtime configuration and empirical observations. Solid node outlines indicate Compose-declared dependencies; a dashed ML node indicates the missing Compose declaration. Explicit text carries the same meaning. ML is selected initially to surface the project's key discovery: `machineLearning.urls` reveals a dependency absent from Compose.

Keep predicted dependent journeys separate from observed impact. Redis is directly predicted for J4/J5, while the recorded experiments also affected J3/J6. A failed upload can block later checks without establishing a direct service dependency. Do not translate empirical edges into causal guarantees.

Read `nodes`, `edges` (`from`, `to`, `source`, `label`), `journey_deps`, and `undeclared_dependencies` from the selected run's `topology.json`. The existing `/graph` serves generated diagrams, but `/api/status` does not expose structured topology: add a validated, run-scoped topology response for this view. Keep real and simulated runs separate; display unavailable/missing topology explicitly. Escape labels and do not fetch arbitrary configured ML URLs from the browser. The showcase uses the documented historical graph in `docs/DEPENDENCY_GRAPH.md`.

## Data and integration contract

The existing frontend is the `PAGE` string in `shutterstorm/dashboard.py`, served by Python's standard-library server. Prefer extracting assets to local files and retaining this server/API before introducing a new framework. The prototype's styles and layout can be translated to any component system.

| UI element | Existing source/action | Integration caveat |
|---|---|---|
| Container state | GET `/api/status`: `containers` | Show running, paused and health separately; no inferred real-time graph |
| Pending faults | `journal` | Nonempty journal does not by itself mean rollback failed |
| Running job / log | `job.running`, `label`, `exit`, `log` | Poll at existing 1500 ms interval; preserve focus and scroll position |
| Run list | `runs` | Keep simulation and real data separate |
| Result strip | `latest.score`, `experiments`, `status` | Count auto-stopped experiments; null is unknown, not zero |
| Matrix / selection | `latest.experiments[].heatmap`, `fault`, `severity`, `score` | Map `ok`, `slow`, `fast-fail`, `hung`, `n/a`; retain full labels |
| Explanation / fix | experiment `analysis`, `analysis_source`, `suggestion` | Render untrusted strings as text; never apply a suggested diff |
| Completed plan | `latest.plan` | Historical plan only |
| New dry plan | POST `/api/plan` `{budget}` | Read current job log; add structured response for rich preview |
| Start AI run | POST `/api/plan-run` `{budget, strict}` | Honor `started:false`; budget supported by server is 1–8 |
| Manual fault | POST `/api/run` `{fault, strict}` | Populate from status `faults`; never invent fault IDs |
| Stop / panic | POST `/api/stop`, POST `/api/panic` | Request acknowledgement is not completion |
| Safety summary | `latest.thresholds`, `cleanup`, `auto_stopped` | Detailed check count and recovery need evidence fields added to API |
| Report / graph | `/runs/<encoded-id>/report.html`, `/graph` | Use existing links; never build paths from unvalidated data |
| Comparison | `latest.comparison.rows` | Before/after display exists; launching a comparison needs a new endpoint or CLI workflow |

Do not derive a phase percentage, live latency chart, guard heartbeat, or exact remaining time from raw logs. Add structured events before showing these as live facts. Expose simulation mode explicitly in status before adding its production banner.

## Required states

- Empty: “No experiments yet”, with Generate plan and environment check guidance; no synthetic scores.
- Planning/running: disable start actions, retain Stop/Panic; preserve latest completed results with their run ID.
- Auto-stopped: amber lifecycle label plus the breached threshold and rollback status; red is for actual failure/hung outcomes.
- Disconnected: last-updated timestamp, stale label, retry; never retain an unlabeled green “live” indicator.
- Failure: show server error or nonzero exit, offer evidence/log access. Do not equate process exit zero with verified restoration.
- Cleanup failed/unknown: prominent explicit status; do not replace missing evidence with PASS.
- Simulation: persistent SIMULATED banner on results, logs, exports and comparisons.
- No comparison: explain that a human must apply a fix before rerunning; never invent improvement figures.

## Responsive and accessibility rules

Above 900 px: 172 px rail; content fills remaining width. At 900 px and below: brand and wrapping navigation across the top. Below 640 px: stack metrics and detail panels; change the matrix into per-fault cards with seven labeled journey outcomes (no hidden columns). Preserve the selected finding when layouts change. Keep 44 px touch targets, visible focus rings, semantic headings and native controls. Announce selection/request outcomes politely. Respect reduced motion; transitions at most 120–180 ms. No blinking alarms or continuous pulses.

## Build order and acceptance

1. Extract and implement tokens, shell, navigation and responsive layout.
2. Wire status polling with loading, stale, empty and error states; escape data safely.
3. Add matrix, selected finding, report links, and comparison display using existing fields.
4. Wire job controls and disabled/requested/verified states. Preserve all backend safety checks.
5. Add structured plan, evidence and mode fields only where needed, with backend tests for the new contracts.

Check at 1440, 1024, 768 and 390 px, and 200% zoom. Keyboard users must reach navigation, every fault selector and emergency actions. Verify a disconnected server, empty runs, `started:false`, unknown heatmap values, missing cleanup, simulation, and an auto-stop. Compare both appearances to the showcase. Do not execute real faults just to review visual changes.

## Handoff prompt

Implement the Stormglass reference in `docs/design/STORMGLASS.md` and `docs/design/showcase.html` against the current ShutterStorm dashboard. Preserve its local-only server, fixed fault catalog, safety semantics, simulation separation, and existing user changes. Start with the existing API; explicitly add structured fields for unsupported presentation rather than fabricating telemetry. Use the example as visual guidance, never as production data. Deliver responsive results, planning and safety views plus meaningful integration checks.
