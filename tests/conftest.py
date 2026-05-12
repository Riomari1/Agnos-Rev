"""Shared test configuration.

Loads .env and forces deterministic local agent mode for ordinary tests.
Dedicated tests monkeypatch the Agno Agent.run path to verify DeepSeek wiring
without depending on live network access.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_tmp_root = Path.cwd() / ".pytest_tmp"
_tmp_root.mkdir(exist_ok=True)
os.environ.setdefault("TMP", str(_tmp_root))
os.environ.setdefault("TEMP", str(_tmp_root))
os.environ.setdefault("REVENUE_OPS_AGENT_MODE", "local")

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()
