# FIX_REPORT — CampusPilot submission freeze

Branch: `submission-fixes` (tag `pre-freeze-backup` marks the pre-freeze state).
Every entry: file, why, before → after. Suite result recorded for each item.

Baseline suite before any fix: `python -m pytest -q` → **138 passed** (Python 3.11, `.venv311`).

---

## Item 1 — TRACK: "Alexa+ simulated experience"

### `frontend/campuspilot_ui.html`
- **Why:** the hackathon track requires a clearly labelled, honest *simulation* of a
  voice-first Alexa+ experience that lives in this repo and can be demoed.
- **Before:** no Alexa/voice-first mode. Voice input only through the sidebar mic
  toggle; no wake phrase; `recognition.start()` failures were swallowed silently
  (no message at all).
- **After:**
  - Header button **"Alexa+ (simulated)"** (`#alexaModeBtn`, `aria-pressed`,
    `cp-on` state) toggles the mode; state persisted in the existing
    `campuspilot.ui` config under `voice.alexa`.
  - Fixed bar under the chat header with a listening ring (`#alexaRing`,
    `alexaRingPulse` animation), a **Talk** button (routes through the existing
    `SpeechRecognition` code), a live status label, and the always-visible line
    *"Simulated Alexa+ experience. Not connected to a real Alexa service."*
  - `ALEXA_WAKE` regex strips `"Alexa, ask CampusPilot to ..."` (and
    `"…CampusPilot what …"`) from the utterance **before** it is shown as a user
    bubble and before the `/chat` POST; the reply is spoken via the existing
    `speechSynthesis` path and rendered as a normal agent card.
  - `<!-- FIX -->` comment marks the simulation disclaimer markup; `/* FIX: */`
    comments mark the simulation code and the mic-start failure message.
  - `recognition.start()` now reports failure (`Toast` + "Mic unavailable — type
    instead") instead of failing silently.

### `docs/alexa_simulation.md` (new)
- **Why:** the track requires the simulation to be documented honestly.
- **Before:** did not exist. **After:** 10 lines — what is simulated, that it is
  not a live Alexa/Amazon/MCP integration, where the source is, how to demo it.

**Evidence:** ran the UI against a live local server (`uvicorn server:app`,
temp DB) and drove the browser: toggle on → placeholder/status update;
typed `Alexa, ask CampusPilot to plan my next two days` → intercepted fetch body
was `{"message":"plan my next two days","language":"en"}` (wake phrase stripped),
user bubble showed the stripped text, proposal modal opened, and
`speechSynthesis.speaking === true` after the reply; ring computed style showed
`animation: alexaRingPulse 1.35s` when listening class is applied.

**Suite after item 1:** `python -m pytest -q` → **138 passed**.
