"""CampusPilot FastAPI server.

Run locally:

    uvicorn server:app --host 127.0.0.1 --port 8000

The server serves the single-file UI at ``/`` and the JSON API on the same
origin, so no CORS is needed and the session cookie stays HttpOnly.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import services.campuspilot_backend as backend
from agent.config import load_config
from agent.live_budget import (
    budget,
    chat_limiter,
    client_ip_from_headers,
    login_limiter,
    reset_limiter,
)
from services.database import get_connection
from services.timeutil import now_ist, to_iso, to_ist
from integration.api_models import (
    ChatRequest,
    ChatResponse,
    CheckinRequest,
    HealthResponse,
    OkResponse,
    PlanCancelRequest,
    PlanSaveRequest,
    PreferencesRequest,
    StateResponse,
    TaskCompleteRequest,
    TaskDurationRequest,
    TaskPostponeRequest,
)
from integration.mappers import map_chat_response
from integration.wiring import (
    IDLE_TIMEOUT_SECONDS,
    SessionRegistry,
    build_state,
    cancel_pending_plan,
    demo_student_id,
    llm_status,
    most_recent_event,
    owned_task,
    pending_conflicts,
    pending_plan_data,
    reset_demo_data,
    startup,
    task_due_at,
    update_preferences,
)

logger = logging.getLogger("campuspilot")
BASE_DIR = Path(__file__).resolve().parent
UI_PATH = BASE_DIR.parent / "frontend" / "campuspilot_ui.html"
COOKIE_NAME = "campuspilot_session"
PORT = int((os.getenv("PORT") or "8000").strip() or "8000")

# FIX (deploy-ready, items 4/6): deploy-only knobs, all env-driven.
MAX_BODY_BYTES = 16_384
DEMO_PASSWORD = (os.getenv("DEMO_PASSWORD") or "").strip()
PASSWORD_COOKIE = "campuspilot_demo"
PASSWORD_COOKIE_AGE = 12 * 60 * 60
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "microphone=(self)",
}
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CampusPilot</title>
<style>body{font-family:system-ui,sans-serif;background:#0b1020;color:#e2e8f0;
min-height:100vh;display:grid;place-items:center;margin:0}form{background:#131a30;
padding:2rem;border-radius:1rem;border:1px solid #26304d;max-width:22rem}
input{width:100%;padding:.7rem;border-radius:.5rem;border:1px solid #33415c;
background:#0b1020;color:#e2e8f0}button{margin-top:.9rem;width:100%;padding:.7rem;
border:0;border-radius:.5rem;background:#4f46e5;color:#fff;font-weight:600}</style>
</head><body><form method="get" action="/">
<h1 style="font-size:1.1rem;margin:0 0 .4rem">CampusPilot demo</h1>
<p style="font-size:.8rem;color:#94a3b8;margin:0 0 1rem">This demo is password
protected. Ask the presenter for the password.</p>
<input type="password" name="demo_password" aria-label="Demo password" autofocus>
<button type="submit">Open demo</button></form></body></html>"""

AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|y|yeah|yep|ok|okay|sure|confirm|confirmed|save it|do it|go ahead)"
    r"[\s.!]*$",
    re.IGNORECASE,
)
LANGUAGE_NAMES = {"te": "Telugu", "hi": "Hindi"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    logger.info("CampusPilot ready on student %s", demo_student_id())
    yield


app = FastAPI(title="CampusPilot", version="1.0.0", lifespan=lifespan)
registry = SessionRegistry()


def _password_digest() -> str:
    return hashlib.sha256(DEMO_PASSWORD.encode("utf-8")).hexdigest()


def _password_ok(request: Request) -> bool:
    """True when the request carries the demo password (or its cookie hash)."""
    presented = (
        request.headers.get("x-demo-password")
        or request.query_params.get("demo_password")
        or request.cookies.get(PASSWORD_COOKIE)
        or ""
    ).strip()
    if not presented or not DEMO_PASSWORD:
        return False
    digest = _password_digest()
    return hmac.compare_digest(presented, DEMO_PASSWORD) or hmac.compare_digest(
        presented, digest
    )


@app.middleware("http")
async def deploy_guard(request: Request, call_next):
    """FIX (deploy-ready, items 4/6): limits, optional gate, safe headers."""
    peer = request.client.host if request.client else None
    request.state.client_ip = client_ip_from_headers(
        peer, request.headers.get("x-forwarded-for")
    )

    # Optional password gate (off unless DEMO_PASSWORD is set); /health is open
    # so the host's health check keeps working.
    if DEMO_PASSWORD and request.url.path != "/health":
        if not _password_ok(request):
            if not login_limiter.allow(request.state.client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"error": "Too many password attempts. Wait a minute."},
                    headers=SECURITY_HEADERS,
                )
            if request.url.path == "/" and request.method == "GET":
                return HTMLResponse(
                    LOGIN_PAGE, status_code=401, headers=SECURITY_HEADERS
                )
            return JSONResponse(
                status_code=401,
                content={"error": "This demo is password protected."},
                headers=SECURITY_HEADERS,
            )

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = MAX_BODY_BYTES + 1
        if declared > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": "That request is too large."},
                headers=SECURITY_HEADERS,
            )

        origin = request.headers.get("origin")
        if origin:
            allowed = {
                f"http://127.0.0.1:{PORT}",
                f"http://localhost:{PORT}",
                str(request.base_url).rstrip("/"),
            }
            if origin.rstrip("/") not in allowed:
                return JSONResponse(
                    status_code=403,
                    content={"error": "Cross-origin request blocked."},
                    headers=SECURITY_HEADERS,
                )

    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # Remember a correct ?demo_password= for the rest of the browsing session;
    # the cookie holds the hash, never the password itself.
    if (
        DEMO_PASSWORD
        and request.query_params.get("demo_password")
        and _password_ok(request)
    ):
        response.set_cookie(
            PASSWORD_COOKIE,
            _password_digest(),
            max_age=PASSWORD_COOKIE_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": (
                "Invalid request. Check the fields and try again."
            )
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(status_code=exc.status_code, content={"error": message})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled API error")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Something went wrong on the server. Nothing was saved."
        },
    )


