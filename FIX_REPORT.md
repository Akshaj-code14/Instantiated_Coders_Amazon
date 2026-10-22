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

---

## Item 3 — REPO READINESS

### `LICENSE` (new at root)
- **Why:** submission requires an MIT license with a team-approved copyright.
- **Before:** no license file. **After:** standard MIT text with
  `Copyright (c) 2026 <TEAM NAME - I WILL CONFIRM>`.
- **⚠ TEAM ACTION:** the team must approve/replace that placeholder before
  submission. This file is not final until they do.

### `.gitignore`
- **Why:** `*.db` / `*.db-wal` / `*.db-shm` were only ignored under
  `backend/data/`; a database written anywhere else (e.g. a temp path or a
  copy) could be staged by accident.
- **Before:** `.env`, `.venv*/`, `__pycache__/`, `backend/data/*.db*`.
- **After:** same, plus blanket `*.db`, `*.db-wal`, `*.db-shm` (marked
  `# FIX:`).

### `.env.example`
- Already present with placeholders only (`GEMINI_API_KEY=` empty, commented
  notes, no real values). Unchanged.

### `README.md` (rewritten)
- **Why:** the old README described a different layout (`cd campuspilot`) and
  had not been proven.
- **Before:** stale paths, no track statement, no honest planned-vs-working
  split, no team placeholders.
 **After:** pitch, honest Alexa+ simulated track statement, features that work
  vs planned, architecture (`Browser → FastAPI → Agent → Backend → SQLite`),
  Windows + Mac/Linux setup, env-var table (what happens with no Gemini key),
  run, tests, demo prompts, limitations (Gemini free-tier quota), license with
  the approval note, team placeholders. Every command in it is one that was
  actually run (see the fresh-clone proof below).

### Secret / artifact scan (ran)
- `git ls-files | grep -Ei "\.env|\.db|screenshot"` → only `.env.example`
  (placeholders). No `.env`, `.db`, WAL/SHM or screenshot is tracked.
