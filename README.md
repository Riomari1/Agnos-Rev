# Revenue Ops Copilot

**Track:** Option D — Revenue Operations / Operators Team
**Framework:** Agno v2.6 (`agno.workflow.Workflow` + `agno.Agent`)
**Language:** Python 3.12+
**Size:** ~12 source files, ~700 lines of application code

---

A multi-agent workflow that ingests a CSV of leads, classifies each by urgency/risk/opportunity, generates prioritised follow-up actions, runs a review/self-correction loop, and writes operator-ready summaries — all deterministically, with no API key required.

## Why This Project Exists

Revenue operations teams sit on top of CRMs full of unprioritised leads. The gap isn't data — it's triage. Someone has to look at every lead, decide if it matters, figure out what to do, and hand it off. Most teams do this manually in a spreadsheet.

This project asks: what's the smallest useful automation you can put in front of an operator to replace that spreadsheet workflow? The answer is a pipeline that reads a CSV, has AI score every lead on three dimensions, generates concrete actions, and checks its own work before handing off.

## Quick Start

```bash
# Python 3.12+, any OS
cd agno-takehome
python -m venv .venv
source .venv/bin/activate   # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt

# Run the demo (no API key needed)
python -m app.main examples/leads.csv

# Run tests
python -m pytest tests/ -v
```

**Optional:** Copy `example.env` to `.env` and set `DEEPSEEK_API_KEY` to register the agents with a real LLM model (visible in AgentOS). The workflow itself stays deterministic either way.

## Demo Paths

