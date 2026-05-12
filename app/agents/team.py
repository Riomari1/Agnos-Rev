"""
Agent implementations for the Revenue Ops Copilot.

All workflow agents execute through Agno with the DeepSeek model. There is no
offline/rule-based fallback. DeepSeek's Agno tool-call loop currently
hangs, so workflow agents return JSON that is immediately validated into the
typed Pydantic models used by the rest of the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

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
    WorkflowState,
)
from app.tools.data_quality import DataQualityTool
from app.tools.follow_up_sla import FollowUpSLATool
from app.tools.run_workflow import RunWorkflowTool

logger = logging.getLogger("agents")


@dataclass(frozen=True)
class AgentSpec:
    """Shared metadata for Agno registration and workflow execution."""

    key: str
    name: str
    description: str
    instructions: str
    tool_factories: tuple[Callable[[], Toolkit], ...] = ()


_JSON_INSTRUCTION = (
    "Return only valid JSON. Do not wrap it in markdown. Do not include prose."
)

INTAKE_SPEC = AgentSpec(
    key="intake",
    name="IntakeAgent",
    description="Validates and normalises incoming CSV lead records.",
    instructions=(
        f"{_JSON_INSTRUCTION} Validate leads. Missing company_name is invalid. "
        "Malformed email is invalid. Duplicate company names after the first "
        "occurrence are duplicate. Missing email alone is acceptable."
    ),
    tool_factories=(DataQualityTool, RunWorkflowTool),
)
CLASSIFY_SPEC = AgentSpec(
    key="classify",
    name="ClassifyAgent",
    description="Classifies leads for urgency, risk, and opportunity.",
    instructions=(
        f"{_JSON_INSTRUCTION} Classify valid leads for urgency, risk, "
        "opportunity, confidence, and enrichment notes."
    ),
)
ACTION_SPEC = AgentSpec(
    key="action",
    name="ActionAgent",
    description="Generates prioritised follow-up actions for each lead.",
    instructions=(
        f"{_JSON_INSTRUCTION} Generate one or two prioritized follow-up "
        "recommendations per classified lead."
    ),
    tool_factories=(FollowUpSLATool,),
)
REVIEW_SPEC = AgentSpec(
    key="review",
    name="ReviewAgent",
    description="Reviews workflow outputs for consistency and completeness.",
    instructions=(
        f"{_JSON_INSTRUCTION} Review whether every valid lead has a "
        "classification and every classified lead has a recommendation."
    ),
)


def get_runtime_config() -> dict[str, str | bool]:
    """Return a small, UI-safe summary of the DeepSeek runtime."""

    return {
        "mode": "deepseek",
        "model_id": os.getenv("LLM_MODEL", "deepseek-chat"),
        "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
    }


def _require_api_key() -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is required. This project intentionally has no "
            "offline fallback."
        )
    return api_key


def _build_model() -> DeepSeek:
    return DeepSeek(
        id=os.getenv("LLM_MODEL", "deepseek-chat"),
        api_key=_require_api_key(),
        temperature=0,
        timeout=60,
        max_retries=1,
    )


def build_agno_agent(
    spec: AgentSpec,
    include_spec_tools: bool = True,
) -> AgnoAgent:
    """Create a new Agno agent from a shared spec."""

    tools: list[Toolkit] = (
        [factory() for factory in spec.tool_factories] if include_spec_tools else []
    )
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


def intake_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Intake agent processing %d raw lead(s)", len(state.leads))

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
                for k, v in lead.model_dump().items()
                if k not in ("status", "validation_errors")
            }
            for lead in state.leads
        ],
        indent=2,
    )
    prompt = (
        "Return a JSON array with exactly one object per input lead. Each object "
        "must contain company_name, contact_email, industry, revenue_millions, "
        "employees, lead_source, notes, status, validation_errors. status must be "
        "valid, invalid, or duplicate.\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for intake (%d leads)", len(state.leads))
    data = _run_json_agent(INTAKE_SPEC, prompt)
    if not isinstance(data, list) or len(data) != len(state.leads):
        raise RuntimeError("IntakeAgent did not return one validated record per lead.")

    state.leads = [LeadRecord(**record) for record in data]
    state.metrics.agent_modes["intake"] = "deepseek"
    state.metrics.total_leads = len(state.leads)
    state.metrics.valid_leads = sum(
        1 for lead in state.leads if lead.status == LeadStatus.valid
    )
    state.metrics.invalid_leads = sum(
        1 for lead in state.leads if lead.status == LeadStatus.invalid
    )
    logger.info(
        "Intake: %d valid, %d invalid, %d total",
        state.metrics.valid_leads,
        state.metrics.invalid_leads,
        state.metrics.total_leads,
    )
    return state


def classify_agent_fn(state: WorkflowState) -> WorkflowState:
    valid = [lead for lead in state.leads if lead.status == LeadStatus.valid]
    logger.info("Classify agent processing %d valid lead(s)", len(valid))

    if not valid:
        state.metrics.agent_modes["classify"] = "skipped_no_valid_leads"
        return state

    leads_json = json.dumps(
        [
            {
                "company_name": lead.company_name,
                "industry": lead.industry or "Unknown",
                "revenue_millions": lead.revenue_millions,
                "employees": lead.employees,
                "notes": lead.notes or "",
            }
            for lead in valid
        ],
        indent=2,
    )
    prompt = (
        "Return a JSON array with exactly one object per lead. Each object must "
        "contain company_name, industry, urgency, risk, opportunity, confidence, "
        "enrichment_notes. urgency/risk/opportunity must be low, medium, or high. "
        "confidence must be 0.0 to 1.0.\n\n"
        "Scoring guidance:\n"
        "- urgency: high if revenue > 50M, employees > 500, or notes mention urgent/hot/timeline/POC\n"
        "- risk: high if missing contact info, unknown industry, revenue < 1M, or notes mention churn/competitor/stalled\n"
        "- opportunity: high if revenue > 100M, employees > 1000, industry is tech/SaaS/AI/fintech/healthtech, "
        "or notes mention expanding/growing/new funding/contract\n\n"
        f"Leads:\n{leads_json}"
    )

    logger.info("  Calling DeepSeek for classification (%d leads)", len(valid))
    data = _run_json_agent(CLASSIFY_SPEC, prompt)
    expected_names = {lead.company_name for lead in valid}
    if not isinstance(data, list) or {item.get("company_name") for item in data} != expected_names:
        raise RuntimeError("ClassifyAgent did not classify every valid lead.")

    state.classifications.clear()
    for record in data:
        result = ClassificationResult(**record)
        state.classifications[result.company_name] = result
    state.metrics.agent_modes["classify"] = "deepseek"
    logger.info("Classification: %d leads", len(state.classifications))
    return state


def action_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Action agent generating recommendations")

    items = _classified_items(state)
    if not items:
        state.metrics.agent_modes["action"] = "skipped_no_classifications"
        state.recommendations = []
        return state

    prompt = (
        "Return a JSON array of recommendations. Include at least one and at most "
        "two objects per lead. Each object must contain company_name, action, "
        "priority, assignee, rationale, due_by. priority must be 1, 2, or 3.\n\n"
        "Rules:\n"
        "- High urgency -> executive outreach, priority=1, assignee='Account Executive', due_by='24h'\n"
        "- High opportunity -> custom demo/proposal, priority=1, assignee='Solutions Engineer', due_by='48h'\n"
        "- High risk -> risk assessment call, priority=1, assignee='Customer Success', due_by='72h'\n"
        "- Medium -> SDR qualification, priority=2, assignee='SDR Team', due_by='1 week'\n"
        "- Low signal -> data enrichment first, priority=3, assignee='SDR Team', due_by='2 weeks'\n\n"
        f"Leads:\n{json.dumps(items, indent=2)}"
    )

    logger.info("  Calling DeepSeek for action generation")
    data = _run_json_agent(ACTION_SPEC, prompt)
    expected_names = {item["company_name"] for item in items}
    if not isinstance(data, list) or not expected_names.issubset(
        {item.get("company_name") for item in data}
    ):
        raise RuntimeError("ActionAgent did not recommend an action for every lead.")

    state.recommendations = sorted(
        [ActionRecommendation(**record) for record in data],
        key=lambda rec: (rec.priority, rec.company_name, rec.action),
    )
    state.metrics.agent_modes["action"] = "deepseek"
    logger.info("Action generation: %d recommendations", len(state.recommendations))
    return state


def review_agent_fn(state: WorkflowState) -> WorkflowState:
    logger.info("Review agent checking workflow outputs")

    valid_names = [lead.company_name for lead in state.leads if lead.status == LeadStatus.valid]
    classified_names = list(state.classifications.keys())
    recommended_names = list({rec.company_name for rec in state.recommendations})
    prompt = (
        "Return a JSON object with keys approved and notes.\n\n"
        f"Total leads: {state.metrics.total_leads}\n"
        f"Valid leads: {state.metrics.valid_leads}\n"
        f"Invalid leads: {state.metrics.invalid_leads}\n"
        f"Valid names: {json.dumps(valid_names)}\n"
        f"Classified names: {json.dumps(classified_names)}\n"
        f"Leads with recs: {json.dumps(recommended_names)}\n"
        f"Total recommendations: {len(state.recommendations)}\n\n"
        "Approve only if total leads is greater than zero, every valid lead has "
        "a classification, and every classified lead has at least one recommendation. "
        "Invalid leads being skipped is fine."
    )

    logger.info("  Calling DeepSeek for review")
    data = _run_json_agent(REVIEW_SPEC, prompt)
    if not isinstance(data, dict) or "approved" not in data or "notes" not in data:
        raise RuntimeError("ReviewAgent did not return an approved/notes object.")

    state.review_notes = str(data["notes"])
    state.review_approved = bool(data["approved"])
    state.metrics.success = state.review_approved
    state.metrics.agent_modes["review"] = "deepseek"
    logger.info("Review: approved=%s", state.review_approved)
    return state


def _run_json_agent(spec: AgentSpec, prompt: str) -> object:
    agent = build_agno_agent(spec, include_spec_tools=False)
    response = agent.run(input=prompt)
    content = getattr(response, "content", response)
    return _parse_json(content)


def _parse_json(content: object) -> object:
    if isinstance(content, (dict, list)):
        return content
    text = str(content).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_candidates = [idx for idx in (text.find("["), text.find("{")) if idx >= 0]
        if not start_candidates:
            raise
        start = min(start_candidates)
        end = max(text.rfind("]"), text.rfind("}"))
        if end < start:
            raise
        return json.loads(text[start : end + 1])


def _classified_items(state: WorkflowState) -> list[dict]:
    items: list[dict] = []
    for lead in state.leads:
        if lead.status != LeadStatus.valid:
            continue
        classification = state.classifications.get(lead.company_name)
        if not classification:
            continue
        items.append(
            {
                "company_name": lead.company_name,
                "industry": lead.industry or "Unknown",
                "revenue_millions": lead.revenue_millions,
                "employees": lead.employees,
                "urgency": classification.urgency.value,
                "risk": classification.risk.value,
                "opportunity": classification.opportunity.value,
            }
        )
    return items


AGENT_REGISTRY: dict[str, dict[str, object]] = {
    "intake": {"spec": INTAKE_SPEC, "fn": intake_agent_fn},
    "classify": {"spec": CLASSIFY_SPEC, "fn": classify_agent_fn},
    "action": {"spec": ACTION_SPEC, "fn": action_agent_fn},
    "review": {"spec": REVIEW_SPEC, "fn": review_agent_fn},
}
