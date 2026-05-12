"""
Workflow orchestrator for the Revenue Ops Copilot.

Extends ``agno.workflow.Workflow`` for real Agno-based orchestration
scaffolding — class hierarchy, session management, and execution metadata.

NOTE on Agno's ``_subclass_run`` pattern:
  ``run_workflow()`` internally assigns ``_subclass_run = self.run``,
  so overriding ``run`` and calling ``run_workflow()`` creates a
  recursion loop.  Instead we expose a clean ``run_sync`` factory
  and call Agno's session setup methods directly.
"""

from __future__ import annotations

import csv
import json
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from agno.workflow import Workflow as AgnoWorkflow
from pydantic import ValidationError

from app.agents.team import AGENT_REGISTRY
from app.models.schemas import LeadRecord, WorkflowState

logger = logging.getLogger("workflow")


class RevenueOpsWorkflow(AgnoWorkflow):
    """Orchestrates the full lead-to-recommendation pipeline.

    Extends ``agno.workflow.Workflow`` for session management, run IDs,
    and execution traceability.  Usage:

        state = RevenueOpsWorkflow.run_sync("examples/leads.csv")
    """

    MAX_ATTEMPTS = 3  # first try + 2 retries

    def __init__(self) -> None:
        super().__init__(
            name="RevenueOpsCopilot",
            description=(
                "Multi-agent workflow: ingests leads from CSV, "
                "validates records, classifies urgency/risk/opportunity, "
                "generates action recommendations, and reviews for consistency."
            ),
        )
        self._output_dir = getattr(
            self.__class__, "_output_dir_override", Path("outputs")
        )
        self._output_dir.mkdir(exist_ok=True)

        # Set up Agno session scaffolding directly
        self.set_storage_mode()
        self.set_debug()
        self.set_monitoring()
        self.set_workflow_id()
        self.set_session_id()
        self.run_id = str(int(time.time() * 1_000_000))
        self.initialize_memory()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def run_sync(csv_path: str | Path) -> WorkflowState:
        """Convenience factory: create a workflow, run it, return state.

        Example:
            state = RevenueOpsWorkflow.run_sync("examples/leads.csv")
        """
        wf = RevenueOpsWorkflow()
        return wf._execute(csv_path)

    def _execute(self, csv_path: str | Path) -> WorkflowState:
        """Core execution logic."""
        state = WorkflowState(input_path=str(csv_path))
        state.metrics.start_time = datetime.now(timezone.utc).isoformat()

        logger.info("=" * 60)
        logger.info("Revenue Ops Copilot — workflow started")
        logger.info("  Agno session : %s", self.session_id or "(not set)")
        logger.info("  Agno run ID  : %s", self.run_id or "(not set)")
        logger.info("  Input file   : %s", csv_path)
        logger.info("=" * 60)

        self._lead_cache = self._load_cache()

        try:
            self._step(state, "load_csv", None, self._load_csv, csv_path)
            self._step(state, "check_cache", None, self._check_cache)
            self._step(state, "intake", *AGENT_REGISTRY["intake"])
            self._step(state, "classify", *AGENT_REGISTRY["classify"])
            self._step(state, "action", *AGENT_REGISTRY["action"])
            self._step(state, "review", *AGENT_REGISTRY["review"])

        except Exception:
            error = traceback.format_exc()
            logger.error("Workflow failed:\n%s", error)
            state.metrics.errors.append(error)
            state.metrics.success = False

        state.metrics.end_time = datetime.now(timezone.utc).isoformat()
        if state.metrics.start_time:
            start_dt = datetime.fromisoformat(state.metrics.start_time)
            end_dt = datetime.fromisoformat(state.metrics.end_time)
            state.metrics.total_duration_ms = round(
                (end_dt - start_dt).total_seconds() * 1000, 2
            )

        self._save_cache(state)
        self._write_outputs(state)
        self._log_summary(state)
        return state

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _step(
        self,
        state: WorkflowState,
        name: str,
        agno_agent: object,
        func: callable,
        *args,
        **kwargs,
    ) -> None:
        """Execute one workflow step with retry, timing, and error capture."""
        agent_name = (
            getattr(agno_agent, "name", name) if agno_agent is not None else name
        )
        instructions = (
            getattr(agno_agent, "instructions", "").strip()
            if agno_agent is not None
            else ""
        )
        instructions_preview = (
            instructions[:60] + "…" if len(instructions) > 60 else instructions
        )

        _last_error: Exception | None = None
        overall_start = time.perf_counter()

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            attempt_start = time.perf_counter()
            try:
                logger.info(
                    "  ⚙  %s  [attempt %d/%d]", agent_name, attempt, self.MAX_ATTEMPTS
                )
                if attempt == 1:
                    logger.debug("  Instructions: %s", instructions_preview)

                func(state, *args, **kwargs)

                elapsed_ms = round((time.perf_counter() - attempt_start) * 1000, 2)
                state.metrics.agent_timings_ms[name] = elapsed_ms
                state.metrics.agent_statuses[name] = "success"
                logger.info("  ✓ %s  completed in %.0f ms", agent_name, elapsed_ms)
                return

            except Exception as e:
                elapsed_ms = round((time.perf_counter() - attempt_start) * 1000, 2)
                _last_error = e
                logger.warning(
                    "  ✗ %s  attempt %d failed after %.0f ms: %s",
                    agent_name,
                    attempt,
                    elapsed_ms,
                    e,
                )
                if attempt == self.MAX_ATTEMPTS:
                    break

        overall_ms = round((time.perf_counter() - overall_start) * 1000, 2)
        state.metrics.agent_timings_ms[name] = overall_ms
        state.metrics.agent_statuses[name] = "failure"
        state.metrics.errors.append(f"[{name}] {_last_error}")
        logger.error(
            "  ✗ %s  failed after %d attempt(s) (%d ms)",
            agent_name,
            self.MAX_ATTEMPTS,
            overall_ms,
        )

    # ------------------------------------------------------------------
    # Cross-session lead cache
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict:
        """Load previously seen leads from a JSON file cache."""
        path = self._output_dir / "lead_cache.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"companies": {}}
        return {"companies": {}}

    def _check_cache(self, state: WorkflowState) -> None:
        """Cross-reference leads against the cache and flag repeats."""
        seen = self._lead_cache.get("companies", {})
        for lead in state.leads:
            key = lead.company_name.strip().lower()
            if key and key in seen and lead.status.value == "valid":
                prev = seen[key]
                logger.info(
                    "  Lead '%s' previously seen on %s — cross-referenced",
                    lead.company_name,
                    prev.get("first_seen", "unknown"),
                )

    def _save_cache(self, state: WorkflowState) -> None:
        """Save newly-seen leads to the cache file."""
        path = self._output_dir / "lead_cache.json"
        now = datetime.now(timezone.utc).isoformat()
        for lead in state.leads:
            key = lead.company_name.strip().lower()
            if key and key not in self._lead_cache["companies"]:
                self._lead_cache["companies"][key] = {
                    "first_seen": now,
                    "email": lead.contact_email,
                }
        path.write_text(
            json.dumps(self._lead_cache, indent=2, default=str), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # CSV Loading
    # ------------------------------------------------------------------

    def _load_csv(self, state: WorkflowState, path: str | Path) -> None:
        """Read a CSV into ``LeadRecord`` objects, skipping malformed rows."""
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
                except ValidationError as e:
                    # Clean one-line summary instead of raw Pydantic error dump
                    brief = "; ".join(
                        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                        for err in e.errors()
                    )
                    logger.warning("  Skipping row %d: %s", row_num, brief)
                    state.metrics.errors.append(f"Row {row_num}: {brief}")
                except Exception as e:
                    logger.warning("  Skipping row %d: %s", row_num, e)
                    state.metrics.errors.append(f"Row {row_num}: {e}")

            if row_num == 0:
                logger.warning("  CSV has header but no data rows.")

            state.leads = leads
            logger.info("  Loaded %d lead(s) from CSV", len(leads))

    @staticmethod
    def _parse_row(row: dict[str, str]) -> LeadRecord:
        """Convert a CSV dict row into a typed ``LeadRecord``."""
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
        """Write all output artifacts to ``outputs/``."""
        self._write_recommendations_json(state)
        self._write_execution_log(state)
        self._write_summary_md(state)
        logger.info("  Outputs written to %s", self._output_dir)

    def _write_recommendations_json(self, state: WorkflowState) -> None:
        data = [r.model_dump() for r in state.recommendations]
        path = self._output_dir / "recommendations.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _write_execution_log(self, state: WorkflowState) -> None:
        data = {
            "workflow": {
                "name": self.name,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "description": self.description,
            },
            "input_path": state.input_path,
            "metrics": state.metrics.model_dump(),
            "agents": {
                name: {
                    "agno_name": getattr(agno, "name", name),
                    "status": state.metrics.agent_statuses.get(name, "unknown"),
                    "timing_ms": state.metrics.agent_timings_ms.get(name),
                }
                for name, (agno, _fn) in AGENT_REGISTRY.items()
            },
            "lead_count": len(state.leads),
            "classification_count": len(state.classifications),
            "recommendation_count": len(state.recommendations),
            "review_approved": state.review_approved,
            "review_notes": state.review_notes,
        }
        path = self._output_dir / "execution_log.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _write_summary_md(self, state: WorkflowState) -> None:
        lines: list[str] = [
            "# Revenue Ops Copilot — Summary",
            "",
            f"- **Input file**: `{state.input_path}`",
            f"- **Agno session**: `{self.session_id or 'N/A'}`",
            f"- **Agno run**: `{self.run_id or 'N/A'}`",
            f"- **Status**: {'✅ Approved' if state.review_approved else '❌ Needs review'}",
            f"- **Total leads**: {state.metrics.total_leads}",
            f"- **Valid leads**: {state.metrics.valid_leads}",
            f"- **Invalid leads**: {state.metrics.invalid_leads}",
            f"- **Duration**: {state.metrics.total_duration_ms:.0f} ms",
            "",
            "## Agent Execution",
            "",
        ]
        for agent, ms in state.metrics.agent_timings_ms.items():
            status = state.metrics.agent_statuses.get(agent, "?")
            ico = "✅" if status == "success" else "❌"
            lines.append(f"- {ico} **{agent}**: {ms:.0f} ms ({status})")

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
            lines.append("| Pri | Company | Action | Assignee | Due By |")
            lines.append("|-----|---------|--------|----------|--------|")
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
        logger.info("=" * 60)
        logger.info("Workflow complete")
        logger.info(
            "  Status:      %s",
            "✅ Approved" if state.review_approved else "❌ Needs review",
        )
        logger.info("  Session:     %s", self.session_id or "N/A")
        logger.info("  Run ID:      %s", self.run_id or "N/A")
        logger.info("  Total leads: %d", state.metrics.total_leads)
        logger.info("  Valid:       %d", state.metrics.valid_leads)
        logger.info("  Invalid:     %d", state.metrics.invalid_leads)
        logger.info("  Recs:        %d", len(state.recommendations))
        logger.info("  Duration:    %.0f ms", state.metrics.total_duration_ms or 0)
        logger.info("  Outputs:     %s", self._output_dir)
        logger.info("=" * 60)
