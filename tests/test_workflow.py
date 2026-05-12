"""
Integration and edge-case tests for the Revenue Ops Copilot workflow.

When DEEPSEEK_API_KEY is available (via .env), all agent functions
use the LLM path via DeepSeek.  When absent, they fall back to
deterministic rules.  Assertions are written to pass under both modes.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agents.team import intake_agent_fn, review_agent_fn
from app.models.schemas import (
    LeadRecord,
    LeadStatus,
    OpportunityLevel,
    RiskLevel,
    UrgencyLevel,
    WorkflowState,
)
from app.workflows.workflow import RevenueOpsWorkflow

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """Create a small, valid CSV for testing."""
    path = tmp_path / "leads.csv"
    rows = [
        {
            "company_name": "Alpha",
            "contact_email": "a@alpha.io",
            "industry": "Tech",
            "revenue_millions": "100",
            "employees": "500",
        },
        {
            "company_name": "Beta",
            "contact_email": "",
            "industry": "Finance",
            "revenue_millions": "5",
            "employees": "30",
        },
        {
            "company_name": "Alpha",
            "contact_email": "dup@alpha.io",
            "industry": "Tech",
            "revenue_millions": "100",
            "employees": "500",
        },
    ]
    _write_csv(path, rows)
    return path


@pytest.fixture
def empty_csv(tmp_path: Path) -> Path:
    """CSV with only a header row and no data."""
    path = tmp_path / "empty.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["company_name", "contact_email", "industry"])
    return path


@pytest.fixture
def malformed_csv(tmp_path: Path) -> Path:
    """CSV with an invalid numeric field."""
    path = tmp_path / "bad.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("company_name,revenue_millions\n")
        f.write("Foo,not_a_number\n")
        f.write("Bar,100\n")
    return path


@pytest.fixture
def fragile_agent_csv(tmp_path: Path) -> Path:
    """A single valid lead for retry/error tests."""
    path = tmp_path / "simple.csv"
    _write_csv(
        path,
        [
            {
                "company_name": "Gamma",
                "contact_email": "g@gamma.io",
                "revenue_millions": "50",
            }
        ],
    )
    return path


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------
# Full workflow integration test
# ------------------------------------------------------------------


def test_workflow_end_to_end(sample_csv: Path) -> None:
    """Run the full pipeline and verify typed outputs."""
    state = RevenueOpsWorkflow.run_sync(sample_csv)

    assert state.metrics.success is True
    assert state.metrics.total_leads == 3
    assert (
        state.metrics.valid_leads == 2
    )  # third is duplicate (neither valid nor invalid)
    assert len(state.recommendations) >= 2
    assert state.review_approved is True

    # Typed state assertions
    assert isinstance(state, BaseModel)
    assert isinstance(state.metrics, BaseModel)
    assert all(isinstance(r, BaseModel) for r in state.recommendations)
    assert all(isinstance(c, BaseModel) for c in state.classifications.values())

    # Verify classifications exist with valid enum values
    assert "Alpha" in state.classifications
    assert "Beta" in state.classifications

    alpha = state.classifications["Alpha"]
    assert isinstance(alpha.urgency, UrgencyLevel)
    assert isinstance(alpha.risk, RiskLevel)
    assert isinstance(alpha.opportunity, OpportunityLevel)
    assert 0.0 <= alpha.confidence <= 1.0

    beta = state.classifications["Beta"]
    assert isinstance(beta.urgency, UrgencyLevel)
    assert isinstance(beta.risk, RiskLevel)
    assert isinstance(beta.opportunity, OpportunityLevel)


# ------------------------------------------------------------------
# Edge case tests
# ------------------------------------------------------------------


def test_empty_csv(empty_csv: Path) -> None:
    """Empty CSV (header only) -> zero leads, review rejects."""
    state = RevenueOpsWorkflow.run_sync(empty_csv)
    assert state.metrics.total_leads == 0
    assert state.metrics.valid_leads == 0
    assert state.metrics.success is False
    assert len(state.recommendations) == 0


def test_malformed_csv(malformed_csv: Path) -> None:
    """Malformed rows skipped; valid ones still process."""
    state = RevenueOpsWorkflow.run_sync(malformed_csv)
    assert state.metrics.total_leads == 1
    assert state.leads[0].company_name == "Bar"
    assert state.metrics.valid_leads == 1


def test_intake_validation() -> None:
    """Direct intake agent validation logic."""
    state = WorkflowState(
        leads=[
            LeadRecord(company_name="", contact_email="bad-email"),
            LeadRecord(company_name="Valid Inc", contact_email="ok@valid.com"),
        ]
    )
    state = intake_agent_fn(state)
    assert state.leads[0].status == LeadStatus.invalid
    assert len(state.leads[0].validation_errors) >= 1
    assert state.leads[1].status == LeadStatus.valid
    assert state.metrics.invalid_leads == 1
    assert state.metrics.valid_leads == 1


def test_review_empty_input() -> None:
    """Review should reject empty input."""
    state = review_agent_fn(WorkflowState())
    assert state.review_approved is False
    assert "leads" in state.review_notes.lower()


# ------------------------------------------------------------------
# Retry behaviour tests
# ------------------------------------------------------------------


def test_retry_on_agent_failure(fragile_agent_csv: Path) -> None:
    """Workflow retries when an agent raises an exception."""
    call_count = 0

    def _failing_fn(s: WorkflowState) -> WorkflowState:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError(f"Simulated failure #{call_count}")
        return s

    import app.agents.team as team_mod

    orig = team_mod.AGENT_REGISTRY.copy()
    team_mod.AGENT_REGISTRY["classify"] = (team_mod.classify, _failing_fn)
    try:
        state = RevenueOpsWorkflow.run_sync(fragile_agent_csv)
        assert state.metrics.agent_statuses.get("classify") == "success"
        assert call_count == 3
    finally:
        team_mod.AGENT_REGISTRY.update(orig)


def test_retry_exhaustion(fragile_agent_csv: Path) -> None:
    """Workflow marks agent as failed when all retries exhausted.

    NOTE: the self-correction loop re-runs the action agent up to 2
    additional times when review rejects, so the counter is higher
    than MAX_ATTEMPTS alone.
    """
    counter = [0]

    def _always_fails(s: WorkflowState) -> WorkflowState:
        counter[0] += 1
        raise RuntimeError("Permanent failure")

    import app.agents.team as team_mod

    orig = team_mod.AGENT_REGISTRY.copy()
    team_mod.AGENT_REGISTRY["action"] = (team_mod.action, _always_fails)
    try:
        state = RevenueOpsWorkflow.run_sync(fragile_agent_csv)
        assert state.metrics.agent_statuses.get("action") == "failure"
        assert counter[0] > 3
    finally:
        team_mod.AGENT_REGISTRY.update(orig)


# ------------------------------------------------------------------
# Output artifact tests
# ------------------------------------------------------------------


def test_output_artifacts_generated(
    sample_csv: Path, monkeypatch, tmp_path: Path
) -> None:
    """Running the workflow creates all three output files with valid content."""
    import app.workflows.workflow as wf_mod

    wf_mod.RevenueOpsWorkflow._output_dir_override = tmp_path

    state = RevenueOpsWorkflow.run_sync(sample_csv)

    # recommendations.json
    recs_path = tmp_path / "recommendations.json"
    assert recs_path.exists()
    recs = json.loads(recs_path.read_text(encoding="utf-8"))
    assert isinstance(recs, list)
    assert len(recs) == len(state.recommendations)
    assert all("company_name" in r and "action" in r for r in recs)

    # execution_log.json
    log_path = tmp_path / "execution_log.json"
    assert log_path.exists()
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert "workflow" in log
    assert log["workflow"]["name"] == "RevenueOpsCopilot"
    assert "metrics" in log
    assert log["review_approved"] is True

    # summary.md
    summary_path = tmp_path / "summary.md"
    assert summary_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "# Revenue Ops Copilot" in summary_text
    assert "Approved" in summary_text


# ------------------------------------------------------------------
# Review rejection scenarios
# ------------------------------------------------------------------


def test_review_rejects_missing_classifications(sample_csv: Path) -> None:
    """If classify agent does nothing, review should reject."""
    import app.agents.team as team_mod

    def _skip(s: WorkflowState) -> WorkflowState:
        return s  # no-op: no classifications added

    orig = team_mod.AGENT_REGISTRY.copy()
    team_mod.AGENT_REGISTRY["classify"] = (team_mod.classify, _skip)
    try:
        state = RevenueOpsWorkflow.run_sync(sample_csv)
        assert state.review_approved is False
        assert (
            "classified" in state.review_notes.lower()
            or "classification" in state.review_notes.lower()
        )
    finally:
        team_mod.AGENT_REGISTRY.update(orig)


def test_review_rejects_empty_workflow() -> None:
    """Running with no leads at all should be rejected."""
    state = review_agent_fn(WorkflowState(leads=[]))
    assert state.review_approved is False
    assert "leads" in state.review_notes.lower() or "empty" in state.review_notes.lower()


def test_error_cases_csv_resilience() -> None:
    """The error-cases CSV should not crash; malformed rows skipped, valid processed."""
    csv_path = Path("examples/leads_error_cases.csv")
    assert csv_path.exists(), f"{csv_path} not found"

    state = RevenueOpsWorkflow.run_sync(csv_path)

    # Rows 3, 8, 9 have fatal parse errors -> skipped (3 rows dropped)
    # 15 rows total - 3 skip = 12 loaded
    assert state.metrics.total_leads == 12

    # At least some leads should be valid and some invalid
    assert state.metrics.valid_leads >= 1
    assert state.metrics.invalid_leads >= 1

    # Errors captured for the parse-skipped rows
    assert len(state.metrics.errors) >= 3

    # Valid leads produced recommendations
    assert len(state.recommendations) >= 1

    # Workflow completed without crashing
    assert state.metrics.success is True


def test_workflow_run_default_csv() -> None:
    """``Workflow.run(input=None)`` defaults to ``examples/leads.csv``."""
    from agno.run.workflow import WorkflowRunOutput

    csv_path = Path("examples/leads.csv")
    assert csv_path.exists(), f"{csv_path} not found"

    wf = RevenueOpsWorkflow()
    result = wf.run(input=None)

    assert isinstance(result, WorkflowRunOutput)
    assert result.content is not None
    assert "Approved" in result.content
    assert "Leads:" in result.content
