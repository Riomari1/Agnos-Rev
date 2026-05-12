"""
Structured output tools for LLM-powered agents.

Instead of asking the LLM to return JSON (fragile), we give each agent
typed tool functions it can call.  The LLM decides *what* values to pass;
the tool handles validation and storage.  Output is always valid.
"""

from __future__ import annotations

from typing import Optional

from agno.tools import Toolkit


class IntakeTools(Toolkit):
    """Tools for IntakeAgent: flag a lead as valid, invalid, or duplicate."""

    def __init__(self, results: list[dict]):
        super().__init__(name="intake_tools")
        self._results = results
        self.register(self.flag_valid)
        self.register(self.flag_invalid)
        self.register(self.flag_duplicate)

    def flag_valid(
        self,
        company_name: str,
        contact_email: Optional[str] = None,
        industry: Optional[str] = None,
        revenue_millions: Optional[float] = None,
        employees: Optional[int] = None,
        lead_source: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Mark a lead as valid with its normalised fields."""
        self._results.append(
            {
                "company_name": company_name,
                "contact_email": contact_email,
                "industry": industry,
                "revenue_millions": revenue_millions,
                "employees": employees,
                "lead_source": lead_source,
                "notes": notes,
                "status": "valid",
                "validation_errors": [],
            }
        )
        return f"OK: {company_name} marked valid"

    def flag_invalid(
        self,
        company_name: str,
        validation_errors: list[str],
        contact_email: Optional[str] = None,
        industry: Optional[str] = None,
        revenue_millions: Optional[float] = None,
        employees: Optional[int] = None,
        lead_source: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Mark a lead as invalid with specific error messages."""
        self._results.append(
            {
                "company_name": company_name or "UNKNOWN",
                "contact_email": contact_email,
                "industry": industry,
                "revenue_millions": revenue_millions,
                "employees": employees,
                "lead_source": lead_source,
                "notes": notes,
                "status": "invalid",
                "validation_errors": validation_errors,
            }
        )
        return f"OK: {company_name} marked invalid: {validation_errors}"

    def flag_duplicate(
        self,
        company_name: str,
        contact_email: Optional[str] = None,
        industry: Optional[str] = None,
        revenue_millions: Optional[float] = None,
        employees: Optional[int] = None,
        lead_source: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Mark a lead as a duplicate of an earlier entry."""
        self._results.append(
            {
                "company_name": company_name,
                "contact_email": contact_email,
                "industry": industry,
                "revenue_millions": revenue_millions,
                "employees": employees,
                "lead_source": lead_source,
                "notes": notes,
                "status": "duplicate",
                "validation_errors": [f"Duplicate company name: {company_name}"],
            }
        )
        return f"OK: {company_name} marked duplicate"


class ClassifyTools(Toolkit):
    """Tools for ClassifyAgent: submit a classification for one lead."""

    def __init__(self, results: list[dict]):
        super().__init__(name="classify_tools")
        self._results = results
        self.register(self.classify_lead)

    def classify_lead(
        self,
        company_name: str,
        urgency: str,
        risk: str,
        opportunity: str,
        confidence: float,
        enrichment_notes: str,
        industry: str = "Unknown",
    ) -> str:
        """Classify a single lead. urgency/risk/opportunity must be low/medium/high.
        confidence must be 0.0-1.0."""
        if urgency not in ("low", "medium", "high"):
            return f"ERROR: urgency must be low/medium/high, got '{urgency}'"
        if risk not in ("low", "medium", "high"):
            return f"ERROR: risk must be low/medium/high, got '{risk}'"
        if opportunity not in ("low", "medium", "high"):
            return f"ERROR: opportunity must be low/medium/high, got '{opportunity}'"
        if not 0.0 <= confidence <= 1.0:
            return f"ERROR: confidence must be 0.0-1.0, got {confidence}"
        self._results.append(
            {
                "company_name": company_name,
                "industry": industry,
                "urgency": urgency,
                "risk": risk,
                "opportunity": opportunity,
                "confidence": confidence,
                "enrichment_notes": enrichment_notes,
            }
        )
        return f"OK: {company_name} classified as urgency={urgency} risk={risk} opportunity={opportunity}"


class ActionTools(Toolkit):
    """Tools for ActionAgent: add a follow-up recommendation."""

    def __init__(self, results: list[dict]):
        super().__init__(name="action_tools")
        self._results = results
        self.register(self.add_recommendation)

    def add_recommendation(
        self,
        company_name: str,
        action: str,
        priority: int,
        assignee: str,
        rationale: str,
        due_by: str,
    ) -> str:
        """Add a follow-up recommendation for a lead. priority 1=highest, 3=lowest."""
        if priority not in (1, 2, 3):
            return f"ERROR: priority must be 1/2/3, got {priority}"
        self._results.append(
            {
                "company_name": company_name,
                "action": action,
                "priority": priority,
                "assignee": assignee,
                "rationale": rationale,
                "due_by": due_by,
            }
        )
        return f"OK: recommendation added for {company_name}: {action}"


class ReviewTools(Toolkit):
    """Tools for ReviewAgent: submit the final review verdict."""

    def __init__(self, result: dict):
        super().__init__(name="review_tools")
        self._result = result
        self.register(self.submit_review)

    def submit_review(self, approved: bool, notes: str) -> str:
        """Submit the review verdict. approved=True if all checks pass."""
        self._result["approved"] = approved
        self._result["notes"] = notes
        return f"OK: review submitted, approved={approved}"
