"""FIX (code freeze, item 4): the Alexa+ simulation lives in the single-file
UI, so its wake-phrase contract is checked by reading the shipped HTML. The
regex is JavaScript, but this pattern uses only syntax Python shares."""

from __future__ import annotations

import re
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[2] / "frontend" / "campuspilot_ui.html"


def _wake_regex() -> re.Pattern[str]:
    html = UI_PATH.read_text(encoding="utf-8")
    match = re.search(r"const ALEXA_WAKE = /(.*?)/i;", html)
    assert match, "ALEXA_WAKE is missing from frontend/campuspilot_ui.html"
    return re.compile(match.group(1), re.IGNORECASE)


def test_alexa_wake_phrase_is_stripped_before_chat() -> None:
    wake = _wake_regex()
    html = UI_PATH.read_text(encoding="utf-8")

    # The parsed utterance is what actually leaves the browser.
    assert "if (alexaModeOn()) text = stripAlexaWake(text);" in html

    assert (
        wake.sub("", "Alexa, ask CampusPilot to plan my next two days").strip()
        == "plan my next two days"
    )
    assert (
        wake.sub("", "Hey Alexa, ask CampusPilot what my deadlines are").strip()
        == "what my deadlines are"
    )

    # Plain typed messages are untouched, so text input keeps working.
    assert wake.sub("", "what are my deadlines") == "what are my deadlines"

    # The honest disclaimer ships with the simulation.
    assert "Simulated Alexa+ experience. Not connected to a real Alexa service." in html
