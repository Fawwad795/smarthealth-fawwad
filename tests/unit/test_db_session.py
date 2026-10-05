"""Tests for the session seam that non-request code depends on."""

from contextlib import nullcontext

import pytest

from app.db.session import session_scope


def test_session_scope_resolves_session_local_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patching app.db.session.SessionLocal must reach session_scope().

    This is the invariant every worker test relies on. If someone later
    "tidies" session_scope into holding a module-level reference -- say
    _factory = SessionLocal at import time -- the lookup stops being late
    and every one of those tests silently starts writing to the real
    database instead. This fails first, and says why.
    """
    sentinel = object()
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: nullcontext(sentinel))

    with session_scope() as db:
        assert db is sentinel