def get_session(request: Request, response: Response):
    session_id, session = registry.get_or_create(
        request.cookies.get(COOKIE_NAME)
    )
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=IDLE_TIMEOUT_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return session


def _log_traces(student_id: str, traces) -> None:
    for trace in traces:
        try:
            backend.log_tool_call(
                student_id=student_id,
                tool_name=trace.name,
                arguments=trace.args,
                result={
                    "success": trace.success,
                    "short_result": trace.short_result,
                    "reason": trace.reason,
                },
            )
        except Exception:  # logging must never fail a user action
            logger.warning("tool log write failed for %s", trace.name)


@app.get("/health", response_model=HealthResponse)
def health():
    try:
        with get_connection() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        logger.exception("health check failed")
        return JSONResponse(
            status_code=503, content={"error": "Database unavailable."}
        )
    # FIX (deploy-ready, item 3): capped is a normal state, and the payload
    # never contains the key or any part of it.
    return {
        "status": "ok",
        "llm": llm_status(),
        "db": "ok",
        "live_calls_remaining": budget.remaining(),
    }


@app.get("/")
def index():
    if not UI_PATH.exists():
        return JSONResponse(
            status_code=500, content={"error": "UI file is missing."}
        )
    return FileResponse(UI_PATH, media_type="text/html")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/state", response_model=StateResponse)
def state(session=Depends(get_session)):
    with session.lock:
        return build_state(session.student_id)


@app.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    session=Depends(get_session),
):
    # FIX (deploy-ready, item 4): one visitor cannot flood the shared demo.
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not chat_limiter.allow(client_ip):
        return JSONResponse(
            status_code=429,
            content={
                "error": (
                    "You are sending messages faster than the demo allows. "
                    "Wait a minute and try again."
                )
            },
        )
    with session.lock:
        # FIX (deploy-ready, item 3): charge this turn's live calls to this IP.
        session.agent.set_client_ip(client_ip)
        state_before = session.agent.export_state()
        pending = state_before.get("pending")
        message = payload.message.strip()
        confirmed_plan_id = None

        if pending and AFFIRMATIVE_RE.match(message):
            response = session.agent.confirm_pending()
            if not response.error:
                confirmed_plan_id = pending.get("plan_id")
        else:
            if payload.language != "en":
                message = (
                    f"{message}\n\n(Please reply in "
                    f"{LANGUAGE_NAMES.get(payload.language, 'English')}.)"
                )
            response = session.agent.handle_message(message)

        _log_traces(session.student_id, response.tool_calls)

        pending_data = pending_plan_data(session.student_id)
        conflicts = pending_conflicts(session.student_id)
        mapped = map_chat_response(
            response,
            pending_data=pending_data,
            conflicts=conflicts,
            # "capped" still means a key exists; only "offline" means there is
            # no key at all.
            has_key=llm_status() != "offline",
        )
        mapped["saved_plan_id"] = confirmed_plan_id
        return mapped


@app.post("/plan/save", response_model=OkResponse)
def save_plan(payload: PlanSaveRequest, session=Depends(get_session)):
    with session.lock:
        pending = session.agent.export_state().get("pending")
        if not pending:
            return JSONResponse(
                status_code=409,
                content={"error": "There is no plan waiting for confirmation."},
            )

        pending_id = pending.get("plan_id")
        if payload.plan_id and payload.plan_id != pending_id:
            return JSONResponse(
                status_code=400,
                content={"error": "That proposal is no longer the pending plan."},
            )

        response = session.agent.confirm_pending()
        _log_traces(session.student_id, response.tool_calls)
        if response.error:
            return JSONResponse(
                status_code=409,
                content={"error": response.reply_text or "The plan was not saved."},
            )

        return {
            "ok": True,
            "plan_id": pending_id,
            "status": "active",
            "reply": response.reply_text,
        }


