"""
Agno Playground UI — visual agent inspection.

Launches a FastAPI app that the Agno OS dashboard connects to.

Usage:
    python -m app.agentos
    # Then open https://app.agno.com/playground and connect to localhost:7777

Note: The workflow pipeline is orchestrated in code via
``RevenueOpsWorkflow._execute()`` and is best experienced through the CLI
(``python -m app.main``) or Streamlit UI (``streamlit run demo/ui.py``).
The Playground is primarily for visual agent inspection.
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

from agno.playground import Playground

from app.agents.team import action, classify, intake, review
from app.workflows.workflow import RevenueOpsWorkflow

playground = Playground(
    agents=[intake, classify, action, review],
    workflows=[RevenueOpsWorkflow()],
)

app = playground.get_app(use_async=False)

if __name__ == "__main__":
    playground.serve(app="app.agentos:app", reload=True)
