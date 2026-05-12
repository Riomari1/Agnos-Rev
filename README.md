# Revenue Ops Copilot

**Track:** Option D - Revenue Operations / Operators Team
**Framework:** Agno 2.6 (`agno.workflow.Workflow` + `agno.Agent`)
**Primary demo:** CLI
**Runtime:** DeepSeek-backed Agno agents with an explicit local fallback mode

Revenue Ops Copilot ingests a CSV of leads, validates and deduplicates records,
classifies urgency/risk/opportunity, recommends follow-up actions, reviews the
result, and writes operator-ready output artifacts.

The CLI is the ground-truth path. Streamlit and AgentOS use the same workflow
and agent definitions.

## Quick Start

```bash
cd agno-takehome
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Optional but recommended for the primary AI runtime
copy example.env .env
# Set DEEPSEEK_API_KEY=...

# Run the primary CLI demo
python -m app.main examples/leads.csv

# Run tests
python -m pytest -q
```

Runtime modes are controlled by `REVENUE_OPS_AGENT_MODE`:

| Mode | Behavior |
|---|---|
| `auto` | Default. Uses DeepSeek when `DEEPSEEK_API_KEY` is set; otherwise local fallback. |
| `deepseek` | Always builds Agno agents with `DeepSeek`; falls back if the API returns no structured tool output. |
| `local` | Deterministic local runtime for tests, offline demos, and fast CLI smoke checks. |

The local fallback is not the main product path. It is a resilience path so the
CLI remains runnable during network/API failures and so tests do not depend on
live model access.

## Demo Paths

| Path | Command | Purpose |
|---|---|---|
| CLI | `python -m app.main examples/leads.csv` | Primary demo with logs, exit codes, and outputs |
| Streamlit | `streamlit run demo/ui.py` | Upload CSV, inspect metrics and recommendations |
| AgentOS | `python -m app.agentos`, then connect at `os.agno.com` to `localhost:7777` | Inspect the four registered Agno agents and workflow |

For a fast offline demo:

```bash
$env:REVENUE_OPS_AGENT_MODE="local"
python -m app.main examples/leads.csv
```

## Architecture

```text
CSV -> IntakeAgent -> ClassifyAgent -> ActionAgent -> ReviewAgent -> outputs/
```

The workflow uses a typed `WorkflowState` Pydantic model for every handoff:

```python
class WorkflowState(BaseModel):
    leads: list[LeadRecord]
    classifications: dict[str, ClassificationResult]
    recommendations: list[ActionRecommendation]
    review_notes: str
    review_approved: bool
    metrics: ExecutionMetrics
```

The same `AgentSpec` definitions power CLI execution, Streamlit, tests, and
AgentOS registration. Each agent is created as a real `agno.Agent` with a
DeepSeek model and role-specific tools.

## Agents

| Agent | Responsibility | Tools |
|---|---|---|
| IntakeAgent | Validate company/email fields, normalize records, flag duplicates | `DataQualityTool`, `RunWorkflowTool`, `IntakeTools` |
| ClassifyAgent | Score urgency, risk, opportunity, confidence, and rationale | `ClassifyTools` |
| ActionAgent | Generate prioritized follow-up recommendations | `FollowUpSLATool`, `ActionTools` |
| ReviewAgent | Approve/reject consistency and trigger repair loop when needed | `ReviewTools` |

The agent functions call `agent.run(input=prompt)` with structured toolkits
instead of parsing free-form JSON. If the model call fails or returns without
complete tool output, the step records `local_fallback` in metrics and produces
a deterministic result so the CLI still completes.

## Observability

Every run records:

- total workflow latency
- per-step latency
- success/failure status
- runtime mode per agent (`deepseek`, `local`, `local_fallback`, or skipped)
- parse errors and retry failures
- Agno workflow metadata (`session_id`, `run_id` when available)

Generated artifacts:

| File | Purpose |
|---|---|
| `outputs/summary.md` | Human-readable operator summary |
| `outputs/recommendations.json` | Machine-readable action list |
| `outputs/execution_log.json` | Structured trace with metrics and agent runtime modes |