@app.post("/plan/cancel", response_model=OkResponse)
def cancel_plan(payload: PlanCancelRequest, session=Depends(get_session)):
    with session.lock:
        pending = session.agent.export_state().get("pending")
        if not pending:
            return JSONResponse(
                status_code=409,
                content={"error": "There is no plan waiting for confirmation."},
            )
        response = session.agent.cancel_pending()
        cancel_pending_plan(session.student_id, pending.get("plan_id"))
        return {"ok": True, "reply": response.reply_text}


@app.post("/tasks/complete", response_model=OkResponse)
def complete_task(payload: TaskCompleteRequest, session=Depends(get_session)):
    with session.lock:
        if not owned_task(session.student_id, payload.task_id):
            return JSONResponse(
                status_code=404, content={"error": "Task not found."}
            )
        result = backend.update_task(
            task_id=payload.task_id,
            status="completed" if payload.done else "pending",
            due_at=None,
            estimated_minutes=None,
            student_id=session.student_id,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=409,
                content={"error": "The task could not be updated."},
            )
        return {
            "ok": True,
            "task_id": payload.task_id,
            "status": "completed" if payload.done else "pending",
        }


@app.post("/tasks/postpone", response_model=OkResponse)
def postpone_task(payload: TaskPostponeRequest, session=Depends(get_session)):
    with session.lock:
        if not owned_task(session.student_id, payload.task_id):
            return JSONResponse(
                status_code=404, content={"error": "Task not found."}
            )

        due_value = task_due_at(session.student_id, payload.task_id)
        new_due = None
        if due_value:
            new_due = to_ist(datetime.fromisoformat(due_value))
            new_due = new_due + timedelta(days=1)

        result = backend.update_task(
            task_id=payload.task_id,
            status="postponed",
            due_at=new_due,
            estimated_minutes=None,
            student_id=session.student_id,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=409,
                content={"error": "The task could not be postponed."},
            )
        return {
            "ok": True,
            "task_id": payload.task_id,
            "status": "postponed",
            "due_at": to_iso(new_due) if new_due else None,
        }


@app.post("/tasks/duration", response_model=OkResponse)
def update_duration(payload: TaskDurationRequest, session=Depends(get_session)):
    with session.lock:
        if not owned_task(session.student_id, payload.task_id):
            return JSONResponse(
                status_code=404, content={"error": "Task not found."}
            )
        result = backend.update_task(
            task_id=payload.task_id,
            status=None,
            due_at=None,
            estimated_minutes=payload.minutes,
            student_id=session.student_id,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=409,
                content={"error": "The duration could not be updated."},
            )
        return {"ok": True, "task_id": payload.task_id}


CHECKIN_STATUS = {
    "Completed": "completed",
    "Partially completed": "partial",
    "Partial": "partial",
    "Needed help": "needs_help",
}


@app.post("/preferences", response_model=OkResponse)
def save_preferences(payload: PreferencesRequest, session=Depends(get_session)):
    with session.lock:
        saved = update_preferences(
            session.student_id,
            daily_start_time=payload.daily_start_time,
            daily_end_time=payload.daily_end_time,
            preferred_break_minutes=payload.preferred_break_minutes,
            max_focus_minutes=payload.max_focus_minutes,
        )
        if not saved:
            return JSONResponse(
                status_code=404,
                content={"error": "No preferences row for this student."},
            )
        return {"ok": True, "reply": "Preferences saved."}


@app.post("/checkin", response_model=OkResponse)
def checkin(payload: CheckinRequest, session=Depends(get_session)):
    with session.lock:
        event_id = most_recent_event(session.student_id)
        result = backend.log_checkin(
            student_id=session.student_id,
            event_id=event_id,
            completion_status=CHECKIN_STATUS[payload.status],
            blocker=None,
            mood=payload.mood,
            energy=None,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=409,
                content={"error": "The check-in could not be saved."},
            )
        return {
            "ok": True,
            "checkin_id": result["data"].get("checkin_id"),
        }


@app.post("/demo/reset", response_model=OkResponse)
def demo_reset(request: Request):
    """FIX (deploy-ready, item 5): reseed the shared demo student.

    Rate-limited to 3 per hour per IP. Clears every session agent as well, so
    no browser keeps a plan that no longer exists in SQLite.
    """
    client_ip = getattr(request.state, "client_ip", "unknown")
    if not reset_limiter.allow(client_ip):
        return JSONResponse(
            status_code=429,
            content={
                "error": (
                    "The demo was reset recently. Please try again in a "
                    "little while."
                )
            },
        )
    registry.reset()
    summary = reset_demo_data()
    return {
        "ok": True,
        "reply": "Demo data was reseeded. All plans were cleared.",
        "status": "seeded" if summary.get("seeded") else "unchanged",
    }


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=PORT,
        log_level="info",
    )
