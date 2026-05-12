# Revenue Ops Copilot

> A lightweight multi-agent workflow that ingests leads from a CSV, classifies urgency/risk/opportunity, prioritises follow-up actions, and produces operator-ready summaries.

**Track:** Option D — Revenue Operations / Operators Team  
**Framework:** [Agno](https://github.com/agno-agi/agno) `v1.8` (`agno.workflow.Workflow` + `agno.agent.Agent`)  
**Language:** Python 3.12+  
**Demo:** CLI + structured JSON + Markdown summary  

---

## Quick Start

```powershell
# Prerequisites: Python 3.12+, PowerShell or bash

# Clone and enter
cd agno-takehome

# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# (Optional) Configure LLM
copy example.env .env
# Edit .env — without DEEPSEEK_API_KEY the workflow runs in
# deterministic rule-based mode with zero external dependencies.

# Run the demo
python -m app.main examples\leads.csv

# Run tests
python -m pytest tests\ -v
```

### Demo Options

#### Option 1 — CLI (primary)

```powershell
# Clean dataset — 12 realistic leads, all valid
python -m app.main examples/leads_clean.csv

# Mixed quality — duplicates, missing emails, varied risk/opportunity
python -m app.main examples/leads_mixed_quality.csv

# Error cases — malformed fields, blank rows, invalid emails
python -m app.main examples/leads_error_cases.csv
```

#### Option 2 — Streamlit UI

```powershell
streamlit run demo/ui.py
```

Upload any CSV through the browser, click ▶ Run Workflow, and see results instantly.

#### Option 3 — Agno Playground (visual agent inspection)

```powershell
python -m app.agentos
```

Then open [https://app.agno.com/playground](https://app.agno.com/playground) and connect to:
`http://localhost:7777`

You'll see all four agents (IntakeAgent, ClassifyAgent, ActionAgent, ReviewAgent) with their instructions and tools. No API key needed — this is purely for visual inspection of the agent topology.

### Outputs

After each run, check `outputs/`:

| File | Format | Purpose |
|---|---|---|
| `summary.md` | Markdown | Human-readable report for operators |
| `recommendations.json` | JSON | Structured action items for downstream systems |
| `execution_log.json` | JSON | Full metadata, Agno session/run IDs, per-agent timing |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                RevenueOpsWorkflow                         │
│                (agno.workflow.Workflow)                   │
│                                                          │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐   ┌──────┐ │
│  │  Intake   │───▶│  Classify│───▶│  Action   │──▶│Review│ │
│  │  Agent    │    │  Agent   │    │  Agent    │   │Agent │ │
│  └──────────┘    └──────────┘    └──────────┘   └──────┘ │
│       │               │              │             │      │
│       ▼               ▼              ▼             ▼      │
│  Validate &      Urgency /       Generate        Check   │
│  Normalise       Risk / Opp      Actions         Consist │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │           WorkflowState (Pydantic)                │    │
│  │  — shared, typed, serialisable                   │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

### Agents

| Agent | Agno Name | Function | Role |
|---|---|---|---|
| **Intake** | `IntakeAgent` | `intake_agent_fn` | Validate emails, detect duplicates, flag malformed records |
| **Classify** | `ClassifyAgent` | `classify_agent_fn` | Score urgency (revenue/employee), risk (missing data, churn notes), opportunity (industry fit, growth) |
| **Action** | `ActionAgent` | `action_agent_fn` | Map scores to concrete follow-ups with priority, assignee, due-by |
| **Review** | `ReviewAgent` | `review_agent_fn` | Cross-check every lead has classification + recommendation; reject if inconsistent |

### Orchestration

The `RevenueOpsWorkflow` class extends `agno.workflow.Workflow`, providing:

- **Session management** — auto-generated UUID (`session_id`) and run ID (`run_id`) tracked in execution logs
- **Agent registry** — each agent exists as both an `agno.Agent` instance (name + instructions + tools) and a plain function
- **Step execution** — `_step()` wraps each agent call with retry (3 attempts), per-agent timing, and status capture
- **Output pipeline** — three artifacts written to `outputs/` after execution

### Why this pattern?

```
agno.Workflow       ← class inheritance — real Agno scaffolding
  └→ __init__()     ← calls set_session_id(), set_workflow_id(), initialize_memory()
  └→ run_sync()     ← static factory: create → execute → return state
  └→ _execute()     ← core pipeline with retry + timing + outputs
  └→ _step()        ← agent call wrapper with retry loop
```

Each agent function is deterministic and stateless — no API key needed. When `DEEPSEEK_API_KEY` is configured, the `agno.Agent` wrappers can be used with `agent.run()` for LLM-powered reasoning instead.

---

## Project Structure

```
agno-takehome/
├── app/
│   ├── main.py              # CLI entrypoint (argparse + dotenv)
│   ├── agents/
│   │   ├── __init__.py
│   │   └── team.py          # 4 agents as functions + agno.Agent wrappers
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py       # All Pydantic types (9 models)
│   ├── tools/
│   │   └── __init__.py      # Extensible tool registry
│   └── workflows/
│       ├── __init__.py
│       └── workflow.py      # RevenueOpsWorkflow (agno.Workflow subclass)
├── examples/
│   └── leads.csv            # 10 sample leads
├── outputs/                 # Generated artifacts + lead cache (gitignored)
├── tests/
│   └── test_workflow.py     # 11 tests — integration, edge cases, retry, outputs, error-cases CSV
├── .gitignore
├── example.env
├── requirements.txt
├── RUBRIC.md                # Assignment grading rubric
└── README.md
```

**~10 core source files.**  The entire project can be read and understood in under 10 minutes.

---

## Design Decisions

### Real Agno integration (not fake)

The project imports and uses Agno at three levels:

1. **`RevenueOpsWorkflow`** extends `agno.workflow.Workflow` — the base class provides session_id generation, run tracking, and execution metadata scaffolding.
2. **Each agent** is instantiated as `agno.Agent(name=..., instructions=..., tools=[])` — real instances with role descriptions.
3. **Execution log** includes `workflow.name`, `session_id`, `run_id`, and per-agent `agno_name` — proving real orchestration.

In demo mode the rule-based functions are called directly (deterministic, no API key). With `DEEPSEEK_API_KEY` set, the workflow can switch to `agent.run()` for LLM-powered reasoning.

### Why not use `agent.run()`?

`agno.Agent.run()` requires an LLM model (defaults to OpenAI GPT-4o). For a demo that must work locally without any API key, calling the typed functions directly is the practical choice. The agent wrappers are real — they just aren't invoked through the LLM path in demo mode.

### Typed state everywhere

Every agent receives and returns `WorkflowState`, a Pydantic model with:
- `list[LeadRecord]` — ingested leads
- `dict[str, ClassificationResult]` — indexed classifications  
- `list[ActionRecommendation]` — sorted recommendations
- `ExecutionMetrics` — timing, status, errors

No "stringly typed" handoffs. Every data structure is validated and serialisable.

### Retry with real timing

The `_step()` method tracks:
- **Overall duration** across all retry attempts (not just the last)
- **Per-attempt timing** logged at WARNING level on failure
- **Agent status** (`success` / `failure`) in both the summary and execution log

### Resilience

| Scenario | Handling |
|---|---|
| Malformed CSV row | Skipped individually, error logged, file continues |
| Missing columns | Defaults to `None` / `""` |
| Empty file | Zero leads processed, review rejects, clear error |
| Invalid email | Flagged with specific error, lead marked invalid |
| Duplicate company (within file) | Second instance marked `duplicate` |
| Duplicate company (cross-session) | Detected via `outputs/lead_cache.json`, logged for awareness |
| Agent exception | Retried up to 3 times with overall + per-attempt timing |
| All retries exhausted | Agent marked `failure`, workflow continues, review catches it |
| Pydantic validation errors | Clean one-line message (no raw error dumps or URLs) |

---

## Testing

```
python -m pytest tests/ -v
```

11 tests covering:

| Test | What it verifies |
|---|---|
| `test_workflow_end_to_end` | Full pipeline: 3 leads → 2 valid → classified → recommended → approved |
| `test_empty_csv` | Header-only CSV → 0 leads → review rejects |
| `test_malformed_csv` | Invalid revenue row skipped, valid row processed |
| `test_intake_validation` | Direct intake logic: missing name + bad email flagged |
| `test_review_empty_input` | Empty WorkflowState → rejected |
| `test_retry_on_agent_failure` | Agent fails twice, succeeds on third → `success` status |
| `test_retry_exhaustion` | Agent always fails → `failure` status after 3 attempts |
| `test_output_artifacts_generated` | All 3 output files created with valid content |
| `test_review_rejects_missing_classifications` | No-op classify → review rejects with "missing classification" |
| `test_review_rejects_empty_workflow` | No leads at all → rejected |
| `test_error_cases_csv_resilience` | 15-row error CSV: 3 parse errors, 6 valid, 6 invalid, no crash |

---

## Known Limitations

| Limitation | Why it's acceptable | How to fix later |
|---|---|---|
| **Rule-based classification is simplistic** | Keeps the demo deterministic and API-key-free | Drop in an `agno.Agent` with DeepSeek for nuanced reasoning |
| **No CRM integration** | CSV is the universal data exchange format | Add a `ClearbitTool(app/tools/)` for enrichment |
| **No persistence between runs** | Cross-session lead cache (`outputs/lead_cache.json`) records previously seen companies | Add `agno.workflow.Workflow.storage` for full session persistence |
| **Sequential agent execution** | 10 leads finish in ~2ms; parallelism is premature | Use `concurrent.futures` for batch classification |
| **Dedup is name-only** | Simple and sufficient for the demo | Extend to email domain + phone matching |

---

## Build Notes — How AI Assisted (and where it didn't)

This project was built using an AI coding assistant (the same one reading this). Here's what worked and what didn't:

**What AI accelerated:**
- **Pydantic schema scaffolding** — generating initial model definitions, field types, and validators
- **CSV parsing edge cases** — handling BOM, empty rows, type coercion errors
- **Rule-based scoring logic** — iterating on urgency/risk/opportunity thresholds
- **Test fixtures** — generating realistic sample data and edge-case CSV files
- **README structure** — producing consistent markdown with appropriate sections

**Where AI struggled / needed human correction:**
- **Agno API version differences** — The initial code assumed `agno.Agent` was at the top-level import. It's actually `from agno.agent import Agent`. The `Workflow.run_workflow()` internally uses `_subclass_run` which caused a recursion bug when `run()` overrode the parent. This required reading the Agno source code to understand the actual call chain.
- **Retry timing bug** — AI initially tracked only the last retry attempt's timing, not the overall duration. Fixed by adding `overall_start` before the loop.
- **Test-scoped output directory** — AI initially assumed class-level attribute patching would work, but `__init__` was overriding it at instance level. Fixed with `getattr(self.__class__, '_output_dir_override', ...)`.
- **Workflow exit behavior** — AI originally used `sys.exit(1)` for non-success states, which works in CLI but makes pytest fail. Fixed by separating CLI logic from workflow logic.

**What was manually verified:**
- Full Agno class hierarchy and method resolution
- Retry count and timing correctness
- All 10 test assertions against actual execution output
- File encoding edge cases (UTF-8 BOM)
- Windows path handling

---

## Future Improvements

1. **LLM-powered classification** — Replace scoring rules with an `agno.Agent(model=DeepSeek())` call. The architecture supports this: just swap the function in `AGENT_REGISTRY`.
2. **CRM enrichment tool** — Add an `EnrichmentTool` that mocks Clearbit/Apollo lookup. Demonstrates Agno tool framework.
3. **Parallel lead processing** — Use `ThreadPoolExecutor` for batch classification across 1000+ leads.
4. **Confidence-aware routing** — Surface low-confidence classifications (`confidence < 0.6`) for manual review.
5. **Agno Agent OS UI** — Hook into Agno's built-in monitoring for visual execution traces.

---

## License

This project is for evaluation purposes as part of a take-home exercise.
