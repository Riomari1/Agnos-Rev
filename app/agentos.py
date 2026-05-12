"""
AgentOS UI - visual agent and workflow inspection.

Launches a FastAPI app that the Agno dashboard connects to.

Usage:
    python -m app.agentos
    # Then open https://os.agno.com and connect to localhost:7777

Note: The workflow is best experienced through the CLI
(``python -m app.main``) or Streamlit UI (``streamlit run demo/ui.py``).
AgentOS is primarily for visual agent inspection and debugging.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Load .env so DEEPSEEK_API_KEY is available for agent model configs
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()

from agno.db.sqlite import SqliteDb
from agno.os import AgentOS

from app.agents.team import build_agent_os_agents
from app.workflows.workflow import RevenueOpsWorkflow

_output_dir = Path(__file__).resolve().parent.parent / "outputs"
_output_dir.mkdir(exist_ok=True)

agent_os = AgentOS(
    agents=build_agent_os_agents(),
    workflows=[RevenueOpsWorkflow()],
    db=SqliteDb(db_file=str(_output_dir / "agentos.db")),
)

app = agent_os.get_app()

if __name__ == "__main__":
    agent_os.serve(app="app.agentos:app", reload=True)
