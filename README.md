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
| `LIVE_CALLS_PER_DAY` | Real Gemini requests allowed per day (all visitors). | `15` |
| `LIVE_CALLS_PER_IP_PER_HOUR` | Real Gemini requests allowed per visitor per hour. | `5` |
| `DEMO_PASSWORD` | Optional password gate for a public deployment. | **Unset = open demo** (no gate) |
| `PORT` | Port the server binds when launched by a host. | `8000` |

`.env.example` contains placeholders only. `.env` and `*.db*` are git-ignored.

## Live demo

**URL:** <https://campus-pilot-ss2y.onrender.com/> — TEAM Instantiated Coders once the host gives you one.

The live instance runs the same code in quick mode, key is configured.
It is a **shared single demo student**: every visitor sees and edits the same
plans, so use the **Reset demo** link in the header (or `POST /demo/reset`,
max 3 per hour per IP) to put the seeded data back.

Expect the **first load to take ~1 minute**: free hosts sleep idle services and
the container has to boot, install nothing (deps are baked in) and seed SQLite.
Live AI is **rate-limited** (`LIVE_CALLS_PER_DAY=15`, 5 per visitor per hour);
when a cap is hit the app answers from quick mode with the usual fallback badge
and the line *"Live AI limit reached; using fallback mode."* — never an error.

### Run the container locally

```bash
docker build -t campuspilot .
docker run --rm -p 8000:8000 -e PORT=8000 campuspilot
# with a key (never commit it):
docker run --rm -p 8000:8000 -e PORT=8000 -e GEMINI_API_KEY="$GEMINI_API_KEY" campuspilot
```

`.dockerignore` keeps `.env`, `*.db*`, `.venv*`, `.git` and `__pycache__` out
of the image.

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
curl http://127.0.0.1:8000/health     # {"status":"ok","llm":"offline","db":"ok","live_calls_remaining":15}
curl http://127.0.0.1:8000/state      # profile, tasks, timeline, plans, conflicts
```

## Tests

```bash
pip install -r requirements-dev.txt  # runtime + pytest, httpx, Streamlit harness
python -m pytest -q                  # from the repo root
```

The suite (backend contract, API, agent guardrails, seed, e2e, manual harness)
runs entirely offline against a throwaway SQLite file — it never calls the LLM.

- Full dev install (`requirements-dev.txt`, Python **3.11**): **156 passed**
  (140 before the deploy work + 16 deploy tests).
- Runtime only (`requirements.txt`, Python **3.14**): **145 passed, 1 skipped**
  (129 + the 16 deploy tests); the Streamlit manual harness is an optional
  extra, so its file skips instead of breaking collection.

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
- Single demo student, no multi-user isolation: every visitor shares it (use
  **Reset demo** between judges).
- Live-AI counters are in process memory, so a restart (or a second worker)
  starts the daily budget over. Run one worker, as the provided image does.
- Public deployments throttle `/chat` (10/min per IP, 2000-char messages,
  ~16 KB bodies); `/health` also answers when a `DEMO_PASSWORD` gate is set.

## License

MIT — see [`LICENSE`](LICENSE). Copyright (c) 2026 **Instantiated Coders**.

## Team : Instantiated Coders

| Role | Name |
| --- | --- | --- |
| UI/UX & Frontend Engineer | Abhinav Jain | 
| Backend & Database Engineer | Akshaj Jana | 
| Agent & LLM Engineer | S. Ariyan Minaxi | 
| Integration, QA & Demo Engineer | Satyaj Srivastava

Change log for the code-freeze and deploy passes lives in
[`docs/FIX_REPORT.md`](docs/FIX_REPORT.md).