## Resilience

Implemented failure-handling scenarios:

- malformed numeric fields are skipped row-by-row
- missing company names and malformed emails are marked invalid
- duplicate company names are flagged
- transient agent/tool failures retry up to 3 attempts
- review rejection reruns ActionAgent up to 2 repair iterations
- DeepSeek/API failure falls back locally and records the fallback mode
- pytest uses a workspace temp directory on Windows to avoid locked user temp roots

## Tests

```bash
python -m pytest -q
```

Current suite: 14 tests.

Coverage includes:

- end-to-end workflow
- CLI subprocess success path
- DeepSeek/Agno tool wiring via a mocked `AgnoAgent.run`
- empty/malformed CSVs
- intake validation
- retry success and retry exhaustion
- output artifact generation
- review rejection for missing classifications
- error-case CSV resilience

The DeepSeek wiring test verifies the four agents are real `AgnoAgent`
instances using a `DeepSeek` model and the expected toolkits, without requiring
live network access.

## Example CLI Output

```text
Revenue Ops Copilot - workflow started
  RUN  load_csv  [attempt 1/3]
  OK   load_csv  completed in 1 ms
  RUN  IntakeAgent  [attempt 1/3]
  OK   IntakeAgent  completed in 0 ms
  RUN  ClassifyAgent  [attempt 1/3]
  OK   ClassifyAgent  completed in 0 ms
  RUN  ActionAgent  [attempt 1/3]
  OK   ActionAgent  completed in 1 ms
  RUN  ReviewAgent  [attempt 1/3]
  OK   ReviewAgent  completed in 0 ms
Workflow complete
  Status:      Approved
  Total leads: 10
  Recs:        16
```

## Rubric Fit

| Rubric area | Evidence |
|---|---|
| Working end-to-end | CLI runs locally, writes outputs, has exit codes and tests |
| Multi-agent design | Four separate agents plus orchestrating workflow and review loop |
| Product/workflow sense | Prioritized rev-ops action list with assignee and due window |
| Code quality | Small modules, shared agent specs, typed state, focused tools |
| Typed interfaces | Pydantic models and structured tool calls |
| Reliability | retries, row-level validation, review repair loop, DeepSeek fallback |
| Observability | timing/status/mode metrics and execution log |
| Explanation | README includes architecture, tradeoffs, AI-assisted notes |

## Tradeoffs

- DeepSeek is the primary runtime, but tests do not call the live API. This keeps
  verification fast, repeatable, and CI-safe while still testing Agno/DeepSeek
  construction and tool wiring.
- The local fallback uses deterministic heuristics. It keeps the CLI demo usable
  during API failures, but production scoring should rely on DeepSeek or a more
  formal evaluator.
- The workflow is sequential. For this take-home scale, clarity and traceability
  matter more than parallel throughput.
- AgentOS is included for inspection and workflow execution, but CLI remains the
  polished primary demo.

## AI-Assisted Build Notes

- AI helped scaffold Pydantic models, tests, CSV examples, and README structure.
- AI initially over-indexed on deterministic shortcuts; the final version makes
  DeepSeek/Agno the primary runtime and labels local execution explicitly.
- Agno API details needed direct debugging: import paths, workflow `run`/`arun`
  behavior, AgentOS registration, and tool invocation semantics.
- Test design was corrected to avoid live API dependence while still verifying
  the real Agno DeepSeek agent setup.
- Windows-specific pytest temp directory issues were fixed with repo-level
  pytest config.
- CLI remained the priority throughout because it is the most reliable way to
  evaluate the workflow against the rubric.

## Future Improvements

1. Add token usage metrics if exposed by the selected Agno/DeepSeek response.
2. Add a human approval breakpoint before writing final recommendations.
3. Add CRM enrichment tools for account history and owner assignment.
4. Add an evaluation harness that scores recommendations against expected rubrics.
5. Add parallel classification for large CSVs.
