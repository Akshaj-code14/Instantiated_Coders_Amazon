"""Global test configuration: the suite is always offline."""

import os

# An empty value is still "present", so python-dotenv (override=False) will not
# replace it with a key from a parent directory's .env.
os.environ["GEMINI_API_KEY"] = ""
os.environ.setdefault("GEMINI_MODEL", "gemini-2.5-flash")
