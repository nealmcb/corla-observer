"""Server-side coordinator sessions behind signed cookies, CSRF tokens, and the server secret.

A session is held in the server's memory (`SessionStore`), so logging out,
or restarting the server, ends it; so do `SESSION_LIFETIME` (12 hours)
from logging in and `SESSION_IDLE` (2 hours) without use. Its cookie lasts
the session's lifetime, so closing the browser does not log a coordinator
out. The cookie carries only a random session
token and an HMAC-SHA256 signature of it made with the server secret, so a
cookie not issued by this server is rejected before any lookup. Each session
has its own CSRF token, which every form carries and every POST checks.

Each session also has a separate random ``label``, recorded in events as
``session_id``: the event log is published, and the cookie token must never
appear in it.

Forms shown before login (the login form itself) use a pre-session token: a
random nonce in its own cookie, and the form field carries the HMAC of that
nonce, so a login can't be forged from another site either.

The server secret is at least 32 random bytes in a file outside the
repository and outside the workspace (`load_secret`), created with mode 0600
if it doesn't exist.

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variable naming the server secret file.
SECRET_ENV = "CORLA_OBSERVER_SECRET_FILE"
SECRET_BYTES = 32

#: How long a session lasts after login, and after its last use.
SESSION_LIFETIME = 12 * 3600
SESSION_IDLE = 2 * 3600
#: Most sessions held at once; the oldest are dropped beyond this.
MAX_SESSIONS = 1000


class SecretError(ValueError):
    """The server secret file is unusable."""


def default_secret_path() -> Path:
    """``$CORLA_OBSERVER_SECRET_FILE``, else ``~/.config/corla-observer/server-secret``."""
    env = os.environ.get(SECRET_ENV)
    if env:
        return Path(env)
    return Path.home() / ".config" / "corla-observer" / "server-secret"


def load_secret(path: Path | str, *, workspace: Path | str | None = None) -> bytes:
    """Read the server secret, creating it (mode 0600) if it doesn't exist.

    Raises:
        SecretError: if the file is inside ``workspace``, readable by others,
            or shorter than `SECRET_BYTES`.
    """
    path = Path(path)
    if workspace is not None:
        resolved, root = path.resolve(), Path(workspace).resolve()
        if resolved == root or root in resolved.parents:
            raise SecretError("the server secret must be kept outside the workspace")
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(SECRET_BYTES))
            f.flush()
            os.fsync(f.fileno())
    if path.stat().st_mode & 0o077:
        raise SecretError(f"the server secret {path} must not be readable by others (chmod 600)")
    secret = path.read_bytes()
    if len(secret) < SECRET_BYTES:
        raise SecretError(f"the server secret must be at least {SECRET_BYTES} bytes")
    return secret


@dataclass
class Session:
    account: str
    actor: str
    csrf: str
    #: The public label recorded in events as ``session_id``.
    label: str
    created: float
    last_used: float = field(default=0.0)


class SessionStore:
    """Sessions in memory, keyed by a random token that a signed cookie carries."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _sign(self, purpose: str, value: str) -> str:
        return hmac.new(self._secret, f"{purpose}\x00{value}".encode(), hashlib.sha256).hexdigest()

    # -- sessions ------------------------------------------------------------

    def create(self, account: str, actor: str) -> tuple[str, Session]:
        """Start a session; return the cookie value and the session."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(
            account=account,
            actor=actor,
            csrf=secrets.token_urlsafe(32),
            label="web-" + secrets.token_hex(8),
            created=now,
            last_used=now,
        )
        with self._lock:
            self._expire(now)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].last_used)
                del self._sessions[oldest]
            self._sessions[token] = session
        return f"{token}.{self._sign('session', token)}", session

    def get(self, cookie: str | None) -> Session | None:
        """The live session a cookie names, or None (for a missing, forged or expired cookie)."""
        token = self._token(cookie)
        if token is None:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if now - session.created > SESSION_LIFETIME or now - session.last_used > SESSION_IDLE:
                del self._sessions[token]
                return None
            session.last_used = now
            return session

    def end(self, cookie: str | None) -> Session | None:
        token = self._token(cookie)
        if token is None:
            return None
        with self._lock:
            return self._sessions.pop(token, None)

    def _token(self, cookie: str | None) -> str | None:
        if not cookie or len(cookie) > 256 or "." not in cookie:
            return None
        token, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign("session", token)):
            return None
        return token

    def _expire(self, now: float) -> None:
        for token in [
            t
            for t, s in self._sessions.items()
            if now - s.created > SESSION_LIFETIME or now - s.last_used > SESSION_IDLE
        ]:
            del self._sessions[token]

    def __len__(self) -> int:
        return len(self._sessions)

    # -- CSRF ----------------------------------------------------------------

    @staticmethod
    def check_csrf(session: Session, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(str(token), session.csrf)

    def new_presession(self) -> str:
        """A nonce for the pre-session cookie."""
        return secrets.token_urlsafe(24)

    def presession_csrf(self, nonce: str) -> str:
        """The form token that goes with a pre-session nonce."""
        return self._sign("presession", nonce)

    def check_presession(self, nonce: str | None, token: str | None) -> bool:
        if not nonce or not token or len(nonce) > 128:
            return False
        return hmac.compare_digest(str(token), self.presession_csrf(nonce))
