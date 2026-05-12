"""
Workflow orchestrator for the Revenue Ops Copilot.

Runs agents sequentially with timing, error handling, and structured output.
Designed to be framework-agnostic — each step is a plain function call
on a shared WorkflowState, making it easy to test, reason about, and extend.
"""

from __future__ import annotations

import csv
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from app.agents.team import (
    action_agent,
    classify_agent,
    intake_agent,
    review_agent,
)
from app.models.schemas import LeadRecord, WorkflowState

logger = logging.getLogger("workflow")


class RevenueOpsWorkflow:
    """Orchestrates the full lead-to-recommendation pipeline.

    Usage:
        workflow = RevenueOpsWorkflow()
        state = workflow.run("examples/leads.csv")
    """

    MAX_RETRIES = 2

    def __init__(self) -> None:
        self._output_dir = Path("outputs")
        self._output_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, csv_path: str | Path) -> WorkflowState:
        """Execute the full workflow from a CSV file path.

        Returns a fully populated WorkflowState with metrics and outputs.
        """
        state = WorkflowState(input_path=str(csv_path))
        state.metrics.start_time = datetime.now(timezone.utc).isoformat()

        logger.info("=" * 60)
        logger.info("Revenue Ops Copilot — workflow started")
        logger.info("Input: %s", csv_path)
        logger.info("=" * 60)

        try:
            # 1. Load CSV into state
            self._step(state, "load_csv", self._load_csv, csv_path)

            # 2. Intake — validate & normalise
            self._step(state, "intake", intake_agent)

            # 3. Classify — urgency / risk / opportunity
            self._step(state, "classify", classify_agent)

            # 4. Recommend — generate actions
            self._step(state, "action", action_agent)

            # 5. Review — consistency check
            self._step(state, "review", review_agent)

        except Exception:
            error = traceback.format_exc()
            logger.error("Workflow failed:\n%s", error)
            state.metrics.errors.append(error)
            state.metrics.success = False

        state.metrics.end_time = datetime.now(timezone.utc).isoformat()
        if state.metrics.start_time:
            start = datetime.fromisoformat(state.metrics.start_time)
            end = datetime.fromisoformat(state.metrics.end_time)
            state.metrics.total_duration_ms = round(
                (end - start).total_seconds() * 1000, 2
            )

        self._write_outputs(state)
        self._log_summary(state)
        return state

    # ------------------------------------------------------------------
    # Step execution with retry, timing, and error capture
    # ------------------------------------------------------------------

    def _step(
        self,
        state: WorkflowState,
        name: str,
        func: callable,
        *args,
        **kwargs,
    ) -> None:
        """Execute a single workflow step with retry and timing."""
        last_error: Exception | None = None
        attempt = 0

        while attempt <= self.MAX_RETRIES:
            attempt += 1
            start = time.perf_counter()
            try:
                logger.info(
                    "[%s] Starting (attempt %d/%d)", name, attempt, self.MAX_RETRIES + 1
                )
                func(state, *args, **kwargs)
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                state.metrics.agent_timings_ms[name] = elapsed_ms
                state.metrics.agent_statuses[name] = "success"
                logger.info("[%s] Completed in %.0f ms", name, elapsed_ms)
                return
            except Exception as e:
                elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
                last_error = e
                logger.warning(
                    "[%s] Attempt %d failed after %.0f ms: %s",
                    name,
                    attempt,
                    elapsed_ms,
                    e,
                )
                if attempt > self.MAX_RETRIES:
                    break

        # All attempts exhausted
        state.metrics.agent_timings_ms[name] = round(
            (time.perf_counter() - start) * 1000, 2
        )
        state.metrics.agent_statuses[name] = "failure"
        state.metrics.errors.append(f"[{name}] {last_error}")
        logger.error("[%s] Failed after %d attempts", name, attempt - 1)

    # ------------------------------------------------------------------
    # CSV Loading
    # ------------------------------------------------------------------

    def _load_csv(self, state: WorkflowState, path: str | Path) -> None:
        """Read a CSV file into LeadRecord objects.

        Gracefully handles missing columns, malformed rows, and empty files.
        """
        csv_path = Path(path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError("CSV file appears empty or has no header row.")

            leads: list[LeadRecord] = []
            row_num = 0
            for row_num, row in enumerate(reader, start=1):
                try:
                    lead = self._parse_row(row)
                    leads.append(lead)
                except Exception as e:
                    logger.warning("Skipping row %d: %s", row_num, e)
                    state.metrics.errors.append(f"Row {row_num}: {e}")

            if row_num == 0:
                logger.warning("CSV has header but no data rows.")

            state.leads = leads
            logger.info("Loaded %d lead(s) from CSV", len(leads))

    @staticmethod
    def _parse_row(row: dict[str, str]) -> LeadRecord:
        """Convert a CSV dict row into a typed LeadRecord."""
        revenue_raw = row.get("revenue_millions", "").strip()
        employees_raw = row.get("employees", "").strip()

        revenue: float | None = None
        if revenue_raw:
            try:
                revenue = float(revenue_raw)
            except ValueError:
                raise ValueError(f"Invalid revenue value: '{revenue_raw}'")

        employees: int | None = None
        if employees_raw:
            try:
                employees = int(employees_raw)
            except ValueError:
                raise ValueError(f"Invalid employees value: '{employees_raw}'")

        return LeadRecord(
            company_name=row.get("company_name", "").strip(),
            contact_email=row.get("contact_email", "").strip() or None,
            industry=row.get("industry", "").strip() or None,
            revenue_millions=revenue,
            employees=employees,
            lead_source=row.get("lead_source", "").strip() or None,
            notes=row.get("notes", "").strip() or None,
        )

    # ------------------------------------------------------------------
    # Output writers
    # ------------------------------------------------------------------

    def _write_outputs(self, state: WorkflowState) -> None:
        """Write all output artifacts to the outputs/ directory."""

        self._write_recommendations_json(state)
        self._write_execution_log(state)
        self._write_summary_md(state)

        logger.info("Outputs written to %s", self._output_dir)

    def _write_recommendations_json(self, state: WorkflowState) -> None:
        """Write recommendations.json — structured action items."""
        data = [r.model_dump() for r in state.recommendations]
        path = self._output_dir / "recommendations.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _write_execution_log(self, state: WorkflowState) -> None:
        """Write execution_log.json — full run metadata."""
        data = {
            "input_path": state.input_path,
            "metrics": state.metrics.model_dump(),
            "lead_count": len(state.leads),
            "classification_count": len(state.classifications),
            "recommendation_count": len(state.recommendations),
            "review_approved": state.review_approved,
            "review_notes": state.review_notes,
        }
        path = self._output_dir / "execution_log.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _write_summary_md(self, state: WorkflowState) -> None:
        """Write summary.md — human-readable markdown report."""
        lines: list[str] = [
            "# Revenue Ops Copilot — Summary",
            "",
            f"- **Input file**: `{state.input_path}`",
            f"- **Status**: {'✅ Approved' if state.review_approved else '❌ Needs review'}",
            f"- **Total leads**: {state.metrics.total_leads}",
            f"- **Valid leads**: {state.metrics.valid_leads}",
            f"- **Invalid leads**: {state.metrics.invalid_leads}",
            f"- **Duration**: {state.metrics.total_duration_ms:.0f} ms",
            "",
            "## Agent Timings",
            "",
        ]
        for agent, ms in state.metrics.agent_timings_ms.items():
            status = state.metrics.agent_statuses.get(agent, "?")
            lines.append(f"- **{agent}**: {ms:.0f} ms ({status})")

        lines += [
            "",
            "## Review Notes",
            "",
            state.review_notes or "No review notes.",
            "",
            "## Recommendations",
            "",
        ]

        if state.recommendations:
            lines.append("| Priority | Company | Action | Assignee | Due By |")
            lines.append("|----------|---------|--------|----------|--------|")
            for r in state.recommendations:
                lines.append(
                    f"| {r.priority} | {r.company_name} | {r.action} | {r.assignee} | {r.due_by or '-'} |"
                )
        else:
            lines.append("_No recommendations generated._")

        if state.metrics.errors:
            lines += ["", "## Errors", ""]
            for err in state.metrics.errors:
                lines.append(f"- {err}")

        path = self._output_dir / "summary.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _log_summary(self, state: WorkflowState) -> None:
        """Print a concise run summary to the terminal."""
        logger.info("=" * 60)
        logger.info("Workflow complete")
        logger.info(
            "  Status:      %s",
            "✅ Approved" if state.review_approved else "❌ Needs review",
        )
        logger.info("  Total leads: %d", state.metrics.total_leads)
        logger.info("  Valid:       %d", state.metrics.valid_leads)
        logger.info("  Invalid:     %d", state.metrics.invalid_leads)
        logger.info("  Recs:        %d", len(state.recommendations))
        logger.info("  Duration:    %.0f ms", state.metrics.total_duration_ms or 0)
        logger.info("  Outputs:     %s", self._output_dir)
        logger.info("=" * 60)
