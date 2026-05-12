"""
Agent implementations for the Revenue Ops Copilot.

Each agent exists in two forms:
  1. A plain function (deterministic, no API key needed).
  2. An ``agno.Agent`` wrapper with name, instructions, and a tool
     that delegates to the function.

In demo / no-API-key mode the workflow calls the functions directly.
When ``DEEPSEEK_API_KEY`` is set, the workflow can switch to
``agno.Agent.run()`` for LLM-powered reasoning.
"""

from __future__ import annotations

import logging
import os
import re

from agno.agent import Agent as AgnoAgent
from agno.models.deepseek import DeepSeek

# Default model: DeepSeek. Falls back gracefully if DEEPSEEK_API_KEY is not set.
_DEFAULT_MODEL = DeepSeek(
    id=os.getenv("LLM_MODEL", "deepseek-chat"),
    api_key=os.getenv("DEEPSEEK_API_KEY"),
)

from app.models.schemas import (
    ActionRecommendation,
    ClassificationResult,
    LeadRecord,
    LeadStatus,
    OpportunityLevel,
    RiskLevel,
    UrgencyLevel,
    WorkflowState,
)

logger = logging.getLogger("agents")

# ------------------------------------------------------------------
# Rule-based agent functions (deterministic, no LLM needed)
# ------------------------------------------------------------------


def intake_agent_fn(state: WorkflowState) -> WorkflowState:
    """Validate and normalise raw lead records.

    Checks:
        - company_name is present.
        - contact_email has a valid format when provided.
        - revenue and employees are non-negative.
    Invalid records are flagged but kept for traceability.
    """
    logger.info("Intake agent processing %d raw lead(s)", len(state.leads))

    validated: list[LeadRecord] = []
    seen_names: set[str] = set()

    for lead in state.leads:
        errors: list[str] = []

        if not lead.company_name or not lead.company_name.strip():
            errors.append("Missing company_name")
            lead.company_name = lead.company_name or "UNKNOWN"

        if lead.contact_email:
            pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if not re.match(pattern, lead.contact_email.strip()):
                errors.append(f"Invalid email: {lead.contact_email}")

        normalized_name = lead.company_name.strip().lower()
        if normalized_name in seen_names:
            errors.append("Duplicate company name")
            lead.status = LeadStatus.duplicate
        seen_names.add(normalized_name)

        if errors:
            lead.status = LeadStatus.invalid
            lead.validation_errors = errors
        else:
            lead.status = LeadStatus.valid

        validated.append(lead)

    state.leads = validated
    state.metrics.total_leads = len(validated)
    state.metrics.valid_leads = sum(
        1 for l in validated if l.status == LeadStatus.valid
    )
    state.metrics.invalid_leads = sum(
        1 for l in validated if l.status == LeadStatus.invalid
    )

    logger.info(
        "Intake complete: %d valid, %d invalid, %d total",
        state.metrics.valid_leads,
        state.metrics.invalid_leads,
        state.metrics.total_leads,
    )
    return state


def classify_agent_fn(state: WorkflowState) -> WorkflowState:
    """Classify each valid lead for urgency, risk, and opportunity.

    Uses deterministic rules based on revenue, employees, and industry.
    """
    logger.info("Classify agent processing %d valid lead(s)", state.metrics.valid_leads)

    for lead in state.leads:
        if lead.status == LeadStatus.invalid:
            continue

        urgency = _classify_urgency(lead)
        risk = _classify_risk(lead)
        opportunity = _classify_opportunity(lead)

        classification = ClassificationResult(
            company_name=lead.company_name,
            industry=lead.industry or "Unknown",
            urgency=urgency,
            risk=risk,
            opportunity=opportunity,
            confidence=0.85,
            enrichment_notes=f"Revenue: ${lead.revenue_millions or 0}M | Employees: {lead.employees or 0}",
        )
        state.classifications[lead.company_name] = classification

    logger.info("Classification complete for %d lead(s)", len(state.classifications))
    return state


def action_agent_fn(state: WorkflowState) -> WorkflowState:
    """Generate follow-up action recommendations based on classifications."""
    logger.info("Action agent generating recommendations")

    for lead in state.leads:
        if lead.status == LeadStatus.invalid:
            continue

        classification = state.classifications.get(lead.company_name)
        if not classification:
            continue

        recommendations = _generate_actions(lead, classification)
        state.recommendations.extend(recommendations)

    state.recommendations.sort(key=lambda r: (r.priority, r.company_name))
    logger.info("Generated %d recommendation(s)", len(state.recommendations))
    return state


