"""Shared test configuration.

Loads .env for the live DeepSeek/Agno test path.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_tmp_root = Path("C:/tmp/agno-takehome-pytest-env")
_tmp_root.mkdir(exist_ok=True)
os.environ.setdefault("TMP", str(_tmp_root))
os.environ.setdefault("TEMP", str(_tmp_root))

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()
