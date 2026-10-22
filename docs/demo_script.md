# CampusPilot — demo script (2:40, hard stop under 2:45)

Total: **160 s**. Say the timings out loud only if asked; they are for the
presenter. Everything below is a real click or a real prompt — no staged data.

**Before you start (30 s, not counted):**
`. .venv/Scripts/activate && cd backend && python -m uvicorn server:app --host 127.0.0.1 --port 8000`
Open <http://127.0.0.1:8000/> — dashboard fills from SQLite.
Optional repeat: `scripts/reset_demo.ps1` (Windows) / `scripts/reset_demo.sh`
(macOS/Linux) — ask the presenter before running; it deletes only the demo DB.

---

## 0:00–0:15 — Hook (15 s)

- Open on the **dashboard**: task cards, deadline countdown, timeline, conflict
  badge. Say: *"This is a co-pilot that plans around real deadlines from a
  local database — and it never writes anything without me confirming."*
- Click nothing yet; let the page settle.

## 0:15–0:35 — Alexa+ (simulated) mode + track statement (20 s)

- Click the **AI Chat & Co-Pilot** tab (top tab bar).
- Point at the button **"Alexa+ (simulated)"** in the chat header, click it
  once so it lights up (`aria-pressed=true`), and read the bar under the
  header: *"Simulated Alexa+ experience. Not connected to a real Alexa
  service."*
- Say: *"Our track is the Alexa+ simulated experience: a wake phrase in the
  browser, using the Web Speech API — not a real Alexa skill."*
- Click **Talk** if the judge wants to hear it (Chrome/Edge + mic permission);
  otherwise the mic is optional — typing the wake phrase proves the same parser.

## 0:35–1:15 — "Plan my next two days" (40 s)

- In the chat box type exactly:
  `Alexa, ask CampusPilot to plan my next two days`
  and press Enter. (Without Alexa mode on, the plain prompt works too:
  `Plan my next two days`.)
- Point at what happens: the wake phrase is gone from your own bubble, tool
  chips appear (*Checked deadlines / Checked your schedule / Found free time /
  Created plan proposal*), and the reply ends with **"Nothing is saved yet —
  press Confirm to save this plan."**
- The **Review Proposed Plan Changes** modal opens with the diff. Say:
  *"Nothing is in the database yet — the proposal is `proposed`, not
  `active`."*

## 1:15–1:50 — Conflict + confirm (35 s)

- In the modal/reply, point at the **conflict warning** (DBMS Lab due 11:00,
  only 30 min after the lab session ends), the tool chip
  **"Found free time (1 conflict)"**, and the sidebar/Day Timeline badge
  showing **"1 conflict"**.
- Click **Confirm and Save** → success panel *"New Plan Activated! Saved to
  your CampusPilot schedule in SQLite."*
- Say: *"/plan/save is the only path to an active plan."*

## 1:50–2:15 — "I lost two hours" replan diff (25 s)

- Type: `I lost two hours today` and press Enter.
- A **new** proposal + diff appears (blocks move, `change_reason: lost 120
  minutes`). Point at it, then click **Cancel** in the modal.
- Say: *"Cancelled — the plan I just saved is untouched."* The sidebar still
  shows **Active plan synced**.

## 2:15–2:30 — Refresh shows persistence (15 s)

- Press **F5 / reload**. The dashboard comes back from SQLite with
  **Active plan synced** and *"An active plan is saved"* in the summary.
- Say: *"Restart the server, refresh — it's still there."*

## 2:30–2:40 — Close (10 s)

- Back to the dashboard, one line: *"Quick mode runs offline, live Gemini is
  optional, and every write is confirmed."*
- Stop. Hard cap 2:45.

---

### Exact prompts used (copy/paste)

```
Alexa, ask CampusPilot to plan my next two days
I lost two hours today
```

Backup if a judge interrupts: `what are my deadlines?`,
`mark my DBMS assignment complete`.

### If something looks wrong

- Fallback badge showing? Good — say *"no key or a quota miss; quick mode took
  the turn"*, it is honest, not a failure.
- Mic unavailable? Type the wake phrase; the same parser runs.
- Reset between judges: stop the server, run the reset script, start again.
