"""
Integration and edge-case tests for the Revenue Ops Copilot.

All tests run against the real AI pipeline (DeepSeek via tools).
Requires DEEPSEEK_API_KEY in .env (loaded by conftest.py).
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
    path = tmp_path / "leads.csv"
    _write_csv(
        path,
        [
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
        ],
    )
    return path


@pytest.fixture
def empty_csv(tmp_path: Path) -> Path:
    path = tmp_path / "empty.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["company_name", "contact_email", "industry"])
    return path


@pytest.fixture
def malformed_csv(tmp_path: Path) -> Path:
    path = tmp_path / "bad.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("company_name,revenue_millions\nFoo,not_a_number\nBar,100\n")
    return path


@pytest.fixture
def fragile_agent_csv(tmp_path: Path) -> Path:
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
    state = RevenueOpsWorkflow.run_sync(sample_csv)

    assert state.metrics.success is True
    assert state.metrics.total_leads == 3
    assert state.metrics.valid_leads >= 1
    assert len(state.recommendations) >= 1
    assert state.review_approved is True

    assert isinstance(state, BaseModel)
    assert isinstance(state.metrics, BaseModel)
    assert all(isinstance(r, BaseModel) for r in state.recommendations)
    assert all(isinstance(c, BaseModel) for c in state.classifications.values())

    # Alpha should be classified (it's the valid one)
    assert "Alpha" in state.classifications
    alpha = state.classifications["Alpha"]
    assert isinstance(alpha.urgency, UrgencyLevel)
    assert isinstance(alpha.risk, RiskLevel)
    assert isinstance(alpha.opportunity, OpportunityLevel)
    assert 0.0 <= alpha.confidence <= 1.0


# ------------------------------------------------------------------
# Edge case tests
# ------------------------------------------------------------------


def test_empty_csv(empty_csv: Path) -> None:
    state = RevenueOpsWorkflow.run_sync(empty_csv)
    assert state.metrics.total_leads == 0
    assert state.metrics.valid_leads == 0
    assert state.metrics.success is False
    assert len(state.recommendations) == 0


def test_malformed_csv(malformed_csv: Path) -> None:
    state = RevenueOpsWorkflow.run_sync(malformed_csv)
    assert state.metrics.total_leads == 1
    assert state.leads[0].company_name == "Bar"
    # Bar has company_name and revenue but no email/industry -
    # LLM intake may mark it valid or invalid, both are reasonable
    assert state.metrics.valid_leads + state.metrics.invalid_leads == 1


def test_intake_validation() -> None:
    """Direct intake: empty name + bad email flagged, clean record passes."""
    state = WorkflowState(
        leads=[
            LeadRecord(company_name="", contact_email="bad-email"),
            LeadRecord(company_name="Valid Inc", contact_email="ok@valid.com"),
        ]
    )
    state = intake_agent_fn(state)

    # First lead has empty name -> should be invalid
    empty_lead = next(l for l in state.leads if l.company_name in ("", "UNKNOWN"))
    assert empty_lead.status == LeadStatus.invalid
    assert len(empty_lead.validation_errors) >= 1

    # Second lead is clean -> should be valid
    valid_lead = next(l for l in state.leads if l.company_name == "Valid Inc")
    assert valid_lead.status == LeadStatus.valid

    assert state.metrics.valid_leads >= 1
    assert state.metrics.invalid_leads >= 1
    assert state.metrics.total_leads == 2


def test_review_empty_input() -> None:
    """Review should reject empty workflow state."""
    state = review_agent_fn(WorkflowState())
    assert state.review_approved is False


def test_review_rejects_empty_workflow() -> None:
    """Review with zero leads should be rejected."""
    state = review_agent_fn(WorkflowState(leads=[]))
    assert state.review_approved is False


# ------------------------------------------------------------------
# Retry behaviour tests (mock agent functions, no LLM calls)
# ------------------------------------------------------------------


def test_retry_on_agent_failure(fragile_agent_csv: Path) -> None:
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


def test_output_artifacts_generated(sample_csv: Path, tmp_path: Path) -> None:
    import app.workflows.workflow as wf_mod

    wf_mod.RevenueOpsWorkflow._output_dir_override = tmp_path

    state = RevenueOpsWorkflow.run_sync(sample_csv)

    recs_path = tmp_path / "recommendations.json"
    assert recs_path.exists()
    recs = json.loads(recs_path.read_text(encoding="utf-8"))
    assert isinstance(recs, list)
    assert len(recs) == len(state.recommendations)
    assert all("company_name" in r and "action" in r for r in recs)

    log_path = tmp_path / "execution_log.json"
    assert log_path.exists()
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log["workflow"]["name"] == "RevenueOpsCopilot"
    assert "metrics" in log
    assert log["review_approved"] is True

    summary_path = tmp_path / "summary.md"
    assert summary_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "Revenue Ops Copilot" in summary_text
    assert "Approved" in summary_text


# ------------------------------------------------------------------
# Review rejection scenarios
# ------------------------------------------------------------------


def test_review_rejects_missing_classifications(sample_csv: Path) -> None:
    """If classify does nothing, review should reject."""
    import app.agents.team as team_mod

    def _skip(s: WorkflowState) -> WorkflowState:
        return s

    orig = team_mod.AGENT_REGISTRY.copy()
    team_mod.AGENT_REGISTRY["classify"] = (team_mod.classify, _skip)
    try:
        state = RevenueOpsWorkflow.run_sync(sample_csv)
        assert state.review_approved is False
    finally:
        team_mod.AGENT_REGISTRY.update(orig)


def test_error_cases_csv_resilience() -> None:
    csv_path = Path("examples/leads_error_cases.csv")
    assert csv_path.exists(), f"{csv_path} not found"

    state = RevenueOpsWorkflow.run_sync(csv_path)

    # 15 rows total, 3 parse errors = 12 loaded
    assert state.metrics.total_leads == 12
    assert state.metrics.valid_leads >= 1
    assert state.metrics.invalid_leads >= 1
    assert len(state.metrics.errors) >= 3
    assert len(state.recommendations) >= 1
    assert state.metrics.success is True


def test_workflow_run_default_csv() -> None:
    from agno.run.workflow import WorkflowRunOutput

    csv_path = Path("examples/leads.csv")
    assert csv_path.exists(), f"{csv_path} not found"

    wf = RevenueOpsWorkflow()
    result = wf.run(input=None)

    assert isinstance(result, WorkflowRunOutput)
    assert result.content is not None
    assert "Approved" in result.content
    assert "Leads:" in result.content
