""" AppError: the one exception type business logic raises for an expected
failure. app/core/error_handlers.py turns every AppError -- and everything
else that can go wrong -- into the same JSON shape.
"""


class AppError(Exception):
    """A deliberate, expected failure: wrong credentials, a role that
    doesn't match, a resource that doesn't exist. Not for bugs -- an
    unhandled AttributeError or IntegrityError is caught by the catch-all
    handler instead, and never reaches the client with this much detail.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)