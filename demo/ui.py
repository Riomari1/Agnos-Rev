"""
Streamlit UI for the Revenue Ops Copilot.

Usage:
    streamlit run demo/ui.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv

load_dotenv(_project_root / ".env")

import streamlit as st

from app.workflows.workflow import RevenueOpsWorkflow

st.set_page_config(page_title="Revenue Ops Copilot", layout="wide")
st.title("💰 Revenue Ops Copilot")
st.markdown(
    "Upload a CSV of leads to classify urgency, risk, opportunity, "
    "and get action recommendations."
)

st.sidebar.header("About")
st.sidebar.markdown(
    "**AI-powered via DeepSeek.** "
    "All 4 agents call DeepSeek for classification, action generation, "
    "and review (~60s for 10 leads). Requires `DEEPSEEK_API_KEY` in `.env`."
)

st.sidebar.header("Sample Files")
st.sidebar.markdown(
    "Try one of the built-in examples from the terminal:\n\n"
    "```\npython -m app.main examples/leads_clean.csv\n"
    "python -m app.main examples/leads_mixed_quality.csv\n"
    "python -m app.main examples/leads_error_cases.csv\n```"
)

uploaded_file = st.file_uploader("Choose a CSV file", type="csv")

if uploaded_file is None:
    st.info("👆 Upload a CSV file to get started.")
    st.stop()

if st.button("▶ Run Workflow", type="primary"):
    with st.spinner("Running workflow..."):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp.write(uploaded_file.getvalue())
                tmp_path = tmp.name

            state = RevenueOpsWorkflow.run_sync(tmp_path)

        except Exception as e:
            st.error(f"Workflow failed: {e}")
            st.stop()
        finally:
            try:
                os.unlink(tmp_path)
            except (NameError, OSError):
                pass

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Status", "✅ Approved" if state.review_approved else "❌ Needs review")
    col2.metric("Total Leads", state.metrics.total_leads)
    col3.metric("Valid", state.metrics.valid_leads)
    col4.metric("Invalid", state.metrics.invalid_leads)

    st.divider()
    st.subheader("Agent Execution")
    agent_cols = st.columns(len(state.metrics.agent_timings_ms) or 1)
    for i, (agent, ms) in enumerate(state.metrics.agent_timings_ms.items()):
        status = state.metrics.agent_statuses.get(agent, "?")
        ico = "✅" if status == "success" else "❌"
        agent_cols[i].metric(f"{ico} {agent}", f"{ms:.0f} ms", status)

    if state.metrics.errors:
        st.divider()
        st.subheader("Errors")
        for err in state.metrics.errors:
            st.warning(err)

    st.divider()
    st.subheader("Review Notes")
    st.text(state.review_notes or "No review notes.")

    st.divider()
    st.subheader(f"Recommendations ({len(state.recommendations)})")

    if state.recommendations:
        rows = [
            {
                "Priority": r.priority,
                "Company": r.company_name,
                "Action": r.action,
                "Assignee": r.assignee,
                "Due By": r.due_by or "-",
                "Rationale": r.rationale,
            }
            for r in state.recommendations
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No recommendations generated.")

    st.divider()
    st.caption(
        f"Duration: {state.metrics.total_duration_ms:.0f} ms | "
        f"Input: {uploaded_file.name}"
    )
else:
    st.info("👆 Click **▶ Run Workflow** to process the uploaded CSV.")