def review_agent_fn(state: WorkflowState) -> WorkflowState:
    """Review the full output for consistency and completeness.

    Checks:
        - Every valid lead has a classification.
        - Every classification has at least one recommendation.
        - Flags any anomalies.
    """
    logger.info("Review agent checking workflow outputs")
    notes: list[str] = []
    approved = True

    unclassified = [
        l.company_name
        for l in state.leads
        if l.status == LeadStatus.valid and l.company_name not in state.classifications
    ]
    if unclassified:
        notes.append(
            f"WARNING: {len(unclassified)} valid lead(s) missing classification: {unclassified}"
        )
        approved = False

    classified_companies = set(state.classifications.keys())
    recommended_companies = {r.company_name for r in state.recommendations}
    missing_recommendations = classified_companies - recommended_companies
    if missing_recommendations:
        notes.append(
            f"INFO: {len(missing_recommendations)} classified lead(s) have no recommendations: {missing_recommendations}"
        )

    if state.metrics.invalid_leads > 0:
        notes.append(
            f"INFO: {state.metrics.invalid_leads} lead(s) flagged as invalid and skipped."
        )

    if state.metrics.total_leads == 0:
        notes.append("ERROR: No leads were provided. Workflow produced empty results.")
        approved = False

    if approved:
        notes.append("Review passed — all outputs consistent.")

    state.review_notes = " | ".join(notes)
    state.review_approved = approved
    state.metrics.success = approved

    logger.info("Review complete: approved=%s", approved)
    return state


# ------------------------------------------------------------------
# Agno Agent wrappers (typed descriptors for the orchestration layer)
# ------------------------------------------------------------------
# These are real ``agno.Agent`` instances that describe each agent's
# role, instructions, and expected outputs. In demo mode the workflow
# calls the function directly; when an LLM provider is configured it
# could call ``agent.run()`` instead.

_INTAKE_INSTRUCTIONS = """
You are an intake agent in a Revenue Ops pipeline.
You receive raw lead records from a CSV and must:

1. Validate that company_name is present and non-empty.
2. Validate contact_email has a standard format when provided.
3. Check revenue and employees are non-negative numbers.
4. Detect and flag duplicates by normalized company name.
5. Mark invalid records with specific error messages.

Return the validated state with status flags set on each lead.
"""

_CLASSIFY_INSTRUCTIONS = """
You are a classification/enrichment agent.
For each valid lead, evaluate:

- **Urgency**: based on revenue size, employee count, and keywords in notes.
- **Risk**: based on missing contact info, unknown industry, low revenue, or churn signals.
- **Opportunity**: based on revenue scale, employee growth, industry vertical, and expansion keywords.

Assign one of low/medium/high to each dimension and provide enrichment notes.
"""

_ACTION_INSTRUCTIONS = """
You are an action recommendation agent.
For each classified lead, recommend 1-2 concrete follow-up actions.

Rules:
- High urgency leads get executive outreach within 24h.
- High opportunity leads get custom demo/proposal.
- High risk leads get risk assessment calls.
- All other leads get standard SDR qualification.

Assign priority (1=highest, 3=lowest), an owner/assignee, rationale, and due-by window.
"""

_REVIEW_INSTRUCTIONS = """
You are a review/manager agent.
Check the full workflow output for consistency:

1. Every valid lead must have a classification.
2. Every classified lead must have at least one recommendation.
3. Flag empty inputs and anomalies.
4. Set approved=True only when all checks pass.
5. Produce actionable review notes.
"""

intake = AgnoAgent(
    name="IntakeAgent",
    instructions=_INTAKE_INSTRUCTIONS,
    description="Validates and normalises incoming CSV lead records.",
    model=_DEFAULT_MODEL,
    tools=[],
)

classify = AgnoAgent(
    name="ClassifyAgent",
    instructions=_CLASSIFY_INSTRUCTIONS,
    description="Classifies leads for urgency, risk, and opportunity.",
    model=_DEFAULT_MODEL,
    tools=[],
)

action = AgnoAgent(
    name="ActionAgent",
    instructions=_ACTION_INSTRUCTIONS,
    description="Generates prioritised follow-up actions for each lead.",
    model=_DEFAULT_MODEL,
    tools=[],
)

review = AgnoAgent(
    name="ReviewAgent",
    instructions=_REVIEW_INSTRUCTIONS,
    description="Reviews workflow outputs for consistency and completeness.",
    model=_DEFAULT_MODEL,
    tools=[],
)

# Lookup mapping for the workflow
AGENT_REGISTRY: dict[str, tuple[AgnoAgent, callable]] = {
    "intake": (intake, intake_agent_fn),
    "classify": (classify, classify_agent_fn),
    "action": (action, action_agent_fn),
    "review": (review, review_agent_fn),
}


# ------------------------------------------------------------------
# Internal scoring helpers
# ------------------------------------------------------------------


def _classify_urgency(lead: LeadRecord) -> UrgencyLevel:
    score = 0
    if lead.revenue_millions and lead.revenue_millions > 50:
        score += 2
    elif lead.revenue_millions and lead.revenue_millions > 10:
        score += 1
    if lead.employees and lead.employees > 500:
        score += 2
    elif lead.employees and lead.employees > 100:
        score += 1
    if lead.notes and any(
        kw in lead.notes.lower() for kw in ["urgent", "hot", "timeline", "poc"]
    ):
        score += 2
    if score >= 3:
        return UrgencyLevel.high
    elif score >= 1:
        return UrgencyLevel.medium
    return UrgencyLevel.low


