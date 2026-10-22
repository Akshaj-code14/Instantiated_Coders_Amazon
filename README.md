# CampusPilot — AI Academic Co-Pilot

One FastAPI process serves the single-file UI and the JSON API from the same
origin, talks to a local SQLite database, and routes chat through an agent
whose every write goes through validated tools. With no API key the agent runs
in **quick mode** (deterministic, rule-based) against the same real backend, so
a demo never depends on the network.

> **Hackathon track — "Alexa+ simulated experience":** this repo contains a
> clearly labelled *simulation* of a voice-first Alexa+ style interaction: a
> toggle called **"Alexa+ (simulated)"** in the chat header, a wake phrase
> `Alexa, ask CampusPilot to ...` that is stripped before the request reaches
> `/chat`, replies spoken with the browser's `speechSynthesis`, and an
> always-visible disclaimer. It is a web-app simulation using the browser Web
> Speech API only — **not** a real Alexa skill, not an Amazon service, and not
> an MCP integration. See `docs/alexa_simulation.md`.

```
Browser ──► FastAPI (backend/server.py) ──► Agent (backend/agent) ──► Backend (backend/services) ──► SQLite
                     │                                    ▲
                     └────────── GET /state read model ───┘
```

## Features that work today (verified in this repo)

- Dashboard, task cards, deadline countdown, day timeline and conflicts — all
  rendered from `GET /state` (no sample cards are shown as real data).
- Chat agent with 12 validated tools: plan, replan, deadlines, schedule, free
  slots, update/create task, notices, check-in, wellbeing.
- Plan lifecycle: a proposal is **never** saved by chat; `/plan/save` is the
  only path to an active plan, `/plan/cancel` leaves the active plan intact,
  a second confirm is rejected with `409`, and everything survives a server
  restart (same SQLite file).
- Task actions (done / postpone / duration), study check-ins, notices.
- **Alexa+ (simulated)** voice mode with wake-phrase stripping, spoken replies
  and a listening ring (`docs/alexa_simulation.md`).
- Visible **"Fallback mode · quick reply"** badge whenever there is no key or
  the LLM call fails; friendly error text, never a stack trace.
- Works fully offline in quick mode (`/health` reports `"llm": "offline"`).

## Planned / not included (honest list)

- **Live AI** requires your own `GEMINI_API_KEY`; the Gemini **free-tier quota**
  can throttle a demo, which is why quick mode is the default path.
- **No real Alexa / Amazon / MCP integration** — simulation only (see above).
- No accounts or auth: sessions are per-browser cookies held in memory
  (dropped after two hours), one seeded demo student.
- The daily study-hours target is a UI preference only; it is not a column in
  the preferences table.
- Check-ins record status/mood and are not linked to a task id (the API
  contract does not accept one).

## Setup

Windows (Git Bash / PowerShell):

```bash
cd campuspilot
python -m venv .venv
. .venv/Scripts/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env              # PowerShell: copy .env.example .env
```

macOS / Linux:

```bash
cd campuspilot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Environment variables (`.env`)

| Variable | Meaning | With no / empty value |
| --- | --- | --- |
| `GEMINI_API_KEY` | Enables live LLM turns. | **The app runs in quick mode**: deterministic rule-based replies, `/health` → `"llm": "offline"`, UI shows the fallback badge. Nothing else changes. |
| `GEMINI_MODEL` | Model id when a key is set. | `gemini-2.5-flash` |
| `CAMPUSPILOT_DB` | SQLite file path. | `data/campuspilot.db` next to `backend/` |
| `DEMO_STUDENT_ID` | Seeded student used by the demo session. | `student_demo` |
| `PORT` | Only used if you pass it to your own launcher. | `8000` |

`.env.example` contains placeholders only. `.env` and `*.db*` are git-ignored.

## Run

```bash
. .venv/Scripts/activate           # or .venv/bin/activate
cd backend
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. The first request creates the tables and seeds
the demo student (tasks, deadlines, timetable, notices) with dates relative to
*today*, so the demo never goes stale. Optional explicit seed, from `backend/`:

```bash
python -m services.seed_data
```

Health check (this is what the demo script uses):

```bash
curl http://127.0.0.1:8000/health     # {"status":"ok","llm":"offline","db":"ok"}
curl http://127.0.0.1:8000/state      # profile, tasks, timeline, plans, conflicts
```

## Tests

```bash
python -m pytest -q                  # from the repo root
```

The suite (backend contract, API, agent guardrails, seed, e2e, manual harness)
runs entirely offline against a throwaway SQLite file — it never calls the LLM.

- Full dev install (`requirements.txt` + `requirements-manual.txt`, Python
  **3.11**): **138 passed**.
- Fresh clone with `requirements.txt` only (Python **3.14**): **127 passed,
  1 skipped** — the Streamlit manual harness is an optional extra, so its file
  skips instead of breaking collection.

Both runs were executed from a fresh `git clone` in a clean virtualenv.

## Demo prompts

1. `Plan my next two days` → proposal + diff modal → **Confirm and Save**.
2. `what are my deadlines?` → deadline tool call and list.
3. `I lost two hours today` → replan proposal with a `lost 120 minutes` diff;
   **Cancel** first to prove the active plan is untouched, then confirm.
4. `Add a 15-min coffee break` → reserves the time by replanning and says so.
5. `mark my DBMS assignment complete` → task status changes, counts update.
6. `save my plan` (with nothing pending) → "nothing to save", no write.
7. Alexa+ simulated: enable **Alexa+ (simulated)**, click **Talk** (or type)
   `Alexa, ask CampusPilot to plan my next two days`.

Timed walkthrough: `docs/demo_script.md`. Repeatable reset:
`scripts/reset_demo.ps1` (Windows) / `scripts/reset_demo.sh` (macOS/Linux).

## Limitations

- Gemini **free-tier quota** can return 429s during a live-AI demo; the app
  then falls back for that turn and says so ("quick mode handled this turn").
- Speech recognition needs Chrome/Edge and microphone permission; without it the
  UI says so and typing works as normal.
- Single demo student, no multi-user isolation or authentication.

## License

MIT — see `LICENSE`. **The copyright line is a placeholder
(`<TEAM NAME - I WILL CONFIRM>`): the team must approve the final name before
submission.**

## Team

| Role | Name | Contact |
| --- | --- | --- |
| TEAM TO FILL | TEAM TO FILL | TEAM TO FILL |
| TEAM TO FILL | TEAM TO FILL | TEAM TO FILL |
| TEAM TO FILL | TEAM TO FILL | TEAM TO FILL |

Change log for the code-freeze pass lives in `FIX_REPORT.md`.
