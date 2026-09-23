"""Session-token auth for FastAPI.

Better Auth stores the session in an HttpOnly cookie (`better-auth.session_token`).
The cookie value is `rawToken.base64(HMAC-SHA256(secret, rawToken))` — the same
`rawToken` is stored in the shared `session` table. FastAPI reads the cookie,
extracts the token, and looks it up in the DB. The user_id is derived from the
token and is never trusted from the request body.

A `Authorization: Bearer <token>` header is also accepted (useful for curl/tests).
"""

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import Header, HTTPException, Request

from db import get_conn

logger = logging.getLogger("auth")

SESSION_COOKIE_NAMES = (
    "better-auth.session_token",
    "__Secure-better-auth.session_token",
)


def _extract_token(request: Request, authorization: str | None) -> tuple[str | None, str | None]:
    """Return (token, source) from the bearer header or the session cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token, "bearer"

    raw_cookie = request.headers.get("cookie", "")
    for pair in raw_cookie.split(";"):
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name.strip() not in SESSION_COOKIE_NAMES:
            continue
        value = unquote(value.strip())
        token = value.rsplit(".", 1)[0]  # strip the HMAC signature suffix
        return token, "cookie"
    return None, None


def _verify_cookie_signature(value: str) -> bool:
    """Verify the HMAC suffix when BETTER_AUTH_SECRET is available."""
    secret = os.environ.get("BETTER_AUTH_SECRET")
    if not secret or "." not in value:
        return True  # nothing to verify against
    token, _, signature = value.rpartition(".")
    expected = base64.b64encode(
        hmac.new(secret.encode(), token.encode(), hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def current_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    token, source = _extract_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing session")

    if source == "cookie":
        # re-read the raw value for signature verification
        raw_cookie = request.headers.get("cookie", "")
        for pair in raw_cookie.split(";"):
            name, _, value = pair.partition("=")
            if name.strip() in SESSION_COOKIE_NAMES and not _verify_cookie_signature(
                unquote(value.strip())
            ):
                logger.warning("AUTH FAIL | bad cookie signature")
                raise HTTPException(status_code=401, detail="Invalid session cookie")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT s.user_id, s.expires_at,
                  u.name AS user_name, u.email AS user_email
           FROM "session" s
           JOIN "user" u ON u.id = s.user_id
           WHERE s.token = %s""",
        (token,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        logger.warning("AUTH FAIL | unknown token via %s", source)
        raise HTTPException(status_code=401, detail="Invalid session")

    expires = row["expires_at"]
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            logger.warning("AUTH FAIL | expired session for user=%s", row["user_id"])
            raise HTTPException(status_code=401, detail="Session expired")

    logger.info(
        "AUTH OK | via=%s | user_id=%s | name=%s | email=%s",
        source,
        row["user_id"],
        row["user_name"],
        row["user_email"],
    )
    return row["user_id"]


def require_chat_owner(chat_id: str, user_id: str) -> None:
    """Raise 404 unless the chat exists and belongs to this user."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM chat WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or row["user_id"] != user_id:
        logger.warning("OWNERSHIP FAIL | chat=%s user=%s", chat_id, user_id)
        raise HTTPException(status_code=404, detail="Chat not found")
