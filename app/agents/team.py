"""
Agent implementations for the Revenue Ops Copilot.

Every agent calls DeepSeek via ``agent.run()`` with typed tool functions
for structured output.  No rule-based fallback — AI-only pipeline.
"""

from __future__ import annotations

import json
import logging
import os

from agno.agent import Agent as AgnoAgent
from agno.models.deepseek import DeepSeek

_DEFAULT_MODEL = DeepSeek(
    id=os.getenv("LLM_MODEL", "deepseek-chat"),
    api_key=os.getenv("DEEPSEEK_API_KEY"),
)

from app.models.schemas import (
    ActionRecommendation,
    ClassificationResult,
    LeadRecord,
    LeadStatus,
    WorkflowState,
)
from app.tools.data_quality import DataQualityTool
from app.tools.follow_up_sla import FollowUpSLATool
from app.tools.run_workflow import RunWorkflowTool
from app.tools.structured_outputs import (
    ActionTools,
    ClassifyTools,
    IntakeTools,
    ReviewTools,
)

logger = logging.getLogger("agents")


# ------------------------------------------------------------------
# Intake agent
# ------------------------------------------------------------------


def intake_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Intake agent processing %d raw lead(s)", len(state.leads))
    results: list[dict] = []
    tools = IntakeTools(results)

    leads_json = json.dumps(
        [
            {
                k: v
                for k, v in l.model_dump().items()
                if k not in ("status", "validation_errors")
            }
            for l in state.leads
        ],
        indent=2,
    )

    prompt = (
        "For each lead below, call the appropriate tool:\n"
        "- flag_valid(...) for clean records\n"
        "- flag_invalid(...) if company_name is missing or email is malformed\n"
        "- flag_duplicate(...) for duplicate company names (case-insensitive)\n"
        "Process EVERY lead. Use 'UNKNOWN' as company_name for missing ones.\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for intake (%d leads)", len(state.leads))
    _run_with_tools(intake, tools, prompt)

    state.leads = [LeadRecord(**r) for r in results]
    state.metrics.total_leads = len(state.leads)
    state.metrics.valid_leads = sum(
        1 for l in state.leads if l.status == LeadStatus.valid
    )
    state.metrics.invalid_leads = sum(
        1 for l in state.leads if l.status == LeadStatus.invalid
    )
    logger.info(
        "Intake: %d valid, %d invalid, %d total",
        state.metrics.valid_leads,
        state.metrics.invalid_leads,
        state.metrics.total_leads,
    )
    return state


# ------------------------------------------------------------------
# Classify agent
# ------------------------------------------------------------------


def classify_agent_fn(state: WorkflowState) -> WorkflowState:
    valid = [l for l in state.leads if l.status == LeadStatus.valid]
    logger.info("Classify agent processing %d valid lead(s)", len(valid))

    results: list[dict] = []
    tools = ClassifyTools(results)

    leads_json = json.dumps(
        [
            {
                "company_name": l.company_name,
                "industry": l.industry or "Unknown",
                "revenue_millions": l.revenue_millions,
                "employees": l.employees,
                "notes": l.notes or "",
            }
            for l in valid
        ],
        indent=2,
    )

    prompt = (
        "For each lead below, call classify_lead(...) ONCE per lead.\n\n"
        "Scoring:\n"
        "- urgency: high if revenue>50M or employees>500 or notes mention urgent/hot/timeline/POC\n"
        "- risk: high if missing contact info, unknown industry, revenue<1M, or notes mention churn/competitor/stalled\n"
        "- opportunity: high if revenue>100M, employees>1000, industry is tech/SaaS/AI/fintech/healthtech, "
        "or notes mention expanding/growing/new funding/contract\n"
        "- confidence: 0.0-1.0 (0.85 if clear, lower if ambiguous)\n"
        "- enrichment_notes: one sentence on what drove the scores\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for classification (%d leads)", len(valid))
    _run_with_tools(classify, tools, prompt)

    for r in results:
        state.classifications[r["company_name"]] = ClassificationResult(**r)
    logger.info("Classification: %d leads", len(results))
    return state


# ------------------------------------------------------------------
# Action agent
# ------------------------------------------------------------------


