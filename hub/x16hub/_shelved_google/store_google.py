"""SHELVED: Google-only pieces of the old store (client path + oauth tables)."""
import os
from pathlib import Path

from ..store import HubError, SECRETS_DIR, check_private_file, now  # noqa: F401
from ..store import connect as _connect

DEFAULT_CLIENT = SECRETS_DIR / "google_client.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_pending (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    state        TEXT NOT NULL,
    verifier     TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    redirect_uri TEXT NOT NULL DEFAULT 'http://localhost'
);
CREATE TABLE IF NOT EXISTS oauth_token (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    refresh_token  TEXT NOT NULL,
    scopes         TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
"""


def client_path() -> Path:
    return Path(os.environ.get("X16HUB_CLIENT") or DEFAULT_CLIENT).expanduser()


def connect():
    conn = _connect()
    conn.executescript(SCHEMA)
    return conn
