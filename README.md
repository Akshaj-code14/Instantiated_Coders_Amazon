# CampusPilot — AI Academic Co-Pilot

One FastAPI process serves the single-file UI and the JSON API from the same
origin, talks to a local SQLite database, and routes chat through an LLM agent
whose every write goes through validated tools. Everything works offline: with
no API key the agent runs in **quick mode** (deterministic, rule-based) against
the same real backend, so a demo never depends on the network.

```
browser ──► FastAPI (server.py) ──► agent (LLM or quick mode) ──► backend ──► SQLite
                    │                                              ▲
                    └────────── /state read model ─────────────────┘
```

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate    # Windows (bash)
pip install -r requirements.txt

cp .env.example .env          # optional; add GEMINI_API_KEY for live AI
cd backend
python -m services.seed_data  # optional; the server also seeds on first run
uvicorn server:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. The first request creates the tables and seeds
the demo student (tasks, timetable, notices) with dates relative to *today*, so
the demo never goes stale.

`/health` returns `{status, llm, db}`, where `llm` is `live` when a key is
configured and `offline` otherwise.

## Layout

| Path | Role |
| --- | --- |
| `frontend/campuspilot_ui.html` | The whole front end: dashboard, day timeline, chat, check-in, plus the kinetic-grid background, magnetic cursor, floating Pomodoro, radial quick menu and settings drawer. |
| `backend/server.py` | FastAPI app: serves the UI, the JSON API, and the session cookie. |
| `backend/integration/wiring.py` | Session registry, per-session agent, `/state` read model, pending-plan lifecycle. |
| `backend/integration/mappers.py` | Agent tool results → API payloads (plans, diffs, tool chips). |
| `backend/integration/api_models.py` | Pydantic request/response schemas. |
| `backend/agent/` | LLM controller, generated tool registry, validators, quick-mode fallback router, prompt. |
| `backend/services/` | SQLite schema and domain services (tasks, planner, replanner, notices, check-ins, wellbeing). |
| `backend/services/seed_data.py` | Relative-date demo seed; idempotent (skips when data exists). |
| `backend/data/` | SQLite database (created on first run) and seed CSVs. |
| `backend/tests/` | Backend contract, API, agent guardrails, seed and end-to-end tests. |

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The UI. |
| `GET` | `/health` | Liveness + LLM status. |
| `GET` | `/state` | Single read model: profile, preferences, tasks, timeline, active plan, pending plan, counts, check-in, notices, conflicts. |
| `POST` | `/chat` | One agent turn: `{message, language}` → `{reply, tool_calls, proposed_plan, ...}`. |
| `POST` | `/plan/save` | Confirm the pending proposal (this is the only way a plan becomes active). |
| `POST` | `/plan/cancel` | Discard the pending proposal; the active plan is untouched. |
| `POST` | `/tasks/complete` | Mark a task done / not done. |
| `POST` | `/tasks/postpone` | Push a task back by one day. |
| `POST` | `/tasks/duration` | Change a task's estimated minutes. |
| `POST` | `/checkin` | Record a study check-in (completion status + mood). |

Sessions are per-browser cookies held in memory; an idle session is dropped
after two hours.

## Agent tools

`get_student_context`, `get_upcoming_deadlines`, `get_schedule`,
`find_free_slots`, `create_plan`, `save_plan`, `update_task`, `create_task`,
`replan_schedule`, `search_notices`, `log_checkin`, `get_wellbeing_support`.

Declarations are generated from the Pydantic argument models, so the schema the
model sees cannot drift from what we validate.

## Guarantees worth knowing before changing this code

- **Nothing is written without confirmation.** `create_plan` and
  `replan_schedule` only insert a `proposed` plan. `/plan/save` is the sole path
  to `active`, and `save_plan` is gated in the registry: dispatch refuses it
  unless the controller recorded a user confirmation for that turn.
- **The model cannot invent identifiers.** `student_id` and
  `confirmed_by_user` are stripped from model-supplied arguments, and every
  `plan_id` / `task_id` / `event_id` must have appeared in an earlier tool
  result in the same session.
- **Cancelling is safe.** `/plan/cancel` marks the proposal `cancelled` and
  leaves the active plan exactly as it was — verified in `tests/test_api.py`.
- **Notices are data, not instructions.** Seeded notices include adversarial
  text; they are summarised, never executed.
- **Distress is handled first.** A crisis screen runs before tool routing and
  routes to `get_wellbeing_support` with real resources rather than a plan.
- **Degrading is normal.** With no key, an LLM error, or a stubborn tool loop,
  the turn falls back to the deterministic quick-mode router, and the reply
  says so rather than pretending.

## Demo script (about five minutes)

1. **Dashboard** — real tasks from SQLite, pending count, completion meter,
   deadline countdown, notice badges.
2. **Chat: "Plan my next two days"** — the agent reads deadlines and free time,
   then proposes blocks. A diff modal shows added / moved / removed. Nothing is
   saved until **Confirm and Save**.
3. **Chat: "I lost two hours today, rebalance"** — capacity drops, blocks move,
   and the diff names what got dropped. **Cancel** proves the active plan is
   untouched, then re-run it and confirm.
4. **Chat: "Add a 15-min coffee break"** — quick mode has no tool for inserting
   a fixed break event, so it reserves the time by replanning and says so; the
   diff shows the reduced capacity. Nothing is claimed that did not happen.
4. **Timeline** — fixed classes, study sprints and physical events on one
   chronological branch, with conflict count.
5. **Check-in** — pick a status and mood; it persists and shows up in `/state`.
6. **Task card** — mark done; the meter and counts update from the server, not
   from local state.

## Tests

```bash
python -m pytest -q          # backend, API, agent, e2e, seed (from the repo root)
```

The suite runs entirely offline against a throwaway SQLite file, so it is safe
in CI and never calls the LLM.
