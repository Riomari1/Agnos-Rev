"""
Agno Playground UI — visual agent inspection.

Launches a FastAPI app that the Agno OS dashboard connects to.

Usage:
    python -m app.agentos
    # Then open https://os.agno.com → Connect OS → http://localhost:7777
"""

from __future__ import annotations

from agno.playground import Playground

from app.agents.team import action, classify, intake, review

playground = Playground(
    agents=[intake, classify, action, review],
)

app = playground.get_app(use_async=False)

if __name__ == "__main__":
    playground.serve(app="app.agentos:app", reload=True)
