"""Hub state: data dir (700) + SQLite hub.db (600), and secret-file checks.

Data dir comes from X16HUB_DATA (so it can be a container volume later),
defaulting to <hub>/data. Nothing secret is ever written to JSON files or logs.
"""
import os
import sqlite3
import stat
import time
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA = HUB_DIR / "data"
SECRETS_DIR = HUB_DIR.parent / "secrets"


class HubError(Exception):
    """User-facing error; message must never contain secrets."""


def data_dir() -> Path:
    return Path(os.environ.get("X16HUB_DATA") or DEFAULT_DATA).expanduser()


def _ensure_private_dir(p: Path) -> None:
    old = os.umask(0o077)
    try:
        p.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    os.chmod(p, 0o700)


def check_private_file(p: Path) -> None:
    """Refuse to use a secret file that is group/world accessible (or whose dir is)."""
    st = p.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HubError(f"{p} has mode {oct(st.st_mode & 0o777)}; run: chmod 600 {p}")
    if st.st_uid != os.getuid():
        raise HubError(f"{p} is not owned by the current user; refusing")
    pst = p.parent.stat()
    if pst.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise HubError(f"{p.parent} has mode {oct(pst.st_mode & 0o777)}; run: chmod 700 {p.parent}")


def connect() -> sqlite3.Connection:
    d = data_dir()
    _ensure_private_dir(d)
    db = d / "hub.db"
    old = os.umask(0o077)
    try:
        conn = sqlite3.connect(db)
    finally:
        os.umask(old)
    os.chmod(db, 0o600)
    conn.execute("PRAGMA secure_delete=ON")
    return conn


def now() -> int:
    return int(time.time())
