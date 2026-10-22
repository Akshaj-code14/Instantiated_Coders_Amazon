# Devpost draft — CampusPilot (TEAM TO FILL: final project name)

> Paste-ready draft for Devpost. Anything marked **TEAM TO FILL** must be
> completed by a human. Do not invent experiences, numbers, screenshots or
> metrics — the prompts at the end exist to collect what actually happened.

## Project description (short)

CampusPilot is an AI academic co-pilot: you tell it what changed ("I lost two
hours"), it proposes a plan, shows you the exact diff, and saves nothing until
you confirm. One FastAPI process serves a single-file UI and a JSON API, the
agent's every write goes through validated tools, and everything lives in a
local SQLite database — so it works offline for a demo and never depends on a
network.

## Problem

University schedules break silently: deadlines move, lab sessions run over,
practice clashes with a seminar, and the to-do list nobody reconciles. Existing
tools either store tasks (no reasoning) or auto-schedule without asking (no
trust). Students end up maintaining the plan by hand.

## How it works

1. **Browser → FastAPI** — one process serves `frontend/campuspilot_ui.html`
   and the JSON API from the same origin.
2. **FastAPI → Agent** — `POST /chat` runs the agent (LLM when a key is set,
   deterministic quick-mode router otherwise) with 12 tools: context,
   deadlines, schedule, free slots, create/replan plan, update/create task,
   notices, check-in, wellbeing.
3. **Agent → Backend → SQLite** — every tool call is validated (the model
   cannot invent ids or flip `confirmed_by_user`), then domain services write
   to SQLite through a repository layer.
4. **Confirm-to-save** — `create_plan` / `replan_schedule` only insert a
   `proposed` plan; `POST /plan/save` is the single path to `active`,
   `POST /plan/cancel` discards the proposal and leaves the active plan alone,
   and a second confirm returns `409`.
5. **One read model** — `GET /state` feeds every card, timeline node, deadline
   and badge in the UI; the page shows no sample data as if it were real.
6. **Honesty rails** — no key, an LLM error or a quota miss falls back for
   that turn and the UI shows a visible *Fallback mode · quick reply* badge;
   errors are friendly strings, never a stack trace.

## Track: Alexa+ simulated experience (honest statement)

This project contains a **web-app simulation** of a voice-first Alexa+ style
interaction. A clearly labelled **"Alexa+ (simulated)"** toggle in the chat
header enables a wake phrase — `Alexa, ask CampusPilot to ...` — which is
stripped before the remaining text is sent through the existing `/chat`
endpoint; the reply is spoken with the browser's `speechSynthesis` and shown
as a normal agent card with a listening-ring animation. Text input keeps
working alongside it.

**What it is not:** it is not connected to any real Alexa device or service,
not an Amazon product, and not an MCP integration; no Amazon logos, trademarks
or assets are used. Source lives in `frontend/campuspilot_ui.html` and is
documented in `docs/alexa_simulation.md`.

## Screenshots / media (TEAM TO FILL)

- TEAM TO FILL: dashboard with real tasks + deadline countdown
- TEAM TO FILL: diff modal before confirm (proposal not saved)
- TEAM TO FILL: Alexa+ (simulated) bar with the disclaimer visible
- TEAM TO FILL: optional 20–30 s screen recording of the wake phrase

## Limitations (honest)

- **Simulation only** for Alexa+ — browser Web Speech API (Chrome/Edge,
  microphone permission); no wake-word detection while idle, the phrase is
  parsed after you speak or type it.
- **Gemini free-tier quota** can throttle live turns; quick mode takes over
  for that turn and says so. With no key at all the app is fully offline.
- **One seeded demo student**, sessions are in-memory cookies (two-hour idle
  drop) — no accounts, no auth, no multi-user isolation.
- The daily study-hours target is a UI preference only; check-ins are not
  linked to a task id (the API contract does not accept one).
- TEAM TO FILL: anything you hit during your own testing (below).

## Built with

FastAPI, Google Gemini (`google-genai`), SQLite, Python 3.11+, vanilla
HTML/JS single-file UI (Tailwind + lucide via CDN), Web Speech API
(`SpeechRecognition` / `speechSynthesis`), pytest.

## Product Feedback — TEAM TO FILL: what actually happened

Answer these only from your own runs. If something was not tested, write
"not tested".

### Gemini / `google-genai`
- Which model did you use, and did a live call ever succeed for you?
  **TEAM TO FILL**
- Did you hit free-tier quota (429 / `resource_exhausted`) during a demo?
  What did the fallback notice say? **TEAM TO FILL**
- What did the model get wrong that the validators caught?
  **TEAM TO FILL**

### FastAPI
- Any request that returned an unexpected status (400/409/500) while you were
  building the flow? **TEAM TO FILL**
- Did same-origin cookie sessions behave as expected behind your launcher?
  **TEAM TO FILL**

### SQLite
- Did you ever inspect the `plans` table mid-demo (`status`:
  `proposed`/`active`/`superseded`/`cancelled`)? What did you see?
  **TEAM TO FILL**
- Any WAL/locking trouble on Windows while the server was running?
  **TEAM TO FILL**

### Web Speech API (SpeechRecognition / speechSynthesis)
- Which browser did you demo in, and did the microphone work first try?
  **TEAM TO FILL**
- Was the spoken reply audible/understandable in the room? Any timing issues
  with the auto-send of a final utterance? **TEAM TO FILL**
- Did anyone try the wake phrase with a different accent or wording? What was
  stripped and what was not? **TEAM TO FILL**

## License

MIT — `LICENSE` at the repo root. **TEAM TO FILL: approve the copyright line
(`<TEAM NAME - I WILL CONFIRM>`) before submitting.**

## Team

- TEAM TO FILL — name, role, contact
- TEAM TO FILL — name, role, contact
