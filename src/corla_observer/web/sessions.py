"""Server-side coordinator sessions behind signed cookies, CSRF tokens, and the server secret.

A session lasts until logging out, or `SESSION_LIFETIME` (30 days) from
logging in; there is no limit on time without use. Its cookie lasts the
session's lifetime, so closing the browser does not log a coordinator out.
Sessions are kept in a file beside the coordinator accounts
(``accounts/sessions.json``, mode 0600), so restarting the server keeps
everyone logged in. The file holds only the SHA-256 of each session's token,
never the token itself, so a copy of it can't be turned into a cookie.
The cookie carries only a random session
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
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variable naming the server secret file.
SECRET_ENV = "CORLA_OBSERVER_SECRET_FILE"
SECRET_BYTES = 32

#: How long a session lasts after login (there is no limit on time without use).
SESSION_LIFETIME = 30 * 24 * 3600
#: The sessions file, relative to the workspace root (beside the accounts).
SESSIONS_PATH = Path("accounts") / "sessions.json"
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


def _key(token: str) -> str:
    """What a session is filed under: the SHA-256 of its token, so the file never holds a usable token."""
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    """Sessions keyed by (the hash of) a random token that a signed cookie carries; kept in ``path`` if given."""

    def __init__(self, secret: bytes, path: Path | str | None = None) -> None:
        self._secret = secret
        self._path = Path(path) if path is not None else None
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw["sessions"] if raw.get("format") == 1 else {}
            for key, e in entries.items():
                self._sessions[str(key)] = Session(
                    account=str(e["account"]),
                    actor=str(e["actor"]),
                    csrf=str(e["csrf"]),
                    label=str(e["label"]),
                    created=float(e["created"]),
                    last_used=float(e["last_used"]),
                )
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # An unreadable file only means logging in again.
            self._sessions = {}
        self._expire(time.time())

    def _save(self) -> None:
        """Write the sessions file atomically (mode 0600); called with the lock held."""
        if self._path is None:
            return
        directory = self._path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = {
            "format": 1,
            "sessions": {
                k: {
                    "account": s.account,
                    "actor": s.actor,
                    "csrf": s.csrf,
                    "label": s.label,
                    "created": s.created,
                    "last_used": s.last_used,
                }
                for k, s in self._sessions.items()
            },
        }
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions-")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _sign(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._secret, f"{purpose}\x00{value}".encode(), hashlib.sha256
        ).hexdigest()

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
            self._sessions[_key(token)] = session
            self._save()
        return f"{token}.{self._sign('session', token)}", session

    def get(self, cookie: str | None) -> Session | None:
        """The live session a cookie names, or None (for a missing, forged or expired cookie)."""
        token = self._token(cookie)
        if token is None:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(_key(token))
            if session is None:
                return None
            if now - session.created > SESSION_LIFETIME:
                del self._sessions[_key(token)]
                self._save()
                return None
            session.last_used = now
            return session

    def end(self, cookie: str | None) -> Session | None:
        token = self._token(cookie)
        if token is None:
            return None
        with self._lock:
            session = self._sessions.pop(_key(token), None)
            if session is not None:
                self._save()
            return session

    def _token(self, cookie: str | None) -> str | None:
        if not cookie or len(cookie) > 256 or "." not in cookie:
            return None
        token, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign("session", token)):
            return None
        return token

    def _expire(self, now: float) -> None:
        for token in [
            t for t, s in self._sessions.items() if now - s.created > SESSION_LIFETIME
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
