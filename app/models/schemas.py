"""
Pydantic models for the Revenue Ops Copilot workflow.

Everything flows as typed objects through the agent pipeline,
enabling validation, serialization, and clear interfaces.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UrgencyLevel(str, Enum):
    """Urgency of follow-up for a lead."""

    low = "low"
    medium = "medium"
    high = "high"


class RiskLevel(str, Enum):
    """Risk rating for a lead."""

    low = "low"
    medium = "medium"
    high = "high"


class OpportunityLevel(str, Enum):
    """Opportunity potential for a lead."""

    low = "low"
    medium = "medium"
    high = "high"


class LeadStatus(str, Enum):
    """Normalised status after intake validation."""

    valid = "valid"
    invalid = "invalid"
    duplicate = "duplicate"


class LeadRecord(BaseModel):
    """A single lead or account record from the CSV intake."""

    company_name: str = Field(description="Company or account name")
    contact_email: Optional[str] = Field(None, description="Primary contact email")
    industry: Optional[str] = Field(None, description="Industry vertical")
    revenue_millions: Optional[float] = Field(
        None, ge=0, description="Annual revenue in USD millions"
    )
    employees: Optional[int] = Field(None, ge=0, description="Number of employees")
    lead_source: Optional[str] = Field(None, description="Origin of the lead")
    notes: Optional[str] = Field(None, description="Free-text notes or context")
    status: LeadStatus = Field(
        default=LeadStatus.valid, description="Validation status"
    )
    validation_errors: list[str] = Field(
        default_factory=list, description="Why invalid"
    )


class ClassificationResult(BaseModel):
    """Output of the classification / enrichment agent."""

    company_name: str
    industry: Optional[str] = None
    urgency: UrgencyLevel = UrgencyLevel.medium
    risk: RiskLevel = RiskLevel.medium
    opportunity: OpportunityLevel = OpportunityLevel.medium
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    enrichment_notes: str = ""


class ActionRecommendation(BaseModel):
    """A recommended follow-up action for a classified lead."""

    company_name: str
    action: str = Field(description="Short action description, e.g. 'Send warm intro'")
    priority: int = Field(default=2, ge=1, le=3, description="1 = highest, 3 = lowest")
    assignee: str = Field(default="SDR Team", description="Suggested owner")
    rationale: str = Field(description="Why this action is recommended")
    due_by: Optional[str] = Field(None, description="Suggested timeframe, e.g. '48h'")


class ExecutionMetrics(BaseModel):
    """Timing, status, and error tracking for a workflow run."""

    start_time: Optional[str] = None
    end_time: Optional[str] = None
    total_duration_ms: Optional[float] = None
    total_leads: int = 0
    valid_leads: int = 0
    invalid_leads: int = 0
    agent_timings_ms: dict[str, float] = Field(default_factory=dict)
    agent_statuses: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    success: bool = False


class AgentResult(BaseModel):
    """Wrapper returned by every agent execution step."""

    agent_name: str
    status: str = "success"  # success | failure
    error: Optional[str] = None
    duration_ms: float = 0.0
    data: Optional[BaseModel] = None


class IntakeAgentOutput(BaseModel):
    """Structured output from the LLM-powered IntakeAgent."""

    validated_leads: list[LeadRecord] = Field(
        default_factory=list,
        description="Leads with status, validation_errors, and dedup flags set",
    )


class ClassifyAgentOutput(BaseModel):
    """Structured output from the LLM-powered ClassifyAgent."""

    classifications: list[ClassificationResult] = Field(
        default_factory=list,
        description="One ClassificationResult per valid lead, keyed by company_name",
    )


class ActionAgentOutput(BaseModel):
    """Structured output from the LLM-powered ActionAgent."""

    recommendations: list[ActionRecommendation] = Field(
        default_factory=list,
        description="Prioritised follow-up actions, 1–2 per lead",
    )


class ReviewAgentOutput(BaseModel):
    """Structured output from the LLM-powered ReviewAgent."""

    approved: bool = Field(description="True if all consistency checks pass")
    notes: str = Field(description="Review findings and recommendations")


class WorkflowState(BaseModel):
    """Shared state passed through the entire workflow pipeline."""

    leads: list[LeadRecord] = Field(default_factory=list)
    classifications: dict[str, ClassificationResult] = Field(default_factory=dict)
    recommendations: list[ActionRecommendation] = Field(default_factory=list)
    review_notes: str = ""
    review_approved: bool = False
    metrics: ExecutionMetrics = Field(default_factory=ExecutionMetrics)
    input_path: str = ""