- Key patterns (`AIza…`, `sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, `xox…`,
  `glpat-`, PEM headers) scanned over (a) tracked files, (b) the whole working
  tree including untracked files, (c) **every commit in git history** → **zero
  matches**.
- Local-only note (untracked, git-ignored, values not printed):
  `.env:1` has a non-empty `GEMINI_API_KEY` (53 chars, does **not** match any
  scanned key pattern); `backend/.env:1` is empty. `.env` and `backend/.env`
  are both ignored, so nothing is committed.

### Stray files — LISTED FOR REVIEW ONLY, nothing deleted
- `backend/.env` — local env copy (ignored, empty key).
- `backend/data/campuspilot.db` (+ WAL/SHM if present) — local runtime DB (ignored).
- `screenshot-dashboard-live.png`, `screenshot-grid.png`, `screenshot-pomodoro.png`,
  `screenshot-pomodoro2.png` — local captures (ignored by `screenshot-*.png`).
- `tests/` (repo root) — contains only `__pycache__`, a leftover from before
  the suite moved to `backend/tests` (`pytest.ini` points at `backend/tests`).
- `requirements-agent.txt`, `requirements-manual.txt` — extra requirement sets
  for the standalone agent/manual scripts; tracked, kept.
- `backend/agent/manual_app.py`, `backend/agent/mock_backend.py`,
  `backend/agent/demo.py`, `backend/agent/live_check.py` — standalone/demo
  entry points covered by their own tests; tracked, kept.
- No `Backend_old/` and no `check_db.py` exist in the tree, and `git log
  --all --diff-filter=A` shows they were **never** committed.

### Fresh-clone proof (ran, from a temp directory)

```
git clone -b submission-fixes . "$TEMP/campuspilot-proof"   # HEAD 0e8b9d9
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt  # exit 0
cp .env.example .env
.venv/Scripts/python.exe -m pytest -q                        # 127 passed, 1 skipped
cd backend && ../.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```
- `/health` → `{"status":"ok","llm":"offline","db":"ok"}` (offline, empty key),
  `/state` → 6 tasks, `/` → `200 text/html`, `python -m services.seed_data` →
  idempotent (`seeded: False … already contains students`).
- **The first proof run FAILED** (this is why there is a second item-3 commit):
  `backend/tests/test_manual_app.py` crashed pytest *collection* on a clean
  install because `requirements.txt` keeps Streamlit optional. Fixed with
  `pytest.importorskip("streamlit")` before the `agent.manual_app` import.
  Re-proved: 127 passed / 1 skipped on the clean clone; **138 passed** in the
  full dev venv (Python 3.11) where `requirements-manual.txt` is installed.

---

## Item 4 — TESTS

- **Baseline:** `python -m pytest -q` → **138 passed** before any test edit
  (offline: `backend/tests/conftest.py` forces `GEMINI_API_KEY=""`; Python
  **3.11.15** in `.venv311`).
- **Real failure found & fixed (minimal):** a clean install could not even
  collect `backend/tests/test_manual_app.py` (`ModuleNotFoundError: streamlit`
  — Streamlit is an optional extra). Fixed in the item-3 proof commit with
  `pytest.importorskip("streamlit")` ahead of the `agent.manual_app` import;
  no assertions changed, nothing skipped in the full dev install.
- **New tests added — 2 (limit was 3):**
  1. `backend/tests/test_alexa_wake.py::test_alexa_wake_phrase_is_stripped_before_chat`
     — extracts `ALEXA_WAKE` from the shipped `frontend/campuspilot_ui.html`,
     asserts `"Alexa, ask CampusPilot to plan my next two days"` →
     `"plan my next two days"`, that a plain message is untouched, that
     `sendMessage` actually applies the strip, and that the honesty disclaimer
     is present.
  2. `backend/tests/test_api.py::test_cancel_keeps_an_existing_active_plan_intact`
     — save a plan, propose a second one, cancel it, assert the active
     `plan_id` is unchanged, pending is cleared and exactly 1 active row
     remains.
- **Coverage check first:** "save with nothing pending is rejected" already
  existed (`test_save_is_server_side_and_guarded` asserts `409`), so no third
  test was needed.
- **Suite after item 4:** `python -m pytest -q` → **140 passed**.

### `backend/services/seed_data.py` — date-dependent failure (found on re-run)

- **Why:** re-running the whole suite on **Friday 2026-10-23** gave
  `1 failed, 139 passed`: `test_seed.py::test_seeded_deadlines_are_plan_able`
  saw **4** plan-able deadlines instead of **5**. Root cause is in the seed, not
  the test: `quiz_due = _next_weekday(today, 4)` is "next Friday", which on a
  Friday is **7 days + 14:00** away, i.e. just past the 7-day horizon the agent
  asks the backend for (`days_ahead or 7`), so the Networks Quiz silently
  disappeared from the demo plan. `java_due` (Sunday) has the same edge on a
  Sunday. Nothing in items 1–3 touched this; it is a pre-existing
  weekday-dependent bug that only shows up on one day of the week.
- **Before:** `quiz_due`/`java_due` could sit 7 days + clock-time out;
  `get_upcoming_deadlines(days_ahead=7)` returned 4 rows on a Friday.
- **After:** if the resolved day is more than 6 days out, the deadline is
  re-seeded to `today+3` (quiz, 14:00) / `today+5` (Java, 23:59) so every seeded
  deadline is always inside the window the agent actually reads. Seed-only
  change, 4 added lines + comment, no test weakened, no assertion changed.
- **Ran after the fix:** `python -m pytest -q` → **140 passed**; a freshly seeded
  DB (`CAMPUSPILOT_DB=$TEMP/cp_seedcheck/friday.db`) now returns **5** rows from
  `get_upcoming_deadlines(days_ahead=7)` (DBMS 24 Oct 11:00, Discrete Math
  24 Oct 18:00, OS 25 Oct 23:59, Java 25 Oct 23:59, Networks Quiz 26 Oct 14:00).
- **Also re-ran after the fix (session re-verification, same day):**
  - live HTTP on a fresh offline DB (`127.0.0.1:8013`): 6 seeded tasks, proposal
    → **0** active plans, confirm → **1** active plan and the same `plan_id`,
    second confirm → **409** with still **1** row, `plan_id` of a *different*
    proposal → **400** (`{"error":"That proposal is no longer the pending
    plan."}`), saving when nothing is pending → **409**, cancel → **200** and the
    active `plan_id` is unchanged, "I lost two hours" → replan proposal with the
    active plan still untouched, `/health` = `{"status":"ok","llm":"offline",
    "db":"ok"}`; after killing and restarting the server, `/state.active_plan`
    is the *same* `plan_id` with its 2 blocks and `pending_plan: null`.
    One line of my own check script was wrong, not the app: it expected `400`
    when saving a foreign `plan_id` while **nothing** was pending — the server
    correctly answers `409` there, so 23/24 of my own assertions passed and the
    odd one was re-checked by hand (result above).
  - fresh clone re-pulled to the final commit (`$TEMP/campuspilot-proof`, Python
    **3.14** venv from `requirements.txt` only): `129 passed, 1 skipped` — the
    skip is `test_manual_app.py` (11 tests, the optional Streamlit harness in
    `requirements-manual.txt`), i.e. 129 + 11 = the 140 seen in the dev venv;
    nothing else is silently missing. Offline server from the clone: `/health`
    ok, `/state` profile *Aditya Verma* + 6 tasks + 4 timeline + 1 conflict,
    `GET /` → `200 text/html`.

---

## Item 5 — SUBMISSION AIDS (docs only)

### `docs/demo_script.md` (new)
- **Why:** a repeatable, timed demo under the 2:45 hard stop.
- **Before:** did not exist. **After:** 160 s (2:40) script — hook 15 s,
  Alexa+ simulated + track statement 20 s, `Alexa, ask CampusPilot to plan my
  next two days` 40 s, conflict + **Confirm and Save** 35 s,
  `I lost two hours today` diff + **Cancel** 25 s, refresh showing
  *Active plan synced* 15 s, close 10 s — with exact prompts and click targets.
- Every quoted UI string was checked against the shipped files (tab is
  `AI Chat & Co-Pilot`, chip is `Found free time (1 conflict)`, modal is
  `Review Proposed Plan Changes`, success is `New Plan Activated!`, reply is
  `Nothing is saved yet — press Confirm to save this plan.`).

### `scripts/reset_demo.ps1` + `scripts/reset_demo.sh` (new)
- **Why:** the demo must be repeatable between judges.
- **Before:** did not exist. **After:** delete **only**
  `backend/data/campuspilot.db` (+ `-wal`, `-shm`) and reseed with
  `python -m services.seed_data`; both scripts say to stop the server first
  and touch nothing else (no code, no `.env`, no git).
- **Not executed — by design:** the brief says ask before running, so they are
  **UNVERIFIED as runs**. Verified instead:
  `bash -n scripts/reset_demo.sh` → syntax OK; Windows PowerShell parser
  (`[System.Management.Automation.Language.Parser]::ParseInput`) on
  `reset_demo.ps1` → **0 errors**.

### `docs/devpost_draft.md` (new)
- **Why:** submission text without invented claims.
- **Before:** did not exist. **After:** project description, problem, how it
  works, honest Alexa+ simulated track statement (what it is *not*), honest
  limitations, and a **Product Feedback** skeleton for Gemini / FastAPI /
  SQLite / Web Speech API where every entry is a
  **"TEAM TO FILL: what actually happened"** prompt. No experiences, numbers
  or metrics were invented; screenshots/media are marked TEAM TO FILL.

**Suite after item 5 (docs only):** `python -m pytest -q` → **140 passed**.

---

## Final status

| Item | Status | Evidence |
| --- | --- | --- |
| 1 — Alexa+ simulated track | **PASS** | Live browser run: toggle → placeholder/status change; typed `Alexa, ask CampusPilot to plan my next two days` and intercepted the fetch body → `{"message":"plan my next two days"}` (wake stripped before display *and* before `/chat`); reply card rendered, `speechSynthesis.speaking === true`; ring computed style `alexaRingPulse 1.35s`; disclaimer string present. Source + `docs/alexa_simulation.md`; unit test `test_alexa_wake.py`. |
| 2 — real path | **PASS** | `USE_MOCK = false`, `API_BASE = window.location.origin`; HTTP on a fresh offline DB: proposal → **0** saved, confirm → **1** active, second confirm → `409` (still 1), saving a *different* proposal's id → `400`, saving with nothing pending → `409`, cancel → active `plan_id` unchanged, restart → same `plan_id` + `pending_plan: null`; browser refresh shows *Active plan synced*; with the server stopped, Confirm shows an error toast and **no** success; fallback badge verified offline **and** on a live-key-that-fails server (`used_fallback: true`, friendly notice); the hard-coded cards/diff rows in the markup are cleared on load, so no sample string reaches the DOM. |
| 3 — repo readiness | **PASS** (team must approve the LICENSE line) | `LICENSE` (MIT, `<TEAM NAME - I WILL CONFIRM>`), `.gitignore` now ignores `.env`/`*.db*`/`.venv*`/`__pycache__`, `git ls-files` shows only `.env.example`, secret scan over tree + **all commits** → 0 matches, stray files listed (nothing deleted), README rewritten from commands actually run, fresh clone in `$TEMP/campuspilot-proof` → venv → install → `127 passed, 1 skipped` at the time (re-pulled to the final commit later: `129 passed, 1 skipped`) → server `/health` ok, `/state` 6 tasks, `/` 200 text/html, seed idempotent. |
| 4 — tests | **PASS** | Baseline **138 passed**; two real failures fixed, both minimal (`streamlit` collection crash on a clean install via `pytest.importorskip`; Friday/Sunday-only seed horizon bug that dropped a deadline from the 7-day plan); 2 new tests added (limit 3); final **140 passed** offline on Python 3.11.15, and **129 passed, 1 skipped** in the fresh clone on Python 3.14. |
| 5 — submission aids | **PASS** (docs) / **UNVERIFIED** (reset scripts not run) | `docs/demo_script.md` (2:40), `docs/devpost_draft.md` (no invented claims), `scripts/reset_demo.ps1` + `.sh` created — **syntax-checked only** (`bash -n` OK; PowerShell parser 0 errors) and deliberately **not executed**, per "ask before running". |

### Exact commands run

```bash
# branch safety
git tag pre-freeze-backup && git checkout -b submission-fixes

# test suite (before every item, after every item)
./.venv311/Scripts/python.exe -m pytest -q          # 138 -> 138 -> 138 -> 140 -> 140

# servers used for verification (temp DBs, offline)
cd backend
GEMINI_API_KEY="" CAMPUSPILOT_DB="$TEMP/cp_freeze/ui.db"  ../.venv311/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8010
GEMINI_API_KEY="" CAMPUSPILOT_DB="$TEMP/cp_freeze/http.db" ../.venv311/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8011
GEMINI_API_KEY="bogus-test-key-for-fallback-check" CAMPUSPILOT_DB="$TEMP/cp_freeze/liveish.db" ../.venv311/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8012
curl http://127.0.0.1:8011/health   # {"status":"ok","llm":"offline","db":"ok"}
curl http://127.0.0.1:8011/state
# plus an inline python/urllib script driving /chat -> /plan/save -> /plan/cancel
# against http://127.0.0.1:8011 while counting `plans.status='active'` in SQLite.

# fresh-clone proof
git clone -b submission-fixes . "$TEMP/campuspilot-proof"
cd "$TEMP/campuspilot-proof" && python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python.exe -m pytest -q                # 127 passed, 1 skipped (Py 3.14)
cd backend && ../.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health && curl http://127.0.0.1:8000/state
../.venv/Scripts/python.exe -m services.seed_data    # idempotent

# re-verification after the 2026-10-23 Friday seed fix
./.venv311/Scripts/python.exe -m pytest -q          # 140 passed
cd backend && CAMPUSPILOT_DB="$TEMP/cp_seedcheck/friday.db" ../.venv311/Scripts/python.exe -c "..."  # 5 deadlines
# live HTTP check (my script, $TEMP/cp_freeze/http_check.py) against port 8013
#   + kill/restart of the same server, then GET /state
cd "$TEMP/campuspilot-proof" && git fetch origin && git reset --hard origin/submission-fixes
./.venv/Scripts/python.exe -m pytest -q             # 129 passed, 1 skipped (Py 3.14)
cd backend && ../.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8014
curl http://127.0.0.1:8014/health && curl http://127.0.0.1:8014/state && curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8014/

# submission aids checks
bash -n scripts/reset_demo.sh                       # syntax OK (not executed)
# PowerShell Parser::ParseInput on reset_demo.ps1  -> 0 errors (not executed)
```

### Remaining biggest risk

**The live-AI path is only verified with an invalid key.** No real Gemini key
was used (and none may be printed), so a genuine `GEMINI_API_KEY` during
judging could behave differently — free-tier quota (`429` / `resource_exhausted`)
or a model-side change mid-demo. Mitigation that *was* verified: every failed
turn falls back to quick mode, the UI flips its **Fallback mode · quick reply**
badge, and the notice text stays friendly. Run the demo with the key removed
to be deterministic.
