"""The one JSON shape every error takes, regardless of what caused it.

No database, no infrastructure of any kind -- register_exception_handlers
takes a bare FastAPI() app, so these tests build one directly instead of
importing the real app.main:app, which would drag in Postgres via /health/db.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.error_handlers import register_exception_handlers
from app.core.exceptions import AppError


def _test_app() -> FastAPI:
    """One route per error origin, wired to the real handlers."""
    app = FastAPI()
    register_exception_handlers(app)

    class Body(BaseModel):
        name: str

    @app.get("/app-error")
    def raise_app_error():
        raise AppError(status_code=403, code="FORBIDDEN", message="not allowed")

    @app.post("/validation")
    def raise_validation(body: Body):
        return body

    @app.get("/boom")
    def raise_unhandled():
        raise RuntimeError("secret internal detail")

    return app


def _client() -> TestClient:
    # raise_server_exceptions=False: without it, TestClient re-raises an
    # unhandled exception instead of letting the app's own handler catch it,
    # which is exactly the path test_unhandled_exception_is_hidden checks.
    return TestClient(_test_app(), raise_server_exceptions=False)


def _assert_envelope(body: dict) -> None:
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}


def test_app_error_uses_its_own_status_and_code() -> None:
    response = _client().get("/app-error")
    assert response.status_code == 403
    body = response.json()
    _assert_envelope(body)
    assert body["error"]["code"] == "FORBIDDEN"


def test_validation_error_is_reshaped_into_the_envelope() -> None:
    """FastAPI's default shape for this is a list under "detail" -- a
    different key and a different structure from every other error. A
    client should not need a special case just for 422s.
    """
    response = _client().post("/validation", json={})
    assert response.status_code == 422
    body = response.json()
    _assert_envelope(body)
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_missing_route_uses_the_envelope_not_the_framework_default() -> None:
    """Starlette's own 404 is {"detail": "Not Found"} unless overridden."""
    response = _client().get("/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    _assert_envelope(body)
    assert body["error"]["code"] == "HTTP_ERROR"


def test_unhandled_exception_is_hidden_from_the_client() -> None:
    """The one case where the real message must never reach the response --
    rule 8.9 forbids leaking stack traces or internal detail to a client.
    """
    response = _client().get("/boom")
    assert response.status_code == 500
    body = response.json()
    _assert_envelope(body)
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in response.text
