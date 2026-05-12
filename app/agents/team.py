"""
Agent implementations for the Revenue Ops Copilot.

Each agent exists in two forms:
  1. An ``agno.Agent`` instance with name, instructions, model, and tools.
  2. A pipeline function that the orchestrator calls directly.

When ``DEEPSEEK_API_KEY`` is set, classify/action/review agents call
``agent.run()`` with ``output_schema`` for structured LLM reasoning.
When no API key is present, they fall back to deterministic rule-based
logic.  IntakeAgent always uses rules (validation is deterministic).
"""

from __future__ import annotations

import json
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
    ActionAgentOutput,
    ActionRecommendation,
    ClassificationResult,
    ClassifyAgentOutput,
    IntakeAgentOutput,
    LeadRecord,
    LeadStatus,
    OpportunityLevel,
    ReviewAgentOutput,
    RiskLevel,
    UrgencyLevel,
    WorkflowState,
)
from app.tools.data_quality import DataQualityTool
from app.tools.follow_up_sla import FollowUpSLATool
from app.tools.run_workflow import RunWorkflowTool

logger = logging.getLogger("agents")


def _llm_available() -> bool:
    """True if DeepSeek API key is configured and usable."""
    return bool(os.getenv("DEEPSEEK_API_KEY"))


# ------------------------------------------------------------------
# Intake agent (LLM-powered with rule-based fallback)
# ------------------------------------------------------------------


def intake_agent_fn(state: WorkflowState) -> WorkflowState:
    """Validate and normalise raw lead records.

    When DEEPSEEK_API_KEY is set: calls the LLM for validation + dedup.
    Otherwise: uses deterministic regex/rule-based checks.
    """
    logger.info("Intake agent processing %d raw lead(s)", len(state.leads))

    if _llm_available():
        try:
            return _intake_llm(state)
        except Exception as exc:
            logger.warning("LLM intake failed (%s), falling back to rules", exc)

    return _intake_rules(state)


def _intake_llm(state: WorkflowState) -> WorkflowState:
    """Use the LLM agent to validate and deduplicate leads."""
    leads_json = json.dumps(
        [
            {
                "company_name": l.company_name,
                "contact_email": l.contact_email,
                "industry": l.industry,
                "revenue_millions": l.revenue_millions,
                "employees": l.employees,
                "lead_source": l.lead_source,
                "notes": l.notes,
            }
            for l in state.leads
        ],
        indent=2,
    )

    prompt = (
        "Validate and normalise these lead records.\n\n"
        "For each lead:\n"
        "1. Set status to 'invalid' if company_name is missing/empty. "
        "Use 'UNKNOWN' as placeholder name for these.\n"
        "2. Validate contact_email against standard email format "
        "(user@domain.tld). Flag invalid emails in validation_errors.\n"
        "3. Check revenue_millions >= 0 and employees >= 0. "
        "Flag negative values.\n"
        "4. Detect duplicate company names (case-insensitive). "
        "Mark duplicates with status='duplicate'. "
        "Only the first occurrence should be 'valid'.\n"
        "5. Add specific, human-readable error messages to "
        "validation_errors for each issue found.\n"
        "6. Set status='valid' for clean records.\n\n"
        f"Leads to validate:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for intake validation (%d leads)", len(state.leads))
    response = intake.run(
        input=prompt,
        output_schema=IntakeAgentOutput,
    )

    output: IntakeAgentOutput = response.content
    state.leads = output.validated_leads
    state.metrics.total_leads = len(output.validated_leads)
    state.metrics.valid_leads = sum(
        1 for l in output.validated_leads if l.status == LeadStatus.valid
    )
    state.metrics.invalid_leads = sum(
        1 for l in output.validated_leads if l.status == LeadStatus.invalid
    )

    logger.info(
        "LLM intake complete: %d valid, %d invalid, %d total",
        state.metrics.valid_leads,
        state.metrics.invalid_leads,
        state.metrics.total_leads,
    )
    return state


def _intake_rules(state: WorkflowState) -> WorkflowState:
    """Deterministic rule-based intake fallback."""
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
        "Rule-based intake complete: %d valid, %d invalid, %d total",
        state.metrics.valid_leads,
        state.metrics.invalid_leads,
        state.metrics.total_leads,
    )
    return state


