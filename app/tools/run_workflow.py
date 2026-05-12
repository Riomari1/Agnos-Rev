"""
Run-workflow tool - bridges AgentOS chat to the CSV pipeline.

Registered on an agent so the user can type prompts like
"process examples/leads.csv" from the AgentOS chat UI, and the LLM
calls this tool to execute the full workflow and return results.
"""

from __future__ import annotations

from agno.tools import Toolkit


class RunWorkflowTool(Toolkit):
    """Executes the full Revenue Ops Copilot pipeline on a CSV file."""

    def __init__(self) -> None:
        super().__init__(name="run_workflow")
        self.register(self.process_leads)

    @staticmethod
    def process_leads(csv_path: str = "examples/leads.csv") -> str:
        """Run the multi-agent lead classification pipeline on the given CSV.

        Lazy-imports ``RevenueOpsWorkflow`` to avoid circular dependency.

        Args:
            csv_path: Path to the CSV file with lead records.
                      Defaults to ``examples/leads.csv``.

        Returns:
            A markdown-formatted summary of results.
        """
        from app.workflows.workflow import RevenueOpsWorkflow

        state = RevenueOpsWorkflow.run_sync(csv_path)
        recs = state.recommendations[:5]
        rec_lines = "".join(
            f"- [{r.priority}] **{r.company_name}**: {r.action}  \n" for r in recs
        )
        return (
            f"### Revenue Ops Copilot - Results\n\n"
            f"**Input:** `{csv_path}`\n"
            f"**Status:** {'Approved' if state.review_approved else 'Needs review'}\n"
            f"**Leads:** {state.metrics.total_leads} total, "
            f"{state.metrics.valid_leads} valid, {state.metrics.invalid_leads} invalid\n"
            f"**Recommendations:** {len(state.recommendations)} generated\n"
            f"**Duration:** {state.metrics.total_duration_ms:.0f} ms\n\n"
            f"**Top actions:**\n{rec_lines}"
        )
