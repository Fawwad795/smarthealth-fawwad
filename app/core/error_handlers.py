"""Registers the exception handlers that give every error response the same
JSON shape: {"error": {"code": ..., "message": ...}}.

Four handlers cover four different origins of failure:
- AppError: raised deliberately by services/ -- the message is safe to show.
- RequestValidationError: FastAPI's own shape for a malformed request body;
  reshaped into the same envelope instead of its default, differently-shaped
  list of errors.
- StarletteHTTPException: the framework's own errors -- a route that doesn't
  exist (404), a method that isn't allowed (405), and later, whatever
  get_current_user's security dependency raises before AppError is even in
  play.
- Exception: anything none of the above caught -- a bug. Logged in full
  here, because this is the only place that ever sees the real detail;
  returned to the client as a flat 500 with no trace, no DB text, nothing
  that could leak an internal detail (rule 8.9).

Starlette picks a handler by walking the raised exception's MRO and using
the first registered match, so AppError being more specific than Exception
is enough -- both can be registered without the catch-all swallowing the
specific one first.
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)


def _error_body(code: str, message: str) -> dict:
    """The one error envelope, built in one place so all four handlers
    below cannot drift into three slightly different shapes."""
    return {"error": {"code": code, "message": message}}


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """A failure services/ raised deliberately -- its status, code and
    message were all chosen for the client, so they pass straight through."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message),
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A request body that failed Pydantic validation.

    Reshaped into the standard envelope rather than returned in FastAPI's
    own default format, which is a differently-shaped list of error dicts.
    """
    # exc.errors() is a list of {"loc", "msg", "type", ...} dicts. Surfacing
    # every field is beyond Week 1's scope -- the first message is enough
    # for a client to know what to fix, and keeps the envelope flat.
    first = exc.errors()[0]
    message = first.get("msg", "Invalid request")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_body("VALIDATION_ERROR", message),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """A framework-level error: a route that doesn't exist (404), a method
    that isn't allowed (405). Its status is already correct; only the body
    needs reshaping into the standard envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body("HTTP_ERROR", str(exc.detail)),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything the three handlers above didn't catch -- i.e. a bug.

    The only place the real detail is ever seen, so it is logged in full
    here and returned to the client as a flat 500: no trace, no DB text,
    nothing that could leak an internal detail or PHI (rule 8.9).
    """
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_body("INTERNAL_ERROR", "Something went wrong."),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire all four handlers onto the app. Called once by create_app().

    Registration order does not matter: Starlette picks a handler by
    walking the raised exception's MRO and taking the first registered
    match, so the Exception catch-all cannot swallow AppError.
    """
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