# ------------------------------------------------------------------
# Classify agent (LLM-powered with rule-based fallback)
# ------------------------------------------------------------------


def classify_agent_fn(state: WorkflowState) -> WorkflowState:
    """Classify each valid lead for urgency, risk, and opportunity.

    When DEEPSEEK_API_KEY is set: calls the LLM with structured output.
    Otherwise: uses deterministic scoring rules.
    """
    valid_leads = [l for l in state.leads if l.status == LeadStatus.valid]
    logger.info("Classify agent processing %d valid lead(s)", len(valid_leads))

    if _llm_available():
        try:
            return _classify_llm(state, valid_leads)
        except Exception as exc:
            logger.warning("LLM classification failed (%s), falling back to rules", exc)

    return _classify_rules(state, valid_leads)


def _classify_llm(state: WorkflowState, valid_leads: list[LeadRecord]) -> WorkflowState:
    """Use the LLM agent to classify leads with structured output."""
    leads_json = json.dumps(
        [
            {
                "company_name": l.company_name,
                "industry": l.industry or "Unknown",
                "revenue_millions": l.revenue_millions,
                "employees": l.employees,
                "notes": l.notes or "",
            }
            for l in valid_leads
        ],
        indent=2,
    )

    prompt = (
        "Classify each lead below by urgency, risk, and opportunity.\n\n"
        "Scoring guidelines:\n"
        "- Urgency: driven by revenue size (>50M = high signal), "
        "employee count (>500 = high signal), and urgency keywords "
        "in notes (urgent, hot, timeline, POC).\n"
        "- Risk: driven by missing contact info, unknown industry, "
        "low revenue (<1M), and churn signals in notes "
        "(churn, at risk, competitor, stalled).\n"
        "- Opportunity: driven by revenue scale (>100M), "
        "employee growth (>1000), industry fit "
        "(technology, SaaS, AI, fintech, healthtech), "
        "and growth keywords (expanding, growing, new funding, contract).\n"
        "- Confidence: 0.0-1.0 reflecting how clear the signals are.\n"
        "- enrichment_notes: brief summary of what drove the scores.\n\n"
        f"Leads to classify:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for classification (%d leads)", len(valid_leads))
    response = classify.run(
        input=prompt,
        output_schema=ClassifyAgentOutput,
    )

    output: ClassifyAgentOutput = response.content
    for c in output.classifications:
        state.classifications[c.company_name] = c

    logger.info(
        "LLM classification complete: %d lead(s) classified",
        len(output.classifications),
    )
    return state


def _classify_rules(
    state: WorkflowState, valid_leads: list[LeadRecord]
) -> WorkflowState:
    """Deterministic rule-based classification fallback."""
    for lead in valid_leads:
        classification = ClassificationResult(
            company_name=lead.company_name,
            industry=lead.industry or "Unknown",
            urgency=_classify_urgency(lead),
            risk=_classify_risk(lead),
            opportunity=_classify_opportunity(lead),
            confidence=0.85,
            enrichment_notes=(
                f"Revenue: ${lead.revenue_millions or 0}M | "
                f"Employees: {lead.employees or 0}"
            ),
        )
        state.classifications[lead.company_name] = classification

    logger.info("Rule-based classification complete for %d lead(s)", len(valid_leads))
    return state


# ------------------------------------------------------------------
# Action agent (LLM-powered with rule-based fallback)
# ------------------------------------------------------------------


def action_agent_fn(state: WorkflowState) -> WorkflowState:
    """Generate follow-up action recommendations based on classifications.

    When DEEPSEEK_API_KEY is set: calls the LLM with structured output.
    Otherwise: uses deterministic action generation rules.
    """
    logger.info("Action agent generating recommendations")

    if _llm_available():
        try:
            return _action_llm(state)
        except Exception as exc:
            logger.warning(
                "LLM action generation failed (%s), falling back to rules", exc
            )

    return _action_rules(state)