| Path | Command | What it shows |
|---|---|---|
| **CLI** (primary) | `python -m app.main examples/leads.csv` | Full pipeline, structured logging, exit codes |
| **Streamlit** | `streamlit run demo/ui.py` | Browser upload, results table, agent timing |
| **AgentOS** | `python -m app.agentos` → connect to `localhost:7777` at [os.agno.com](https://os.agno.com) | Visual agent/team inspection, workflow execution from UI |

Each path runs the same pipeline. The CLI is the ground-truth demo — deterministic, no browser needed, clean terminal output.

## Multi-Agent Architecture

```
CSV ──▶ IntakeAgent ──▶ ClassifyAgent ──▶ ActionAgent ──▶ ReviewAgent ──▶ outputs/
            │                │                 │               │
            ▼                ▼                 ▼               ▼
        Validate &       Urgency /          Generate         Cross-check
        Normalise        Risk / Opp         Actions          + self-correct
```

Four agents, each a standalone function operating on a shared typed state object. No agent calls another directly — the orchestrator (`RevenueOpsWorkflow`) passes state between them. This keeps each agent testable in isolation and the pipeline easy to trace.

### Agent Responsibilities

| Agent | What it does | Input | Output |
|---|---|---|---|
| **IntakeAgent** | Validates emails, detects duplicates, flags malformed records | `list[LeadRecord]` | `list[LeadRecord]` with status flags + validation errors |
| **ClassifyAgent** | Scores urgency (revenue/employees), risk (missing data/churn), opportunity (industry/growth) | Validated leads | `dict[str, ClassificationResult]` indexed by company name |
| **ActionAgent** | Maps classifications to concrete follow-up actions with priority, assignee, and due-by window | Classifications | `list[ActionRecommendation]` sorted by priority |
| **ReviewAgent** | Cross-checks every lead has a classification and recommendation. Rejects inconsistent output, triggering a self-correction loop | Full state | `review_approved: bool` + review notes |

Each agent also exists as a real `agno.Agent` instance with a name, role-specific instructions, and registered `Toolkit` subclasses. In the current deterministic mode the orchestrator calls the plain functions directly. With `DEEPSEEK_API_KEY` set, the architecture supports swapping to `agent.run()` for LLM-powered reasoning — same interface, different implementation.

### Self-Correction Loop

```
ActionAgent ──▶ ReviewAgent
    ▲               │
    └── re-run ◀────┘ (if rejected, up to 2 attempts)
```

If ReviewAgent finds missing classifications or inconsistent recommendations, it sets `review_approved = False` and the orchestrator re-runs ActionAgent. This catches classification gaps and empty-action edge cases without operator intervention.

## Typed State & Orchestration

All inter-agent communication uses `WorkflowState`, a single Pydantic model:

```python
class WorkflowState(BaseModel):
    leads: list[LeadRecord]
    classifications: dict[str, ClassificationResult]
    recommendations: list[ActionRecommendation]
    review_notes: str
    review_approved: bool
    metrics: ExecutionMetrics
```

No dicts, no string munging, no implicit contracts. Every agent takes `WorkflowState` and returns `WorkflowState`. The orchestrator owns the flow:

```
RevenueOpsWorkflow._execute()
  ├── _step("load_csv", ...)       # CSV → LeadRecord objects
  ├── _step("intake", ...)         # Validate + deduplicate
  ├── _step("classify", ...)       # Score urgency/risk/opportunity
  ├── _step("action", ...)         # Generate recommendations
  └── self-correction loop (max 2):
      ├── _step("review", ...)     # Validate consistency
      └── _step("action", ...)     # Repair if rejected
```

`_step()` wraps every call with retry (3 attempts), per-attempt timing, overall duration tracking, and status capture.

## Agno Integration — Real, Not Fake

The project uses real Agno constructs at every level:

1. **`RevenueOpsWorkflow(Workflow)`** — inherits session management, run IDs, AgentOS registration, and the `deep_copy()` pattern for request isolation
2. **`agno.Agent` instances** — each with a name, `instructions`, `description`, model config, and registered `Toolkit` tools
3. **`agno.tools.Toolkit` subclasses** — `DataQualityTool` on IntakeAgent, `FollowUpSLATool` on ActionAgent, `RunWorkflowTool` for AgentOS chat
4. **`WorkflowRunOutput`** — the standard Agno return type for UI integration (both streaming and non-streaming paths supported)

The execution log captures `workflow.name`, `session_id`, `run_id`, and per-agent Agno names — traceable end-to-end.

## Tools

### DataQualityTool (`agno.tools.Toolkit`)
Registered on IntakeAgent. Three functions:
- `validate_email(email)` — regex-based format check
- `validate_company_name(name)` — presence check
- `check_duplicate(name, seen)` — intra-batch dedup

### FollowUpSLATool (`agno.tools.Toolkit`)
Registered on ActionAgent. Two functions:
- `recommend_deadline(urgency, risk, opportunity)` — maps classification to a 24h/48h/72h/1-week/2-week window
- `recommend_priority_note(urgency, risk, opportunity)` — generates a one-line rationale

### RunWorkflowTool (`agno.tools.Toolkit`)
Registered on IntakeAgent. Bridges AgentOS chat to the CSV pipeline — lets users type "process examples/leads.csv" in AgentOS chat and get results.

## Observability & Reliability

**Structured logging:** Every agent call, retry, timing, and error is logged at the appropriate level. The CLI shows a clean play-by-play; `--verbose` exposes debug detail including agent instructions.

**Per-agent timing:** Each agent's execution time is measured and stored in `ExecutionMetrics.agent_timings_ms`. Visible in CLI output, Streamlit metrics cards, and the JSON execution log.

**Execution log (`outputs/execution_log.json`):** Full metadata per run — workflow name, Agno session/run IDs, per-agent timing and status, lead/recommendation counts, review outcome.

**Three output artifacts per run:**

| File | Purpose |
|---|---|
| `summary.md` | Operator-facing markdown report with results table |
| `recommendations.json` | Machine-readable action items for downstream systems |
| `execution_log.json` | Full traceability: session/run IDs, agent timing, errors |

## Error Handling & Resilience

| Scenario | Behaviour |
|---|---|
| Malformed CSV row | Skipped individually, error logged, processing continues |
| Missing columns | Default to `None`/`""` |
| Empty file | 0 leads → ReviewAgent rejects, clear error message |
| Invalid email | Flagged with specific error, lead marked invalid |
| Duplicate company (same file) | Second instance marked `duplicate` |
| Duplicate company (cross-session) | Detected via `outputs/lead_cache.json` |
| Agent exception | Retried up to 3 times with per-attempt + overall timing |
| All retries exhausted | Agent marked `failure`, self-correction loop triggers |
| Review rejection | ActionAgent re-run up to 2 additional times |
| Pydantic validation error | Clean one-line message — no raw stack traces |

## Testing

12 pytest tests, all passing:

```
tests/test_workflow.py::test_workflow_end_to_end
tests/test_workflow.py::test_empty_csv
tests/test_workflow.py::test_malformed_csv
tests/test_workflow.py::test_intake_validation
tests/test_workflow.py::test_review_empty_input
tests/test_workflow.py::test_retry_on_agent_failure
tests/test_workflow.py::test_retry_exhaustion
tests/test_workflow.py::test_output_artifacts_generated
tests/test_workflow.py::test_review_rejects_missing_classifications
tests/test_workflow.py::test_review_rejects_empty_workflow
tests/test_workflow.py::test_error_cases_csv_resilience
tests/test_workflow.py::test_workflow_run_default_csv
```

Covers: end-to-end integration, empty/malformed CSVs, retry behaviour (success and exhaustion), output artifact generation, review rejection, error-case CSV resilience, and default input handling.

## Example Output

Running `python -m app.main examples/leads.csv`:

```
============================================================
Revenue Ops Copilot — workflow started
  Agno session : (not set)
  Input file   : examples/leads.csv
============================================================
  ⚙  load_csv  [attempt 1/3]
  ✓ load_csv  completed in 1 ms
  ⚙  IntakeAgent  [attempt 1/3]
  ✓ IntakeAgent  completed in 0 ms
  ⚙  ClassifyAgent  [attempt 1/3]
  ✓ ClassifyAgent  completed in 0 ms
  ⚙  ActionAgent  [attempt 1/3]
  ✓ ActionAgent  completed in 0 ms
  ⚙  ReviewAgent  [attempt 1/3]
  ✓ ReviewAgent  completed in 0 ms
============================================================
Workflow complete
  Status:      ✅ Approved
  Total leads: 10
  Valid:       10
  Recs:        17
  Duration:    3 ms
============================================================
```

Generated `outputs/summary.md` includes a full recommendations table with priority, company, action, assignee, and due-by columns — ready for operator handoff.

## Engineering Tradeoffs

### Deterministic rules vs. LLM reasoning
**Choice:** Rule-based classification functions that run in ~2ms with zero API dependencies.
**Why:** The rubric asks for a working demo. LLM calls add latency, cost, non-determinism, and an API key requirement. The architecture cleanly separates agent *definitions* (real `agno.Agent` instances with instructions) from agent *execution* (plain functions). Swapping to LLM-powered reasoning is a one-line change per agent.

### `run()` override vs. step-based configuration
**Choice:** Override `Workflow.run()` with a custom pipeline rather than configuring `self.steps`.
**Why:** The step-based API (`Step`, `Parallel`, `Loop`, `Router`) is powerful but heavyweight for a linear 4-agent pipeline. The custom `_execute()` + `_step()` pattern gives us retry, timing, and self-correction with ~150 lines of readable code. Tradeoff: streaming support requires a thin `arun` adapter (see `_arun_stream`).

### Sequential vs. parallel agent execution
**Choice:** Sequential.
**Why:** 10 leads process in ~2ms. Parallelism adds complexity with no measurable benefit at this scale. The architecture supports batching — Classification is per-lead and trivially parallelisable with `ThreadPoolExecutor` if lead count grows.

### No external dependencies in demo mode
**Choice:** Runs without any API key.
**Why:** Makes the demo reproducible anywhere. Reviewer can clone, install, run, and see results in under 30 seconds.

## Known Limitations

| What | Why it's fine | Production path |
|---|---|---|
| Rule-based scoring | Deterministic, instant, API-free | Swap `AGENT_REGISTRY` functions for `agent.run()` calls |
| No CRM integration | CSV is universal; same pipeline works with any data source | Add enrichment tool (Clearbit/Apollo pattern) |
| Name-only dedup | Sufficient for demo-quality lead lists | Add email domain + phone fuzzy matching |
| No persistence between runs | Cross-session cache (`lead_cache.json`) handles basic dedup | Use `Workflow.storage` for full session persistence |
| Sequential execution | ~2ms for 10 leads | `ThreadPoolExecutor` for batch classification |

## Why This Project Is Intentionally Compact

~12 source files. ~700 lines of application code. Readable in under 10 minutes.

This isn't a production system — it's a demonstration of engineering judgment. The goal was to show:

- Real framework integration without cargo-culting
- Typed, testable inter-agent communication
- Resilience patterns (retry, self-correction, graceful degradation)
- Multiple execution surfaces (CLI, Streamlit, AgentOS) from a single codebase
- The ability to identify where AI-assisted coding helps and where it doesn't

Every file has a clear job. There are no abstractions waiting for a future that hasn't arrived.

## AI-Assisted Development Notes

This project was built with an AI coding assistant. Here's what worked and what required human intervention:

**AI was effective at:**
- Pydantic model scaffolding and field validation
- CSV parsing edge cases (BOM, empty rows, type coercion)
- Rule-based scoring logic iteration (threshold tuning)
- Test fixture generation and edge-case CSV creation
- README structure and documentation consistency

**AI required correction on:**
- **Agno API surface** — hallucinated import paths (`agno.Agent` vs `from agno.agent import Agent`) and misunderstood the `_subclass_run` recursion pattern. Required reading the framework source.
- **`arun` contract** — initially wrote `async def arun`, but AgentOS's streaming path calls `arun` without `await` and expects an async generator. The parent class uses `def arun` (regular function) that branches on `stream`. Fixed by matching the parent's dispatch pattern.
- **Retry timing** — only tracked the last attempt's duration, not overall. Fixed by adding `overall_start`.
- **Test isolation** — class-level attribute patching didn't work because `__init__` overrode it at instance level. Fixed with `getattr(self.__class__, ...)`.
- **Exit behaviour** — used `sys.exit(1)` which breaks pytest. Separated CLI exit codes from workflow return values.

**Manually verified:**
- Full Agno MRO and method resolution order
- Retry count correctness across all 12 test scenarios
- UTF-8 BOM handling on Windows paths
- AgentOS streaming and non-streaming execution paths (via TestClient)

## Future Improvements

1. **LLM-powered classification** — swap rule functions for `agent.run()` in `AGENT_REGISTRY`. Same interface, richer reasoning.
2. **CRM enrichment tool** — `EnrichmentTool` subclassing `agno.tools.Toolkit`, mocking Clearbit/Apollo.
3. **Confidence threshold routing** — surface low-confidence classifications (`confidence < 0.6`) for manual operator review.
4. **Parallel batch processing** — `ThreadPoolExecutor` for classification across 1000+ leads.
5. **Persistent session storage** — use `Workflow.storage` with SQLite for run history and session continuity.

## Demo Walkthrough (3–5 Minute Screen Recording)

### 0:00–0:30 — Setup & Context
- Show terminal: `git log --oneline -5` (brief commit history)
- Show project tree: `ls app/ agents/ models/ tools/ workflows/`
- One sentence: "Four-agent pipeline that reads a CSV, classifies leads, generates actions, and checks its own work."

### 0:30–1:30 — CLI Demo (Primary Path)
- Run: `python -m app.main examples/leads.csv`
- Point out: structured logging, per-agent timing, retry counters
- Show `outputs/summary.md` — operator-facing markdown with recommendations table
- Show `outputs/recommendations.json` — machine-readable output
- Show `outputs/execution_log.json` — Agno session/run IDs, agent timing, errors

### 1:30–2:00 — Resilience Demo
- Run: `python -m app.main examples/leads_error_cases.csv`
- Point out: malformed rows skipped, valid rows still processed, no crash
- Run: `python -m pytest tests/ -v` (12 passing)
- Highlight one test: `test_retry_on_agent_failure` or `test_error_cases_csv_resilience`

### 2:00–2:30 — Streamlit UI
- Run: `streamlit run demo/ui.py`
- Upload `examples/leads.csv` through browser
- Click ▶ Run Workflow
- Show: metrics cards, recommendations dataframe, agent timing

### 2:30–3:30 — AgentOS Integration
- Run: `python -m app.agentos`
- Open browser: connect to `localhost:7777` at os.agno.com
- Show four agents visible in the UI (IntakeAgent, ClassifyAgent, ActionAgent, ReviewAgent)
- Show each agent's instructions and registered tools
- Run workflow from AgentOS: type `examples/leads.csv` in the message box
- Show result: markdown summary with recommendations

### 3:30–4:00 — Code Walkthrough (Key Files)
- Open `app/workflows/workflow.py` — show `_execute()` pipeline, `_step()` retry wrapper, self-correction loop
- Open `app/agents/team.py` — show `AGENT_REGISTRY` pattern (agno.Agent + plain function), one agent function
- Open `app/models/schemas.py` — show `WorkflowState`, typed inter-agent contract
- Open `app/tools/data_quality.py` — show `DataQualityTool(Toolkit)` registration

### 4:00–4:30 — Engineering Decisions
- Why deterministic: runs in ~2ms, no API key, reproducible anywhere
- Why custom `_execute()` over `self.steps`: simpler for a linear pipeline, gives retry + timing control
- Why typed state: every agent receives and returns `WorkflowState` — testable, traceable, no dicts
- Why compact: ~12 files, ~700 lines, readable in 10 minutes

### 4:30–5:00 — Close
- Quick mention: self-correction loop (ReviewAgent → re-run ActionAgent)
- Quick mention: AgentOS streaming fix (the `arun` contract adaptation)
- "Happy to dive deeper into any part."

## License

For evaluation purposes as part of a take-home exercise.