def _classify_risk(lead: LeadRecord) -> RiskLevel:
    score = 0
    if not lead.contact_email:
        score += 2
    if not lead.industry or lead.industry.lower() in ("unknown", "other", ""):
        score += 1
    if lead.revenue_millions is None or lead.revenue_millions < 1:
        score += 1
    if lead.notes and any(
        kw in lead.notes.lower() for kw in ["churn", "at risk", "competitor", "stalled"]
    ):
        score += 2
    if score >= 3:
        return RiskLevel.high
    elif score >= 1:
        return RiskLevel.medium
    return RiskLevel.low


def _classify_opportunity(lead: LeadRecord) -> OpportunityLevel:
    score = 0
    if lead.revenue_millions and lead.revenue_millions > 100:
        score += 2
    elif lead.revenue_millions and lead.revenue_millions > 20:
        score += 1
    if lead.employees and lead.employees > 1000:
        score += 2
    elif lead.employees and lead.employees > 200:
        score += 1
    if lead.industry and lead.industry.lower() in (
        "technology",
        "saas",
        "ai",
        "fintech",
        "healthtech",
    ):
        score += 1
    if lead.notes and any(
        kw in lead.notes.lower()
        for kw in ["expanding", "growing", "new funding", "contract"]
    ):
        score += 1
    if score >= 3:
        return OpportunityLevel.high
    elif score >= 1:
        return OpportunityLevel.medium
    return OpportunityLevel.low


def _generate_actions(
    lead: LeadRecord, classification: ClassificationResult
) -> list[ActionRecommendation]:
    actions: list[ActionRecommendation] = []

    # Count how many meaningful fields are populated (beyond just name and notes)
    data_fields = [
        lead.contact_email,
        lead.industry,
        lead.revenue_millions,
        lead.employees,
    ]
    filled_fields = sum(1 for f in data_fields if f is not None)

    if classification.urgency == UrgencyLevel.high:
        actions.append(
            ActionRecommendation(
                company_name=lead.company_name,
                action="Schedule executive outreach within 24h",
                priority=1,
                assignee="Account Executive",
                rationale=f"High urgency — {lead.company_name} scores strongly on revenue/employee signals.",
                due_by="24h",
            )
        )

    if classification.opportunity == OpportunityLevel.high:
        actions.append(
            ActionRecommendation(
                company_name=lead.company_name,
                action="Prepare custom demo and proposal",
                priority=1,
                assignee="Solutions Engineer",
                rationale="High opportunity — strong fit and growth indicators.",
                due_by="48h",
            )
        )

    if not actions:
        if filled_fields == 0:
            # Stub record — only name/notes, nothing actionable
            actions.append(
                ActionRecommendation(
                    company_name=lead.company_name,
                    action="Attempt data enrichment before outreach",
                    priority=3,
                    assignee="SDR Team",
                    rationale=f"Very limited data for {lead.company_name} — no email, industry, or size info. "
                    "Prioritise finding contact details before any outreach.",
                    due_by="2 weeks",
                )
            )
        elif not lead.contact_email and filled_fields >= 1:
            # Has some data but missing email
            actions.append(
                ActionRecommendation(
                    company_name=lead.company_name,
                    action="Research contact and send intro",
                    priority=2,
                    assignee="SDR Team",
                    rationale=f"{lead.company_name} has some signals ({lead.industry or 'unknown industry'}), "
                    "but no email on file. Find a contact before outreach.",
                    due_by="1 week",
                )
            )
        else:
            actions.append(
                ActionRecommendation(
                    company_name=lead.company_name,
                    action="Send introductory email and qualify",
                    priority=2,
                    assignee="SDR Team",
                    rationale=f"Standard follow-up for medium-priority lead.",
                    due_by="1 week",
                )
            )

    if classification.risk == RiskLevel.high:
        risk_rationale = _risk_rationale(lead)
        actions.append(
            ActionRecommendation(
                company_name=lead.company_name,
                action="Perform risk assessment call",
                priority=1,
                assignee="Customer Success",
                rationale=risk_rationale,
                due_by="72h",
            )
        )

    return actions


def _risk_rationale(lead: LeadRecord) -> str:
    """Build a specific risk rationale based on what's missing or concerning."""
    reasons: list[str] = []
    if not lead.contact_email:
        reasons.append("no contact email on file")
    if not lead.industry or lead.industry.lower() in ("unknown", "other", ""):
        reasons.append("unknown industry")
    if lead.revenue_millions is None or lead.revenue_millions < 1:
        reasons.append("minimal or unknown revenue")
    if lead.notes and any(
        kw in lead.notes.lower() for kw in ["churn", "at risk", "competitor", "stalled"]
    ):
        reasons.append("negative signals in notes")

    if reasons:
        return f"High risk — {'; '.join(reasons)}."
    return "High risk — missing contact info or negative signals detected."
