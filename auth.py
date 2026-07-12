import base64
import hashlib
import hmac
import time

from fastapi import Request

from settings import settings

COOKIE_NAME = "ch_session"
SESSION_TTL = 7 * 24 * 3600  # 7 days in seconds


def _sign(message: str) -> str:
    """Return a URL-safe HMAC-SHA256 signature of ``message``."""
    digest = hmac.new(settings.APP_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def create_session_token(ttl: int = SESSION_TTL) -> str:
    """Create a signed session token that expires after ``ttl`` seconds."""
    expiry = str(int(time.time()) + ttl)
    return f"{expiry}.{_sign(expiry)}"


def verify_session_token(token: str) -> bool:
    """Validate a session token's signature and expiry in constant time."""
    if not token or token.count(".") != 1:
        return False
    expiry, signature = token.split(".", 1)
    if not hmac.compare_digest(signature, _sign(expiry)):
        return False
    try:
        return int(expiry) > int(time.time())
    except ValueError:
        return False


def verify_password(password: str) -> bool:
    """Compare a submitted password against APP_PASSWORD in constant time."""
    if not settings.APP_PASSWORD:
        return False
    return hmac.compare_digest(password.encode(), settings.APP_PASSWORD.encode())


def is_authenticated(request: Request) -> bool:
    """Whether the request carries a valid session cookie."""
    return verify_session_token(request.cookies.get(COOKIE_NAME, ""))
