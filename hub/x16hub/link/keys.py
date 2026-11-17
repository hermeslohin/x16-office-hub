"""Pre-shared keys (secrets/x16link.keys) and hub link config (hub.db table).

Keys file: one `<devid> <64 hex>` line per device, mode 600 in a 700 dir.
Config lives in SQLite (hub.db, table link_config), never in git or JSON.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from pathlib import Path

from ..store import HubError, SECRETS_DIR, check_private_file, connect

KEYS_FILE = SECRETS_DIR / "x16link.keys"
DEVID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
SD_KEY_NAME = "X16LINK.KEY"


def keys_path() -> Path:
    return Path(os.environ.get("X16HUB_LINK_KEYS") or KEYS_FILE).expanduser()


def load_keys(path: Path | None = None) -> dict[str, bytes]:
    p = path or keys_path()
    if not p.exists():
        return {}
    check_private_file(p)
    out: dict[str, bytes] = {}
    for n, line in enumerate(p.read_text(encoding="ascii").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not DEVID_RE.fullmatch(parts[0]) or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[1]):
            raise HubError(f"{p}: line {n} is malformed (expected '<devid> <64 hex>')")  # no content echoed
        out[parts[0]] = bytes.fromhex(parts[1])
    return out


def _atomic_write(p: Path, text: str) -> None:
    """Write via a fresh temp file (O_EXCL|O_NOFOLLOW, random name, 600) + rename: never follows or
    reuses a file/symlink someone planted at the target or temp name."""
    tmp = p.with_name(f".{p.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise HubError(f"{tmp} is not owned by this user")
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        if fd != -1:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _write_private(p: Path, text: str) -> None:
    d = p.parent
    if not d.exists():
        d.mkdir(parents=True, mode=0o700)
    if d.resolve() == Path(KEYS_FILE).expanduser().parent.resolve():
        os.chmod(d, 0o700)                       # our own secrets dir: always private
    st = d.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise HubError(f"{d} must be owned by you and not group/world-writable (chmod 700 {d})")
    _atomic_write(p, text)


def new_key(devid: str, path: Path | None = None) -> bytes:
    if not DEVID_RE.fullmatch(devid):
        raise HubError("device id must be 1-16 chars of A-Z a-z 0-9 _ -")
    p = path or keys_path()
    keys = load_keys(p)
    keys[devid] = secrets.token_bytes(32)
    _write_private(p, "# x16link pre-shared keys: <devid> <hex>\n"
                      + "".join(f"{d} {k.hex()}\n" for d, k in sorted(keys.items())))
    return keys[devid]


def remove_key(devid: str, path: Path | None = None) -> bool:
    p = path or keys_path()
    keys = load_keys(p)
    if keys.pop(devid, None) is None:
        return False
    _write_private(p, "# x16link pre-shared keys: <devid> <hex>\n"
                      + "".join(f"{d} {k.hex()}\n" for d, k in sorted(keys.items())))
    return True


def write_sd_keyfile(directory: Path, host: str, devid: str, key: bytes) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,40}:\d{1,5}", host):
        raise HubError("host must look like 192.168.1.10:6502")
    if not DEVID_RE.fullmatch(devid):
        raise HubError("device id must be 1-16 chars of A-Z a-z 0-9 _ -")
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise HubError(f"{d} is not a directory (mount the SD card first)")
    out = d / SD_KEY_NAME
    _atomic_write(out, f"HOST={host}\nDEV={devid}\nKEY={key.hex()}\n")   # replaces a planted symlink/file
    return out


# --------------------------------------------------------------------------- auth math

def transcript(devid: bytes, cnonce: bytes, snonce: bytes, ver: int = 1) -> bytes:
    return devid + cnonce + snonce + bytes([ver])


def client_tag(key: bytes, t: bytes) -> bytes:
    return hmac.new(key, b"X16L1C" + t, hashlib.sha256).digest()


def server_tag(key: bytes, t: bytes) -> bytes:
    return hmac.new(key, b"X16L1S" + t, hashlib.sha256).digest()


# --------------------------------------------------------------------------- config (hub.db)

DEFAULTS = {
    "bind": "127.0.0.1",
    "port": "6502",
    "allow": "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10",
    "hubname": "x16hub",
    "calendar": "x16",
}


def _db():
    c = connect()
    c.execute("CREATE TABLE IF NOT EXISTS link_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return c


def get_config() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    with _db() as c:
        for k, v in c.execute("SELECT key, value FROM link_config"):
            cfg[k] = v
    return cfg


def set_config(key: str, value: str | None) -> None:
    if key not in DEFAULTS:
        raise HubError(f"unknown key {key!r}; known: {', '.join(DEFAULTS)}")
    if value is not None:
        validate_config(key, value)
    with _db() as c:
        if value is None:
            c.execute("DELETE FROM link_config WHERE key=?", (key,))
        else:
            c.execute("INSERT OR REPLACE INTO link_config(key, value) VALUES(?, ?)", (key, value))


def parse_allow(value: str) -> list:
    nets = []
    for part in value.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def validate_config(key: str, value: str) -> None:
    try:
        if key == "bind":
            ipaddress.ip_address(value)
        elif key == "port":
            if not 1 <= int(value) <= 65535:
                raise ValueError
        elif key == "allow":
            nets = parse_allow(value)
            if not nets:
                raise ValueError
            for n in nets:
                if n.prefixlen == 0:
                    raise HubError("allow must not contain a catch-all network (0.0.0.0/0, ::/0)")
                if n.is_global:
                    raise HubError(f"{n} is a public network; X16Link is LAN-only")
        elif key == "hubname":
            if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,32}", value):
                raise ValueError
    except HubError:
        raise
    except ValueError:
        raise HubError(f"invalid value for {key}") from None