def _action_llm(state: WorkflowState) -> WorkflowState:
    """Use the LLM agent to generate follow-up actions with structured output."""
    leads_with_classifications = []
    for lead in state.leads:
        if lead.status == LeadStatus.invalid:
            continue
        c = state.classifications.get(lead.company_name)
        if not c:
            continue
        leads_with_classifications.append(
            {
                "company_name": lead.company_name,
                "industry": lead.industry or "Unknown",
                "contact_email": lead.contact_email,
                "revenue_millions": lead.revenue_millions,
                "employees": lead.employees,
                "urgency": c.urgency.value,
                "risk": c.risk.value,
                "opportunity": c.opportunity.value,
            }
        )

    if not leads_with_classifications:
        return state

    prompt = (
        "Generate 1-2 concrete follow-up actions per lead.\n\n"
        "Rules:\n"
        "- High urgency -> executive outreach within 24h, priority=1\n"
        "- High opportunity -> custom demo/proposal, priority=1\n"
        "- High risk -> risk assessment call, priority=1\n"
        "- Medium priority -> SDR qualification, priority=2\n"
        "- Low signal / stub records -> data enrichment first, priority=3\n"
        "- Assignee: 'Account Executive', 'Solutions Engineer', "
        "'Customer Success', or 'SDR Team'\n"
        "- due_by: '24h', '48h', '72h', '1 week', or '2 weeks'\n"
        "- rationale: one sentence explaining why this action was chosen.\n\n"
        f"Classified leads:\n{json.dumps(leads_with_classifications, indent=2)}"
    )

    logger.info("  Calling DeepSeek for action generation")
    response = action.run(
        input=prompt,
        output_schema=ActionAgentOutput,
    )

    output: ActionAgentOutput = response.content
    state.recommendations = sorted(
        output.recommendations, key=lambda r: (r.priority, r.company_name)
    )

    logger.info(
        "LLM action generation complete: %d recommendation(s)",
        len(state.recommendations),
    )
    return state


def _action_rules(state: WorkflowState) -> WorkflowState:
    """Deterministic rule-based action generation fallback."""
    for lead in state.leads:
        if lead.status == LeadStatus.invalid:
            continue
        classification = state.classifications.get(lead.company_name)
        if not classification:
            continue
        state.recommendations.extend(_generate_actions(lead, classification))

    state.recommendations.sort(key=lambda r: (r.priority, r.company_name))
    logger.info(
        "Rule-based action generation: %d recommendation(s)",
        len(state.recommendations),
    )
    return state


# ------------------------------------------------------------------
# Review agent (LLM-powered with rule-based fallback)
# ------------------------------------------------------------------


def review_agent_fn(state: WorkflowState) -> WorkflowState:
    """Review the full output for consistency and completeness.

    When DEEPSEEK_API_KEY is set: calls the LLM for nuanced review.
    Otherwise: uses deterministic consistency checks.
    """
    logger.info("Review agent checking workflow outputs")

    if _llm_available():
        try:
            return _review_llm(state)
        except Exception as exc:
            logger.warning("LLM review failed (%s), falling back to rules", exc)

    return _review_rules(state)


def _review_llm(state: WorkflowState) -> WorkflowState:
    """Use the LLM agent to review workflow consistency."""
    valid_names = [l.company_name for l in state.leads if l.status == LeadStatus.valid]
    classified_names = list(state.classifications.keys())
    recommended_names = list({r.company_name for r in state.recommendations})

    prompt = (
        "Review this Revenue Ops workflow output for consistency.\n\n"
        f"Total leads: {state.metrics.total_leads}\n"
        f"Valid leads: {state.metrics.valid_leads}\n"
        f"Invalid leads: {state.metrics.invalid_leads}\n"
        f"Valid lead names: {json.dumps(valid_names)}\n"
        f"Classified lead names: {json.dumps(classified_names)}\n"
        f"Leads with recommendations: {json.dumps(recommended_names)}\n"
        f"Total recommendations: {len(state.recommendations)}\n\n"
        "Checks:\n"
        "1. Every valid lead must have a classification.\n"
        "2. Every classified lead must have at least one recommendation.\n"
        "3. An empty input (0 leads) is an automatic rejection.\n"
        "4. Invalid leads being skipped is fine -- note it but don't reject.\n\n"
        "Set approved=True only when all checks pass. "
        "Write concise, actionable review notes."
    )

    logger.info("  Calling DeepSeek for review")
    response = review.run(
        input=prompt,
        output_schema=ReviewAgentOutput,
    )

    output: ReviewAgentOutput = response.content
    state.review_notes = output.notes
    state.review_approved = output.approved
    state.metrics.success = output.approved

    logger.info("LLM review complete: approved=%s", output.approved)
    return state


