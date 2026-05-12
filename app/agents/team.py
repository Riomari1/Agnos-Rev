"""
Mock agent implementations for the Revenue Ops Copilot.

Each agent is a standalone function that operates on WorkflowState.
In production, these would be Agno Agent instances backed by an LLM.
Here they use deterministic rule-based logic for demo reliability.
"""

from __future__ import annotations

import logging
import re

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


def intake_agent(state: WorkflowState) -> WorkflowState:
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


def classify_agent(state: WorkflowState) -> WorkflowState:
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


def _classify_urgency(lead: LeadRecord) -> UrgencyLevel:
    """High urgency: large revenue, many employees, or known high-value signals."""
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
    """Higher risk: no contact, small revenue, unknown industry, or negative notes."""
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
    """Opportunity: high revenue, growing industries, good engagement signals."""
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


def action_agent(state: WorkflowState) -> WorkflowState:
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

    # Sort by priority (1 = highest), then by company name
    state.recommendations.sort(key=lambda r: (r.priority, r.company_name))

    logger.info("Generated %d recommendation(s)", len(state.recommendations))
    return state


def _generate_actions(
    lead: LeadRecord, classification: ClassificationResult
) -> list[ActionRecommendation]:
    """Create 1-2 action items per lead based on its classification."""
    actions: list[ActionRecommendation] = []

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
                rationale=f"High opportunity — strong fit and growth indicators.",
                due_by="48h",
            )
        )

    # Default action for medium/low leads
    if not actions:
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
        actions.append(
            ActionRecommendation(
                company_name=lead.company_name,
                action="Perform risk assessment call",
                priority=1,
                assignee="Customer Success",
                rationale=f"High risk — missing contact info or negative signals detected.",
                due_by="72h",
            )
        )

    return actions


def review_agent(state: WorkflowState) -> WorkflowState:
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
