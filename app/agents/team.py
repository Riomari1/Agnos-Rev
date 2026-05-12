"""
Agent implementations for the Revenue Ops Copilot.

DeepSeek-backed Agno agents are the primary runtime.  The local runtime is an
explicit resilience path used when DeepSeek is not configured, the API call
fails, or tests need deterministic execution without network access.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

# Load .env immediately so DEEPSEEK_API_KEY is available regardless of how this
# module is imported (CLI, Streamlit, AgentOS, tests, etc.).
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()

from agno.agent import Agent as AgnoAgent
from agno.models.deepseek import DeepSeek
from agno.tools import Toolkit

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

AGENT_MODE_ENV = "REVENUE_OPS_AGENT_MODE"
VALID_AGENT_MODES = {"auto", "deepseek", "local"}


@dataclass(frozen=True)
class AgentSpec:
    """Shared metadata for Agno registration and workflow execution."""

    key: str
    name: str
    description: str
    instructions: str
    tool_factories: tuple[Callable[[], Toolkit], ...] = ()


_INTAKE_INSTRUCTIONS = (
    "You are an intake agent. Use the provided tools to validate every lead. "
    "Call flag_valid/flag_invalid/flag_duplicate for each lead."
)

_CLASSIFY_INSTRUCTIONS = (
    "You are a classification agent. Use classify_lead() to score each lead "
    "by urgency, risk, and opportunity. Call it once per valid lead."
)

_ACTION_INSTRUCTIONS = (
    "You are an action recommendation agent. Use add_recommendation() to "
    "generate one or two follow-up actions per classified lead."
)

_REVIEW_INSTRUCTIONS = (
    "You are a review agent. Use submit_review() after checking that every "
    "valid lead has a classification and at least one recommendation."
)


INTAKE_SPEC = AgentSpec(
    key="intake",
    name="IntakeAgent",
    description="Validates and normalises incoming CSV lead records.",
    instructions=_INTAKE_INSTRUCTIONS,
    tool_factories=(DataQualityTool, RunWorkflowTool),
)
CLASSIFY_SPEC = AgentSpec(
    key="classify",
    name="ClassifyAgent",
    description="Classifies leads for urgency, risk, and opportunity.",
    instructions=_CLASSIFY_INSTRUCTIONS,
)
ACTION_SPEC = AgentSpec(
    key="action",
    name="ActionAgent",
    description="Generates prioritised follow-up actions for each lead.",
    instructions=_ACTION_INSTRUCTIONS,
    tool_factories=(FollowUpSLATool,),
)
REVIEW_SPEC = AgentSpec(
    key="review",
    name="ReviewAgent",
    description="Reviews workflow outputs for consistency and completeness.",
    instructions=_REVIEW_INSTRUCTIONS,
)


def get_runtime_config() -> dict[str, str | bool]:
    """Return a small, UI-safe summary of the active agent runtime."""

    return {
        "mode": _agent_mode(),
        "model_id": os.getenv("LLM_MODEL", "deepseek-chat"),
        "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
    }


def _agent_mode() -> str:
    mode = os.getenv(AGENT_MODE_ENV, "auto").strip().lower()
    if mode not in VALID_AGENT_MODES:
        logger.warning(
            "Unknown %s=%r; falling back to auto mode", AGENT_MODE_ENV, mode
        )
        return "auto"
    return mode


def _should_call_deepseek(mode: str) -> bool:
    if mode == "local":
        return False
    if mode == "deepseek":
        if not os.getenv("DEEPSEEK_API_KEY"):
            logger.warning("DeepSeek mode requested but DEEPSEEK_API_KEY is not set")
        return True
    return bool(os.getenv("DEEPSEEK_API_KEY"))


def _build_model() -> DeepSeek:
    """Build a fresh DeepSeek model using the current environment."""

    return DeepSeek(
        id=os.getenv("LLM_MODEL", "deepseek-chat"),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
    )


def build_agno_agent(
    spec: AgentSpec,
    extra_tools: list[Toolkit] | None = None,
) -> AgnoAgent:
    """Create a new Agno agent from a shared spec."""

    tools: list[Toolkit] = [factory() for factory in spec.tool_factories]
    tools.extend(extra_tools or [])
    return AgnoAgent(
        name=spec.name,
        instructions=spec.instructions,
        description=spec.description,
        model=_build_model(),
        tools=tools,
    )


def build_agent_os_agents() -> list[AgnoAgent]:
    """Build the four agents shown in AgentOS."""

    return [
        build_agno_agent(INTAKE_SPEC),
        build_agno_agent(CLASSIFY_SPEC),
        build_agno_agent(ACTION_SPEC),
        build_agno_agent(REVIEW_SPEC),
    ]


# ------------------------------------------------------------------
# Intake agent
# ------------------------------------------------------------------


def intake_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Intake agent processing %d raw lead(s)", len(state.leads))
    results: list[dict] = []
    tools = IntakeTools(results)

    if not state.leads:
        state.metrics.total_leads = 0
        state.metrics.valid_leads = 0
        state.metrics.invalid_leads = 0
        state.metrics.agent_modes["intake"] = "skipped_empty"
        return state

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
        "- flag_duplicate(...) for duplicate company names, case-insensitive\n"
        "Process every lead. Use 'UNKNOWN' as company_name for missing ones.\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info("  Running intake runtime=%s (%d leads)", _agent_mode(), len(state.leads))
    mode = _run_with_tools(
        spec=INTAKE_SPEC,
        call_tools=tools,
        prompt=prompt,
        fallback=lambda: _local_intake(state.leads, results),
        output_ready=lambda: len(results) == len(state.leads),
    )
    state.metrics.agent_modes["intake"] = mode

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

    if not valid:
        state.metrics.agent_modes["classify"] = "skipped_no_valid_leads"
        return state

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
        "For each lead below, call classify_lead(...) once per lead.\n\n"
        "Scoring:\n"
        "- urgency: high if revenue > 50M, employees > 500, or notes mention urgent/hot/timeline/POC\n"
        "- risk: high if missing contact info, unknown industry, revenue < 1M, or notes mention churn/competitor/stalled\n"
        "- opportunity: high if revenue > 100M, employees > 1000, industry is tech/SaaS/AI/fintech/healthtech, "
        "or notes mention expanding/growing/new funding/contract\n"
        "- confidence: 0.0-1.0, higher when the evidence is explicit\n"
        "- enrichment_notes: one sentence on what drove the scores\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info(
        "  Running classification runtime=%s (%d leads)", _agent_mode(), len(valid)
    )
    mode = _run_with_tools(
        spec=CLASSIFY_SPEC,
        call_tools=tools,
        prompt=prompt,
        fallback=lambda: _local_classify(valid, results),
        output_ready=lambda: len({r.get("company_name") for r in results})
        == len({lead.company_name for lead in valid}),
    )
    state.metrics.agent_modes["classify"] = mode

    state.classifications.clear()
    for r in results:
        state.classifications[r["company_name"]] = ClassificationResult(**r)
    logger.info("Classification: %d leads", len(state.classifications))
    return state


# ------------------------------------------------------------------
# Action agent
# ------------------------------------------------------------------


def action_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Action agent generating recommendations")

    items = _classified_items(state)
    if not items:
        state.metrics.agent_modes["action"] = "skipped_no_classifications"
        state.recommendations = []
        return state

    results: list[dict] = []
    tools = ActionTools(results)

    prompt = (
        "For each lead below, call add_recommendation(...) one or two times.\n\n"
        "Rules:\n"
        "- High urgency -> executive outreach, priority=1, assignee='Account Executive', due_by='24h'\n"
        "- High opportunity -> custom demo/proposal, priority=1, assignee='Solutions Engineer', due_by='48h'\n"
        "- High risk -> risk assessment call, priority=1, assignee='Customer Success', due_by='72h'\n"
        "- Medium -> SDR qualification, priority=2, assignee='SDR Team', due_by='1 week'\n"
        "- Low signal -> data enrichment first, priority=3, assignee='SDR Team', due_by='2 weeks'\n\n"
        f"Leads:\n{json.dumps(items, indent=2)}"
    )

    logger.info("  Running action generation runtime=%s", _agent_mode())
    expected_names = {item["company_name"] for item in items}
    mode = _run_with_tools(
        spec=ACTION_SPEC,
        call_tools=tools,
        prompt=prompt,
        fallback=lambda: _local_actions(state, results),
        output_ready=lambda: expected_names.issubset(
            {r.get("company_name") for r in results}
        ),
    )
    state.metrics.agent_modes["action"] = mode

    state.recommendations = sorted(
        [ActionRecommendation(**r) for r in results],
        key=lambda r: (r.priority, r.company_name, r.action),
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
        "3. Zero leads is an automatic rejection\n"
        "4. Invalid leads being skipped is fine - note it, do not reject for that alone\n"
    )

    logger.info("  Running review runtime=%s", _agent_mode())
    mode = _run_with_tools(
        spec=REVIEW_SPEC,
        call_tools=tools,
        prompt=prompt,
        fallback=lambda: _local_review(state, result),
        output_ready=lambda: "approved" in result and "notes" in result,
    )
    state.metrics.agent_modes["review"] = mode

    state.review_notes = result.get("notes", "No review notes")
    state.review_approved = result.get("approved", False)
    state.metrics.success = state.review_approved
    logger.info("Review: approved=%s", state.review_approved)
    return state


# ------------------------------------------------------------------
# Runtime helper
# ------------------------------------------------------------------


def _run_with_tools(
    spec: AgentSpec,
    call_tools: Toolkit,
    prompt: str,
    fallback: Callable[[], None],
    output_ready: Callable[[], bool],
) -> str:
    """Run an Agno agent with tools, falling back if structured output is absent."""

    mode = _agent_mode()
    if _should_call_deepseek(mode):
        try:
            agent = build_agno_agent(spec, extra_tools=[call_tools])
            agent.run(input=prompt)
        except Exception as exc:
            logger.warning("%s DeepSeek call failed: %s", spec.name, exc)
        else:
            if output_ready():
                return "deepseek"
            logger.warning(
                "%s returned without complete structured tool output; using local fallback",
                spec.name,
            )

        fallback()
        return "local_fallback"

    fallback()
    return "local"


# ------------------------------------------------------------------
# Local deterministic resilience runtime
# ------------------------------------------------------------------


_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _local_intake(leads: list[LeadRecord], results: list[dict]) -> None:
    results.clear()
    seen: set[str] = set()

    for lead in leads:
        company = (lead.company_name or "").strip()
        key = company.lower()
        errors: list[str] = []

        if not company:
            errors.append("Missing company name")
            company = "UNKNOWN"
        if lead.contact_email and not _EMAIL_PATTERN.match(lead.contact_email.strip()):
            errors.append(f"Malformed email: {lead.contact_email}")

        record = {
            "company_name": company,
            "contact_email": lead.contact_email,
            "industry": lead.industry,
            "revenue_millions": lead.revenue_millions,
            "employees": lead.employees,
            "lead_source": lead.lead_source,
            "notes": lead.notes,
        }

        if errors:
            results.append(
                {
                    **record,
                    "status": LeadStatus.invalid.value,
                    "validation_errors": errors,
                }
            )
        elif key and key in seen:
            results.append(
                {
                    **record,
                    "status": LeadStatus.duplicate.value,
                    "validation_errors": [f"Duplicate company name: {company}"],
                }
            )
        else:
            if key:
                seen.add(key)
            results.append(
                {
                    **record,
                    "status": LeadStatus.valid.value,
                    "validation_errors": [],
                }
            )


def _local_classify(leads: list[LeadRecord], results: list[dict]) -> None:
    results.clear()
    for lead in leads:
        notes = (lead.notes or "").lower()
        industry = (lead.industry or "Unknown").lower()
        revenue = lead.revenue_millions or 0
        employees = lead.employees or 0

        urgency_signals = ["urgent", "hot", "timeline", "poc", "demo by", "procurement"]
        risk_signals = ["churn", "competitor", "stalled", "unhappy"]
        opportunity_signals = [
            "expanding",
            "growing",
            "new funding",
            "contract",
            "pilot",
            "partnership",
            "rollout",
        ]
        opportunity_industries = [
            "tech",
            "saas",
            "ai",
            "fintech",
            "healthtech",
            "cloud",
            "cybersecurity",
            "devops",
        ]

        urgency = UrgencyLevel.low
        if revenue > 50 or employees > 500 or any(s in notes for s in urgency_signals):
            urgency = UrgencyLevel.high
        elif revenue >= 10 or employees >= 100:
            urgency = UrgencyLevel.medium

        risk = RiskLevel.low
        if (
            not lead.contact_email
            or not lead.industry
            or 0 < revenue < 1
            or any(s in notes for s in risk_signals)
        ):
            risk = RiskLevel.high
        elif revenue < 10 or employees < 100:
            risk = RiskLevel.medium

        opportunity = OpportunityLevel.low
        if (
            revenue > 100
            or employees > 1000
            or any(s in industry for s in opportunity_industries)
            or any(s in notes for s in opportunity_signals)
        ):
            opportunity = OpportunityLevel.high
        elif revenue >= 20 or employees >= 250:
            opportunity = OpportunityLevel.medium

        strong_signal_count = sum(
            [
                urgency == UrgencyLevel.high,
                risk == RiskLevel.high,
                opportunity == OpportunityLevel.high,
            ]
        )
        confidence = 0.86 if strong_signal_count else 0.68
        notes_out = (
            f"Revenue {revenue:g}M, employees {employees}, industry "
            f"{lead.industry or 'Unknown'} drove the score."
        )

        results.append(
            {
                "company_name": lead.company_name,
                "industry": lead.industry or "Unknown",
                "urgency": urgency.value,
                "risk": risk.value,
                "opportunity": opportunity.value,
                "confidence": confidence,
                "enrichment_notes": notes_out,
            }
        )


def _classified_items(state: WorkflowState) -> list[dict]:
    items: list[dict] = []
    for lead in state.leads:
        if lead.status != LeadStatus.valid:
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
    return items


def _local_actions(state: WorkflowState, results: list[dict]) -> None:
    results.clear()
    sla = FollowUpSLATool()

    for item in _classified_items(state):
        company = item["company_name"]
        urgency = item["urgency"]
        risk = item["risk"]
        opportunity = item["opportunity"]
        due_by = sla.recommend_deadline(urgency, risk, opportunity)
        rationale = sla.recommend_priority_note(urgency, risk, opportunity)

        if urgency == UrgencyLevel.high.value:
            results.append(
                {
                    "company_name": company,
                    "action": "Schedule executive follow-up and confirm buying timeline",
                    "priority": 1,
                    "assignee": "Account Executive",
                    "rationale": rationale,
                    "due_by": due_by,
                }
            )
        elif risk == RiskLevel.high.value:
            results.append(
                {
                    "company_name": company,
                    "action": "Run risk assessment call and identify blockers",
                    "priority": 1,
                    "assignee": "Customer Success",
                    "rationale": rationale,
                    "due_by": due_by,
                }
            )
        elif opportunity == OpportunityLevel.high.value:
            results.append(
                {
                    "company_name": company,
                    "action": "Prepare tailored demo and ROI proposal",
                    "priority": 1,
                    "assignee": "Solutions Engineer",
                    "rationale": rationale,
                    "due_by": due_by,
                }
            )
        elif urgency == UrgencyLevel.medium.value or opportunity == OpportunityLevel.medium.value:
            results.append(
                {
                    "company_name": company,
                    "action": "Run SDR qualification and capture missing buying signals",
                    "priority": 2,
                    "assignee": "SDR Team",
                    "rationale": rationale,
                    "due_by": due_by,
                }
            )
        else:
            results.append(
                {
                    "company_name": company,
                    "action": "Enrich account data before outbound follow-up",
                    "priority": 3,
                    "assignee": "SDR Team",
                    "rationale": rationale,
                    "due_by": due_by,
                }
            )

        if (
            opportunity == OpportunityLevel.high.value
            and urgency == UrgencyLevel.high.value
        ):
            results.append(
                {
                    "company_name": company,
                    "action": "Build a custom demo or proposal for the active opportunity",
                    "priority": 1,
                    "assignee": "Solutions Engineer",
                    "rationale": "High urgency and high opportunity justify parallel sales engineering support.",
                    "due_by": "48h",
                }
            )


def _local_review(state: WorkflowState, result: dict) -> None:
    result.clear()
    valid_names = {l.company_name for l in state.leads if l.status == LeadStatus.valid}
    classified_names = set(state.classifications)
    recommended_names = {r.company_name for r in state.recommendations}

    if not state.leads:
        result["approved"] = False
        result["notes"] = "Rejected: no leads were loaded."
        return

    missing_classifications = sorted(valid_names - classified_names)
    missing_recommendations = sorted(classified_names - recommended_names)
    notes: list[str] = []

    if missing_classifications:
        notes.append(
            "Missing classifications for: " + ", ".join(missing_classifications)
        )
    if missing_recommendations:
        notes.append(
            "Missing recommendations for: " + ", ".join(missing_recommendations)
        )

    invalid_count = sum(1 for lead in state.leads if lead.status == LeadStatus.invalid)
    duplicate_count = sum(1 for lead in state.leads if lead.status == LeadStatus.duplicate)
    if invalid_count or duplicate_count:
        notes.append(
            f"Skipped {invalid_count} invalid and {duplicate_count} duplicate lead(s)."
        )

    result["approved"] = not missing_classifications and not missing_recommendations
    note_text = " ".join(notes)
    if result["approved"]:
        result["notes"] = "Approved: every valid lead has a classification and recommendation."
        if note_text:
            result["notes"] += f" {note_text}"
    else:
        result["notes"] = "Rejected: " + note_text


AGENT_REGISTRY: dict[str, dict[str, object]] = {
    "intake": {"spec": INTAKE_SPEC, "fn": intake_agent_fn},
    "classify": {"spec": CLASSIFY_SPEC, "fn": classify_agent_fn},
    "action": {"spec": ACTION_SPEC, "fn": action_agent_fn},
    "review": {"spec": REVIEW_SPEC, "fn": review_agent_fn},
}