def _review_rules(state: WorkflowState) -> WorkflowState:
    """Deterministic rule-based consistency checks fallback."""
    notes: list[str] = []
    approved = True

    unclassified = [
        l.company_name
        for l in state.leads
        if l.status == LeadStatus.valid and l.company_name not in state.classifications
    ]
    if unclassified:
        notes.append(
            f"WARNING: {len(unclassified)} valid lead(s) missing "
            f"classification: {unclassified}"
        )
        approved = False

    classified_companies = set(state.classifications.keys())
    recommended_companies = {r.company_name for r in state.recommendations}
    missing_recs = classified_companies - recommended_companies
    if missing_recs:
        notes.append(
            f"WARNING: {len(missing_recs)} classified lead(s) need "
            f"recommendations: {missing_recs}"
        )
        approved = False

    if state.metrics.invalid_leads > 0:
        notes.append(
            f"INFO: {state.metrics.invalid_leads} lead(s) flagged as "
            f"invalid and skipped."
        )

    if state.metrics.total_leads == 0:
        notes.append("ERROR: No leads were provided. Workflow produced empty results.")
        approved = False

    if approved:
        notes.append("Review passed -- all outputs consistent.")

    state.review_notes = " | ".join(notes)
    state.review_approved = approved
    state.metrics.success = approved

    logger.info("Rule-based review complete: approved=%s", approved)
    return state


# ------------------------------------------------------------------
# Agno Agent wrappers (real instances with role descriptions)
# ------------------------------------------------------------------

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

Assign one of low/medium/high to each dimension. Set confidence (0.0-1.0)
reflecting how clear the signals are. Write enrichment_notes summarising
what drove the scores. Return a JSON object matching ClassifyAgentOutput.
"""

_ACTION_INSTRUCTIONS = """
You are an action recommendation agent.
For each classified lead, recommend 1-2 concrete follow-up actions.

Rules:
- High urgency leads get executive outreach within 24h.
- High opportunity leads get custom demo/proposal.
- High risk leads get risk assessment calls.
- All other leads get standard SDR qualification.

Assign priority (1=highest, 3=lowest), an owner/assignee, rationale,
and due-by window. Return a JSON object matching ActionAgentOutput.
"""

_REVIEW_INSTRUCTIONS = """
You are a review/manager agent.
Check the full workflow output for consistency:

1. Every valid lead must have a classification.
2. Every classified lead must have at least one recommendation.
3. Flag empty inputs and anomalies.
4. Set approved=True only when all checks pass.
5. Produce actionable review notes.

Return a JSON object matching ReviewAgentOutput.
"""

intake = AgnoAgent(
    name="IntakeAgent",
    instructions=_INTAKE_INSTRUCTIONS,
    description="Validates and normalises incoming CSV lead records.",
    model=_DEFAULT_MODEL,
    tools=[DataQualityTool(), RunWorkflowTool()],
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
    tools=[FollowUpSLATool()],
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
# Rule-based scoring helpers (fallback when no API key)
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
                rationale=(
                    f"High urgency -- {lead.company_name} scores strongly "
                    "on revenue/employee signals."
                ),
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
                rationale=("High opportunity -- strong fit and growth indicators."),
                due_by="48h",
            )
        )

    if not actions:
        if filled_fields == 0:
            actions.append(
                ActionRecommendation(
                    company_name=lead.company_name,
                    action="Attempt data enrichment before outreach",
                    priority=3,
                    assignee="SDR Team",
                    rationale=(
                        f"Very limited data for {lead.company_name} -- "
                        "no email, industry, or size info. Prioritise "
                        "finding contact details before any outreach."
                    ),
                    due_by="2 weeks",
                )
            )
        elif not lead.contact_email and filled_fields >= 1:
            actions.append(
                ActionRecommendation(
                    company_name=lead.company_name,
                    action="Research contact and send intro",
                    priority=2,
                    assignee="SDR Team",
                    rationale=(
                        f"{lead.company_name} has some signals "
                        f"({lead.industry or 'unknown industry'}), "
                        "but no email on file. Find a contact before outreach."
                    ),
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
                    rationale="Standard follow-up for medium-priority lead.",
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
        return f"High risk -- {'; '.join(reasons)}."
    return "High risk -- missing contact info or negative signals detected."
