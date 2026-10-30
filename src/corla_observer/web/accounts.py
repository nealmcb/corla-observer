"""Coordinator accounts, kept in the workspace with salted scrypt password hashes.

Accounts live in ``<workspace>/accounts/coordinators.json`` (`ACCOUNTS_PATH`),
readable only by the server's user (mode 0600, in a 0700 directory). Each
account has a name (the ``ID`` in its actor ``coordinator:ID``), a random
16-byte salt and the scrypt hash of its password with the parameters used.
No password is ever stored or logged. The ``accounts/`` directory must not be
published with the rest of the workspace.

Adding an account records an ``account.added`` event (actor ``system``, data
``name`` and ``actor``); changing a password records ``account.password_set``.

Standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from corla_observer.events import EventLog

ACCOUNTS_DIRNAME = "accounts"
ACCOUNTS_FILENAME = "coordinators.json"
#: Relative to the workspace root.
ACCOUNTS_PATH = Path(ACCOUNTS_DIRNAME) / ACCOUNTS_FILENAME

#: scrypt parameters for new hashes (about 16 MiB of memory per hash).
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_MAX_MEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class AccountError(ValueError):
    """An invalid account name or password, or an account that exists or doesn't."""


@dataclass(frozen=True)
class Account:
    name: str
    salt: bytes
    hash: bytes
    n: int
    r: int
    p: int
    created: str

    @property
    def actor(self) -> str:
        return f"coordinator:{self.name}"


def check_name(name: str) -> str:
    """Return ``name`` if it is a valid account name (letters, digits, ``.``, ``_``, ``-``; 1 to 64)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AccountError("an account name is 1 to 64 letters, digits, '.', '_' or '-'")
    return name


def _check_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AccountError(f"a password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=_MAX_MEM)


# A hash to check against when the account doesn't exist, so that the time a
# failed login takes doesn't reveal whether the name is an account.
_DUMMY_SALT = secrets.token_bytes(16)
_DUMMY_HASH = _scrypt("not a password", _DUMMY_SALT, SCRYPT_N, SCRYPT_R, SCRYPT_P)


class AccountStore:
    """The coordinator accounts of one workspace."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = Path(workspace_root) / ACCOUNTS_PATH

    def _load(self) -> dict[str, Account]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        accounts = {}
        for name, entry in raw.get("accounts", {}).items():
            accounts[name] = Account(
                name=name,
                salt=bytes.fromhex(entry["salt"]),
                hash=bytes.fromhex(entry["hash"]),
                n=int(entry["n"]),
                r=int(entry["r"]),
                p=int(entry["p"]),
                created=str(entry["created"]),
            )
        return accounts

    def _save(self, accounts: dict[str, Account]) -> None:
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        body = {
            "format": 1,
            "accounts": {
                a.name: {"salt": a.salt.hex(), "hash": a.hash.hex(), "n": a.n, "r": a.r, "p": a.p, "created": a.created}
                for a in sorted(accounts.values(), key=lambda a: a.name)
            },
        }
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".coordinators-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=1, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def names(self) -> list[str]:
        return sorted(self._load())

    def get(self, name: str) -> Account | None:
        return self._load().get(name)

    def add(self, name: str, password: str, log: EventLog | None = None) -> Account:
        """Create an account; record ``account.added`` in ``log`` if given.

        Raises:
            AccountError: for an invalid name or password, or an existing account.
        """
        check_name(name)
        _check_password(password)
        accounts = self._load()
        if name in accounts:
            raise AccountError(f"account {name!r} already exists")
        account = self._new(name, password)
        accounts[name] = account
        self._save(accounts)
        if log is not None:
            log.append("account.added", "system", data={"name": name, "actor": account.actor})
        return account

    def set_password(self, name: str, password: str, log: EventLog | None = None) -> Account:
        """Replace an account's password; record ``account.password_set`` in ``log`` if given."""
        check_name(name)
        _check_password(password)
        accounts = self._load()
        if name not in accounts:
            raise AccountError(f"no account {name!r}")
        account = self._new(name, password, created=accounts[name].created)
        accounts[name] = account
        self._save(accounts)
        if log is not None:
            log.append("account.password_set", "system", data={"name": name, "actor": account.actor})
        return account

    @staticmethod
    def _new(name: str, password: str, created: str | None = None) -> Account:
        salt = secrets.token_bytes(16)
        return Account(
            name=name,
            salt=salt,
            hash=_scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            created=created or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def authenticate(self, name: str, password: str) -> Account | None:
        """The account if ``password`` is its password, else None, taking about as long either way."""
        account = self.get(name) if isinstance(name, str) and _NAME_RE.match(name) else None
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
            password = ""
        if account is None:
            hmac.compare_digest(_scrypt(password, _DUMMY_SALT, SCRYPT_N, SCRYPT_R, SCRYPT_P), _DUMMY_HASH)
            return None
        candidate = _scrypt(password, account.salt, account.n, account.r, account.p)
        return account if hmac.compare_digest(candidate, account.hash) else None
