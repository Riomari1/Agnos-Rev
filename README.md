# Revenue Ops Copilot

**Track:** Option D - Revenue Operations / Operators Team
**Framework:** Agno 2.6 (`agno.workflow.Workflow` + `agno.Agent`)
**Primary demo:** CLI
**Runtime:** DeepSeek-backed Agno agents

Revenue Ops Copilot ingests a CSV of leads, validates and deduplicates records,
classifies urgency/risk/opportunity, recommends follow-up actions, reviews the
result, and writes operator-ready output artifacts.

The CLI is the ground-truth path. Streamlit uses the same workflow and agent
definitions. AgentOS/Agno registration is present, but the AgentOS UI workflow
path is currently not working reliably, so it should not be used as the primary
demo path.

## Quick Start

```bash
cd agno-takehome
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy example.env .env
# Set DEEPSEEK_API_KEY=...

python -m app.main examples/leads.csv
python -m pytest -q
```

There is no offline or rule-based runtime. CLI, Streamlit, and tests all require
`DEEPSEEK_API_KEY` and call DeepSeek through Agno agents.

## Demo Paths

| Path | Command | Purpose |
|---|---|---|
| CLI | `python -m app.main examples/leads.csv` | Primary demo with logs, exit codes, and outputs |
| Streamlit | `streamlit run demo/ui.py` | Upload CSV, inspect metrics and recommendations |
| AgentOS / Agno | `python -m app.agentos`, then connect at `os.agno.com` to `localhost:7777` | Known issue: agents register, but the AgentOS UI workflow path is currently not working reliably |

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
AgentOS registration. Each workflow step creates a real `agno.Agent` with a
DeepSeek model and validates the AI response into typed Pydantic models. Current
limitation: the Agno/AgentOS UI workflow path is not reliable enough for
evaluation, so the CLI remains the supported end-to-end runner.

## Agents

| Agent | Responsibility | Tools |
|---|---|---|
| IntakeAgent | Validate company/email fields, normalize records, flag duplicates | `DataQualityTool`, `RunWorkflowTool` registered for AgentOS inspection |
| ClassifyAgent | Score urgency, risk, opportunity, confidence, and rationale | DeepSeek JSON response validated with Pydantic |
| ActionAgent | Generate prioritized follow-up recommendations | `FollowUpSLATool` registered for AgentOS inspection |
| ReviewAgent | Approve/reject consistency and trigger repair loop when needed | DeepSeek JSON response validated with Pydantic |

The agent functions call `agent.run(input=prompt)` and require JSON that matches
the typed output schema. If DeepSeek is unavailable or does not return valid
schema-compatible JSON, the step fails and the workflow records the failure.

## Observability

Every run records:

- total workflow latency
- per-step latency
- success/failure status
- agent runtime mode (`deepseek` or skipped)
- parse errors and retry failures
- Agno workflow metadata (`session_id`, `run_id` when available)

Generated artifacts:

| File | Purpose |
|---|---|
| `outputs/summary.md` | Human-readable operator summary |
| `outputs/recommendations.json` | Machine-readable action list |
| `outputs/execution_log.json` | Structured trace with metrics and agent status |

## Resilience

Implemented failure-handling scenarios:

- malformed numeric fields are skipped row-by-row
- missing company names and malformed emails are marked invalid by the intake agent
- duplicate company names are flagged by the intake agent
- transient agent/tool failures retry up to 3 attempts
- review rejection reruns ActionAgent up to 2 repair iterations
- missing/failed DeepSeek calls produce explicit workflow failures
- pytest uses a workspace temp directory on Windows to avoid locked user temp roots

## Tests

```bash
python -m pytest -q
```

Current suite: 15 tests. Tests require `DEEPSEEK_API_KEY` and exercise the live
DeepSeek/Agno JSON response route. The retry tests still monkeypatch failure
functions because they intentionally validate retry behavior.

Coverage includes:

- end-to-end DeepSeek workflow
- CLI subprocess success path
- direct Agno/DeepSeek typed AI output execution
- empty/malformed CSVs
- intake validation
- retry success and retry exhaustion
- output artifact generation
- review rejection for missing classifications
- error-case CSV resilience

## Example CLI Output

```text
Revenue Ops Copilot - workflow started
  RUN  load_csv  [attempt 1/3]
  OK   load_csv  completed in 1 ms
  RUN  IntakeAgent  [attempt 1/3]
  OK   IntakeAgent  completed in 3500 ms
  RUN  ClassifyAgent  [attempt 1/3]
  OK   ClassifyAgent  completed in 4200 ms
  RUN  ActionAgent  [attempt 1/3]
  OK   ActionAgent  completed in 3900 ms
  RUN  ReviewAgent  [attempt 1/3]
  OK   ReviewAgent  completed in 1800 ms
Workflow complete
  Status:      Approved
  Total leads: 10
```

## Rubric Fit

| Rubric area | Evidence |
|---|---|
| Working end-to-end | CLI runs the full DeepSeek pipeline, writes outputs, has exit codes and tests |
| Multi-agent design | Four separate agents plus orchestrating workflow and review loop |
| Product/workflow sense | Prioritized rev-ops action list with assignee and due window |
| Code quality | Small modules, shared agent specs, typed state, focused tools |
| Typed interfaces | Pydantic models and validated AI JSON outputs |
| Reliability | retries, row-level validation, review repair loop, explicit DeepSeek failures |
| Observability | timing/status/mode metrics and execution log |
| Explanation | README includes architecture, tradeoffs, AI-assisted notes |

## Tradeoffs

- Tests call the live AI route, so they are slower and require a valid
  `DEEPSEEK_API_KEY`.
- The workflow is sequential. For this take-home scale, clarity and traceability
  matter more than parallel throughput.
- AgentOS/Agno is included for registration and inspection work, but its UI
  workflow execution path is currently not working reliably. CLI remains the
  supported primary demo.

## AI-Assisted Build Notes

- AI helped scaffold Pydantic models, tests, CSV examples, and README structure.
- AI initially over-indexed on local workarounds; those were removed so the
  workflow and tests use DeepSeek through Agno.
- Agno API details needed direct debugging: import paths, workflow `run`/`arun`
  behavior, AgentOS registration, and tool invocation semantics.
- Windows-specific pytest temp directory issues were fixed with repo-level
  pytest config.
- CLI remained the priority throughout because it is the most reliable way to
  evaluate the workflow against the rubric.

## Future Improvements

1. Add token usage metrics if exposed by the selected Agno/DeepSeek response.
2. Fix the AgentOS UI workflow path.
3. Add a human approval breakpoint before writing final recommendations.
4. Add CRM enrichment tools for account history and owner assignment.
5. Add an evaluation harness that scores recommendations against expected rubrics.
