"""Exceptions raised by the client and the price stream."""


class CapitalError(Exception):
    """Base class for every error raised by this library."""


class AuthError(CapitalError):
    """Login failed, or an operation needs a session and none is available."""


class APIError(CapitalError):
    """Capital.com answered with a non-success status."""

    def __init__(self, status_code: int, body: str = "", error_code: str | None = None, url: str = ""):
        self.status_code = status_code
        self.body = body
        self.error_code = error_code
        self.url = url
        detail = f"{status_code}" + (f" {error_code}" if error_code else "")
        super().__init__(f"Capital.com API error {detail}: {body[:300]}")


class StreamError(CapitalError):
    """The price stream could not be established."""
