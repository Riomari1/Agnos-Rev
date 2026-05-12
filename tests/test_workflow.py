"""
Lightweight integration test for the Revenue Ops Copilot workflow.

Tests cover:
    - Full workflow on known sample data
    - Empty / malformed CSV handling
    - Missing company name validation
    - Duplicate detection
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.agents.team import intake_agent, review_agent
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
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def empty_csv(tmp_path: Path) -> Path:
    """CSV with only a header row and no data."""
    path = tmp_path / "empty.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company_name", "contact_email", "industry"])
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


# ------------------------------------------------------------------
# Full workflow integration test
# ------------------------------------------------------------------


def test_workflow_end_to_end(sample_csv: Path) -> None:
    """Run the full pipeline and verify outputs."""
    wf = RevenueOpsWorkflow()
    state = wf.run(sample_csv)

    assert state.metrics.success is True
    assert state.metrics.total_leads == 3
    assert state.metrics.valid_leads == 2  # third is duplicate
    assert state.metrics.invalid_leads == 1
    assert len(state.recommendations) >= 2  # at least 2 valid leads get recs
    assert state.review_approved is True

    # Verify classification outputs
    assert "Alpha" in state.classifications
    assert "Beta" in state.classifications

    # Alpha: revenue=100, employees=500 → medium opportunity (score=2)
    # Would need employees>1000 or revenue>100 for high
    alpha = state.classifications["Alpha"]
    assert alpha.opportunity == OpportunityLevel.medium
    assert alpha.urgency in (UrgencyLevel.high, UrgencyLevel.medium)

    # Beta: no email (score=2), revenue=5 (>1 so no extra) → medium risk
    # Beta: revenue=5, employees=30, industry=Finance → low opportunity (score=0)
    beta = state.classifications["Beta"]
    assert beta.risk == RiskLevel.medium
    assert beta.opportunity == OpportunityLevel.low


# ------------------------------------------------------------------
# Edge case tests
# ------------------------------------------------------------------


def test_empty_csv(empty_csv: Path) -> None:
    """Empty CSV (header only) should produce zero leads and not crash."""
    wf = RevenueOpsWorkflow()
    state = wf.run(empty_csv)

    assert state.metrics.total_leads == 0
    assert state.metrics.valid_leads == 0
    assert state.metrics.success is False  # review catches empty input
    assert len(state.recommendations) == 0


def test_malformed_csv(malformed_csv: Path) -> None:
    """Malformed rows are skipped; valid ones still process."""
    wf = RevenueOpsWorkflow()
    state = wf.run(malformed_csv)

    # "Foo" row has invalid revenue → skipped
    # "Bar" row should parse and process
    assert state.metrics.total_leads == 1
    assert state.leads[0].company_name == "Bar"


def test_intake_validation() -> None:
    """Direct test of the intake agent's validation logic."""
    state = WorkflowState(
        leads=[
            LeadRecord(
                company_name="", contact_email="bad-email"
            ),  # invalid: no name, bad email
            LeadRecord(company_name="Valid Inc", contact_email="ok@valid.com"),  # valid
        ]
    )
    state = intake_agent(state)

    assert state.leads[0].status == LeadStatus.invalid
    assert len(state.leads[0].validation_errors) >= 1
    assert state.leads[1].status == LeadStatus.valid
    assert state.metrics.invalid_leads == 1
    assert state.metrics.valid_leads == 1


def test_review_empty_input() -> None:
    """Review should flag empty input as not approved."""
    state = WorkflowState()
    state = review_agent(state)
    assert state.review_approved is False
    assert "No leads" in state.review_notes
