"""CampusPilot FastAPI server.

Run locally:

    uvicorn server:app --host 127.0.0.1 --port 8000

The server serves the single-file UI at ``/`` and the JSON API on the same
origin, so no CORS is needed and the session cookie stays HttpOnly.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import backend.campuspilot_backend as backend
from agent.config import load_config
from backend.database import get_connection
from backend.timeutil import now_ist, to_iso, to_ist
from integration.api_models import (
    ChatRequest,
    ChatResponse,
    CheckinRequest,
    HealthResponse,
    OkResponse,
    PlanCancelRequest,
    PlanSaveRequest,
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
    startup,
    task_due_at,
)

logger = logging.getLogger("campuspilot")
BASE_DIR = Path(__file__).resolve().parent
UI_PATH = BASE_DIR / "ui" / "campuspilot_ui.html"
COOKIE_NAME = "campuspilot_session"
PORT = int((os.getenv("PORT") or "8000").strip() or "8000")

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


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
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
                )
    return await call_next(request)


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
    return {"status": "ok", "llm": llm_status(), "db": "ok"}


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
def chat(payload: ChatRequest, session=Depends(get_session)):
    with session.lock:
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
            has_key=llm_status() == "live",
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