def action_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Action agent generating recommendations")

    results: list[dict] = []
    tools = ActionTools(results)

    items = []
    for lead in state.leads:
        if lead.status == LeadStatus.invalid:
            continue
        c = state.classifications.get(lead.company_name)
        if not c:
            continue
        items.append(
            {
                "company_name": lead.company_name,
                "industry": lead.industry or "Unknown",
                "revenue_millions": lead.revenue_millions,
                "employees": lead.employees,
                "urgency": c.urgency.value,
                "risk": c.risk.value,
                "opportunity": c.opportunity.value,
            }
        )
    if not items:
        return state

    prompt = (
        "For each lead below, call add_recommendation(...) 1-2 times.\n\n"
        "Rules:\n"
        "- High urgency -> executive outreach, priority=1, assignee='Account Executive', due_by='24h'\n"
        "- High opportunity -> custom demo/proposal, priority=1, assignee='Solutions Engineer', due_by='48h'\n"
        "- High risk -> risk assessment call, priority=1, assignee='Customer Success', due_by='72h'\n"
        "- Medium -> SDR qualification, priority=2, assignee='SDR Team', due_by='1 week'\n"
        "- Low signal -> data enrichment first, priority=3, assignee='SDR Team', due_by='2 weeks'\n\n"
        f"Leads:\n{json.dumps(items, indent=2)}"
    )

    logger.info("  Calling DeepSeek for action generation")
    _run_with_tools(action, tools, prompt)

    state.recommendations = sorted(
        [ActionRecommendation(**r) for r in results],
        key=lambda r: (r.priority, r.company_name),
    )
    logger.info("Action generation: %d recommendations", len(state.recommendations))
    return state


# ------------------------------------------------------------------
# Review agent
# ------------------------------------------------------------------


def review_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Review agent checking workflow outputs")

    result: dict = {}
    tools = ReviewTools(result)

    valid_names = [l.company_name for l in state.leads if l.status == LeadStatus.valid]
    classified_names = list(state.classifications.keys())
    recommended_names = list({r.company_name for r in state.recommendations})

    prompt = (
        "Call submit_review(...) with your verdict.\n\n"
        f"Total leads: {state.metrics.total_leads}\n"
        f"Valid leads: {state.metrics.valid_leads}\n"
        f"Invalid leads: {state.metrics.invalid_leads}\n"
        f"Valid names: {json.dumps(valid_names)}\n"
        f"Classified names: {json.dumps(classified_names)}\n"
        f"Leads with recs: {json.dumps(recommended_names)}\n"
        f"Total recommendations: {len(state.recommendations)}\n\n"
        "Checks:\n"
        "1. Every valid lead must have a classification\n"
        "2. Every classified lead must have at least one recommendation\n"
        "3. 0 leads = automatic rejection\n"
        "4. Invalid leads being skipped is fine - note it, don't reject\n"
    )

    logger.info("  Calling DeepSeek for review")
    _run_with_tools(review, tools, prompt)

    state.review_notes = result.get("notes", "No review notes")
    state.review_approved = result.get("approved", False)
    state.metrics.success = state.review_approved
    logger.info("Review: approved=%s", state.review_approved)
    return state


# ------------------------------------------------------------------
# Tool helper
# ------------------------------------------------------------------


def _run_with_tools(agent: AgnoAgent, tools: Toolkit, prompt: str) -> None:
    """Temporarily add *tools* to *agent*, call agent.run(prompt), then remove."""
    from agno.tools import Toolkit as Tk

    agent.tools = list(agent.tools or []) + [tools]
    try:
        agent.run(input=prompt)
    finally:
        agent.tools = [t for t in agent.tools if t is not tools]


# ------------------------------------------------------------------
# Agno Agent wrappers
# ------------------------------------------------------------------

_INTAKE_INSTRUCTIONS = """
You are an intake agent. Use the provided tools to validate every lead.
Call flag_valid/flag_invalid/flag_duplicate for each lead.
"""

_CLASSIFY_INSTRUCTIONS = """
You are a classification agent. Use classify_lead() to score each lead
by urgency, risk, and opportunity. Call it ONCE per lead.
"""

_ACTION_INSTRUCTIONS = """
You are an action recommendation agent. Use add_recommendation() to
generate 1-2 follow-up actions per classified lead.
"""

_REVIEW_INSTRUCTIONS = """
You are a review agent. Use submit_review() to deliver your verdict
after checking all leads have classifications and recommendations.
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

AGENT_REGISTRY: dict[str, tuple[AgnoAgent, callable]] = {
    "intake": (intake, intake_agent_fn),
    "classify": (classify, classify_agent_fn),
    "action": (action, action_agent_fn),
    "review": (review, review_agent_fn),
}
