"""Shared test configuration.

Loads .env so DEEPSEEK_API_KEY is available to tests.  When the key
is present all agent functions use the LLM path; when absent they fall
back to deterministic rules.
"""

from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
else:
    load_dotenv()
