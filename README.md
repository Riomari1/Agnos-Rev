# Revenue Ops Copilot

> A lightweight multi-agent workflow that ingests leads from a CSV, classifies urgency/risk/opportunity, prioritises follow-up actions, and produces operator-ready summaries.

Built as a take-home exercise for an AI-native startup engineering role. Powered by [Agno](https://github.com/agno-agi/agno) multi-agent patterns.

---

## Quick Start

### Prerequisites

- Python 3.12+
- PowerShell (Windows) or bash (macOS / Linux)

### Setup

```powershell
# 1. Clone and enter the project
cd agno-takehome

# 2. Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Copy and edit environment file
copy example.env .env
# Edit .env and set DEEPSEEK_API_KEY if you want LLM-powered classification.
# Without it, the workflow runs in deterministic rule-based mode.
```

### Run

```powershell
python -m app.main examples\leads.csv
```

For verbose / debug logging:

```powershell
python -m app.main examples\leads.csv -v
```

### Run Tests

```powershell
python -m pytest tests\ -v
```

### Output

After running, check the `outputs/` directory:

| File | Description |
|---|---|
| `summary.md` | Human-readable markdown report |
| `recommendations.json` | Structured action items |
| `execution_log.json` | Full metadata, timings, errors |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    RevenueOpsWorkflow                    │
│                    (orchestrator)                        │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │  Intake   │→│  Classify │→│  Action   │→│  Review  │ │
│  │  Agent    │  │  Agent    │  │  Agent    │  │  Agent   │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────┘ │
│       │              │             │            │        │
│       ▼              ▼             ▼            ▼        │
│  Validate &     Urgency /     Generate       Check      │
│  Normalise      Risk / Opp    Actions        Consistency│
│                                                         │
│  ┌────────────────────────────────────────────────┐     │
│  │           Shared WorkflowState                  │     │
│  │  (typed Pydantic models, passed by reference)   │     │
│  └────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────┘
```

### Agent Responsibilities

| Agent | Role |
|---|---|
| **Intake** | Validate emails, detect duplicates, flag malformed records |
| **Classify** | Score urgency (revenue/employee signals), risk (missing data, churn notes), opportunity (industry fit, growth keywords) |
| **Action** | Map classification scores to concrete follow-up actions with priority, assignee, and due-by date |
| **Review** | Cross-check that every valid lead has a classification and recommendation. Flags inconsistencies. |

All agents are **deterministic rule-based functions** by default. They operate on a shared `WorkflowState` object and return it after each step.

### Key Design Decisions

1. **No LLM dependency by default** — The demo runs without any API key. Classification uses simple scoring rules, making the workflow fast, deterministic, and interview-friendly.

2. **Typed state throughout** — Every agent receives and returns `WorkflowState`, which is a Pydantic model. This makes data flow explicit and testable.

3. **Retry wrapper** — The `_step()` method in the orchestrator wraps each agent call with retry logic (max 2 retries) and captures per-agent timing.

4. **CSV resilience** — Malformed rows are skipped individually (not fail-fast). Empty files and missing columns are handled gracefully.

5. **Clean output artifacts** — Three output files: a markdown summary for humans, a JSON log for machines, and structured recommendations for downstream systems.

---

## Project Structure

```
agno-takehome/
├── app/
│   ├── __init__.py
│   ├── main.py                # CLI entrypoint (argparse)
│   ├── agents/
│   │   ├── __init__.py
│   │   └── team.py            # Agent implementations
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py         # All Pydantic models
│   ├── tools/
│   │   └── __init__.py        # Extensible tool registry
│   └── workflows/
│       ├── __init__.py
│       └── workflow.py        # RevenueOpsWorkflow orchestrator
├── examples/
│   └── leads.csv              # Sample data (10 leads)
├── outputs/                   # Generated after each run
├── tests/
│   ├── __init__.py
│   └── test_workflow.py       # Integration + edge-case tests
├── .gitignore
├── example.env                # Environment template
├── requirements.txt
└── README.md
```

---

## Design Rationale

### Why not use Agno Agent classes directly?

The Agno library's `Agent` class is designed for LLM-backed agents. For this take-home exercise, we use **plain function agents** that operate on a shared typed state. This:

- Makes the workflow **deterministic** (no API calls = no flaky tests)
- Keeps the project **easy to understand in 5 minutes**
- Isolates the **agent logic** from the transport (Agno or otherwise)
- Still follows Agno's **multi-agent orchestration pattern** (sequential, state-passing)

To swap in real LLM agents, wrap each function in an `agno.Agent` with a system prompt and tool access.

### Why no database?

The spec says: "DO NOT add databases." All state lives in `WorkflowState` for the duration of a single run. Outputs are written to disk as JSON and Markdown. This keeps the project self-contained and trivially demoable.

### Why mock implementations?

Mock agents are intentional: they let the workflow run in <1 second without any external dependency, produce identical results every time, and make the test suite reliable. The scoring logic is still real — it's just implemented with `if` statements instead of an LLM.

---

## Known Limitations

- **Rule-based classification is simplistic** — Real RevOps would use LLM reasoning, historical conversion data, and CRM enrichment APIs.
- **No CRM integration** — Leads come from a CSV; there's no Salesforce/HubSpot sync.
- **No persistence** — State is ephemeral. Every run starts fresh.
- **Single-threaded** — Agents run sequentially. For thousands of leads, parallel processing would be needed.
- **Dedup is case-insensitive name match** — Real dedup considers domains, email, phone.

---

## Future Improvements

1. **LLM-powered classification** — Drop in an `agno.Agent` with DeepSeek for nuanced reasoning.
2. **CRM enrichment tool** — Use the Agno tool framework to enrich leads from Clearbit or Apollo.
3. **Parallel lead processing** — Use Python's `concurrent.futures` for batch classification.
4. **Confidence scoring** — Track per-field confidence and surface low-confidence classifications for human review.
5. **Slack / email output** — Push the summary to a channel instead of a local file.

---

## Where AI-Assisted Coding Helped

This entire project was scaffolded using an AI coding assistant. The AI was particularly helpful for:

| Area | How AI helped |
|---|---|
| **Pydantic schema design** | Ensuring correct field types, validators, and `model_dump()` compatibility |
| **CSV edge cases** | Handling BOM, empty rows, type coercion errors |
| **Rule-based scoring logic** | Quickly iterating on urgency/risk/opportunity threshold design |
| **README generation** | Producing well-structured markdown with consistent tone |
| **Test fixtures** | Generating realistic sample data and edge-case CSV files |
| **Windows compatibility** | Using `pathlib` and avoiding Linux-specific assumptions |

---

## License

This project is for evaluation purposes as part of a take-home exercise.
