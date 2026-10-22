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

---

## Item 2 — REAL PATH

### `frontend/campuspilot_ui.html`
- **Why:** sample markup in the core workflow must never be presented as real
  data, the UI must flag fallback mode visibly, and failures must be friendly.
- **Before:**
  - Hard-coded task cards, deadline cards, timeline nodes, a canned chat
    conversation ("Hey Aditya! I've scanned your syllabi…"), and canned diff
    rows (`[ADDED]` / `[MOVED]` / `[REMOVED]`) sat in the markup as initial
    content; they were only overwritten *after* a successful `/state` read, so a
    failed or slow load left sample content on screen looking real.
  - No explicit fallback label: only a small "Model: Rule-based quick mode"
    readout; if a live key failed mid-session the header still said "Gemini
    (live)".
  - A backend/network failure surfaced as the browser's bare `Failed to fetch`.
- **After:**
  - A `<!-- FIX -->`-marked init block clears `#taskListContainer`,
    `#deadlineItems`, `#timelineBranchContainer`, `#diffList` and the sample
    chat children (keeping `#agentThinkingBox`, which bubbles are inserted
    before) **before** the first `/state` read, so every card, timeline node and
    chat message on screen comes from `/state` or is absent.
  - New always-present badge `#fallbackBadge` ("Fallback mode · quick reply")
    beside the agent status: shown whenever `state.llm !== 'live'`, and latched
    on by `data.used_fallback` from any chat turn (so a live key that fails
    mid-demo flips it too, and it survives the post-chat `/state` refresh).
  - `callApi()` now throws a friendly message ("Cannot reach the CampusPilot
    server…") instead of `Failed to fetch`; `friendly_notice` on the server
    already covers quota/unavailable cases with no traceback.

**Evidence (ran, not assumed):**
- `USE_MOCK = false`, `API_BASE = window.location.origin` in the served HTML;
  fresh server + fresh DB on `:8011` → `GET /state` 200, 6 seeded tasks.
- HTTP flow (urllib + cookie jar + direct SQLite counts): plan → proposal with
  **0** active rows → confirm → **1** active → confirm again → **409 "There is
  no plan waiting for confirmation."**, still 1 active → new proposal → cancel
  → active `plan_id` unchanged, pending cleared, still 1 active → save of an
  unknown plan → 409 error payload.
- **Restart persistence:** killed the server, restarted on the same DB →
  `/state` returned the same `plan_5fbb45ae…` as active; in the browser a page
  refresh showed "Active plan synced".
- **Backend failure, UI:** stopped the server, clicked *Confirm and Save* →
  error toast "Cannot reach the CampusPilot server…", success panel stayed
  hidden, no "Plan saved" text anywhere.
- **Fallback label:** server with `llm=live` but an invalid key → `/chat`
  returned `used_fallback: true`, notice "Live AI was unavailable; quick mode
  handled this turn.", no traceback; in the UI the badge flipped from hidden to
  "Fallback mode · quick reply" and stayed visible after the state refresh.
- **Real-path round trip in UI:** clicking *Mark done* on "DBMS Lab
  Submission" → toast + completed count 1 → 2 from `/state`.
- Sample content check after load: none of the canned strings
  ("I've scanned your syllabi", "Dept Seminar vs Badminton Practice",
  "[MOVED] Badminton Practice", "AI Proposed Slot") remain in the DOM.

**Suite after item 2:** `python -m pytest -q` → **138 passed**.
