"""Global test configuration: the suite is always offline."""

import os

import pytest

# An empty value is still "present", so python-dotenv (override=False) will not
# replace it with a key from a parent directory's .env.
os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("GEMINI_MODEL", "gemini-2.5-flash")

# FIX (deploy-ready, item 7): existing LLM unit tests make a dozen fake calls
# in one test, so the suite runs with the caps effectively off. Tests that
# exercise the caps set their own (small) values with monkeypatch.
os.environ.setdefault("LIVE_CALLS_PER_DAY", "10000")
os.environ.setdefault("LIVE_CALLS_PER_IP_PER_HOUR", "10000")


@pytest.fixture(autouse=True)
def _reset_deploy_limits():
    """FIX (deploy-ready, item 7): the budget/throttles are process-global.

    Without this, whichever test runs 15 live-client attempts first would make
    every later test see a capped budget.
    """
    from agent.live_budget import reset_all

    reset_all()
    yield
    reset_all()
