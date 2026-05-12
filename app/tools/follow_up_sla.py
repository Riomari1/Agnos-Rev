"""
Follow-up SLA tool for action recommendations.

Registered on the ActionAgent to demonstrate Agno's ``Toolkit`` pattern.
"""

from __future__ import annotations

from agno.tools import Toolkit

from app.models.schemas import OpportunityLevel, RiskLevel, UrgencyLevel


class FollowUpSLATool(Toolkit):
    """Maps urgency / risk / opportunity levels to a recommended follow-up
    deadline and a short priority note."""

    def __init__(self) -> None:
        super().__init__(name="follow_up_sla")
        self.register(self.recommend_deadline)
        self.register(self.recommend_priority_note)

    @staticmethod
    def recommend_deadline(
        urgency: str,
        risk: str,
        opportunity: str,
    ) -> str:
        """Return a recommended follow-up deadline based on lead classification.

        Args:
            urgency: 'low', 'medium', or 'high'.
            risk: 'low', 'medium', or 'high'.
            opportunity: 'low', 'medium', or 'high'.

        Returns:
            A human-readable deadline string (e.g. '24h', '72h', '1 week').
        """
        # High urgency trumps everything
        if urgency == UrgencyLevel.high.value:
            return "24h"
        # High risk needs quick triage
        if risk == RiskLevel.high.value:
            return "72h"
        # High opportunity deserves prompt follow-up
        if opportunity == OpportunityLevel.high.value:
            return "48h"
        # Medium urgency/opportunity
        if (
            urgency == UrgencyLevel.medium.value
            or opportunity == OpportunityLevel.medium.value
        ):
            return "1 week"
        # Everything else
        return "2 weeks"

    @staticmethod
    def recommend_priority_note(
        urgency: str,
        risk: str,
        opportunity: str,
    ) -> str:
        """Return a short justification for the recommended priority level.

        Args:
            urgency: 'low', 'medium', or 'high'.
            risk: 'low', 'medium', or 'high'.
            opportunity: 'low', 'medium', or 'high'.

        Returns:
            A one-line rationale (e.g. 'High urgency drives immediate action').
        """
        reasons: list[str] = []
        if urgency == UrgencyLevel.high.value:
            reasons.append("high urgency")
        elif urgency == UrgencyLevel.medium.value:
            reasons.append("moderate urgency")
        if risk == RiskLevel.high.value:
            reasons.append("elevated risk")
        if opportunity == OpportunityLevel.high.value:
            reasons.append("high opportunity")

        if not reasons:
            return "Standard follow-up cadence - no overriding signals."
        return "Driven by " + ", ".join(reasons) + "."
