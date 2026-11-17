"""SHELVED (not imported by the hub). One-time Google OAuth sign-in for a headless host (PKCE, loopback redirect).

Flow: `auth url` prints a consent URL; the user signs in, the browser is
redirected to http://localhost/?state=..&code=.. (which fails to load);
the user pastes that address-bar URL into `auth finish`.

Never print/log the client secret, auth code, access token or refresh token.
"""
import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from .store_google import HubError, check_private_file, client_path, connect, now

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
# Port 80 loopback: Desktop-app clients accept any loopback port, and browsers
# (notably iOS Safari) block navigation to "unsafe" port 1.
REDIRECT_URI = "http://localhost"
# Redirect URLs `auth finish` accepts (localhost:1 kept for older pending sign-ins).
ACCEPTED_REDIRECT_NETLOCS = {"localhost", "localhost:80", "localhost:1", "127.0.0.1", "127.0.0.1:80"}
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
PENDING_MAX_AGE = 3600  # seconds a pending sign-in stays valid


def load_client() -> dict:
    p = client_path()
    if not p.exists():
        raise HubError(f"OAuth client file not found: {p}")
    check_private_file(p)
    with open(p) as f:
        data = json.load(f)
    c = data.get("installed")
    if not c:
        raise HubError("client file must be a Desktop-app client ('installed' section)")
    return {"client_id": c["client_id"], "client_secret": c["client_secret"],
            "token_uri": c.get("token_uri", TOKEN_ENDPOINT)}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_auth_url() -> str:
    client = load_client()
    verifier = _b64url(secrets.token_bytes(64))          # 86 chars, within 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO oauth_pending (id, state, verifier, created_at, redirect_uri) "
                     "VALUES (1, ?, ?, ?, ?)", (state, verifier, now(), REDIRECT_URI))
    params = {
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _post_form(url: str, form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:
            err = {}
        # Only the error code/description are surfaced, never the request body.
        return {"_http_status": e.code, "error": err.get("error", "http_error"),
                "error_description": err.get("error_description", "")}


class StaleCode(HubError):
    """Code expired/used or pending sign-in lost; a fresh URL is needed."""


def finish(pasted_url: str) -> dict:
    """Exchange the pasted redirect URL. Returns {'granted': [...], 'missing': [...]}."""
    u = urllib.parse.urlsplit(pasted_url.strip())
    if u.scheme != "http" or u.netloc.lower() not in ACCEPTED_REDIRECT_NETLOCS:
        raise HubError("expected the redirect URL http://localhost/?state=...&code=... "
                       "(copy the address bar after tapping Continue)")
    q = urllib.parse.parse_qs(u.query)
    if "error" in q:
        raise HubError(f"Google returned error: {q['error'][0]}")
    state = (q.get("state") or [""])[0]
    code = (q.get("code") or [""])[0]
    if not state or not code:
        raise HubError("pasted URL has no state/code; copy the full http://localhost/?... address")

    with connect() as conn:
        row = conn.execute("SELECT state, verifier, created_at, redirect_uri FROM oauth_pending "
                           "WHERE id=1").fetchone()
    if row is None:
        raise StaleCode("no pending sign-in (already finished or never started)")
    p_state, verifier, created, p_redirect = row
    if not hmac.compare_digest(state.encode(), p_state.encode()):
        raise HubError("state mismatch: this URL is not from the latest `auth url`; refusing")
    if now() - created > PENDING_MAX_AGE:
        raise StaleCode("pending sign-in is older than 1 hour")

    client = load_client()
    resp = _post_form(client["token_uri"], {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": p_redirect,  # must equal the one used in the auth URL
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    })
    if "error" in resp:
        if resp["error"] == "invalid_grant":
            raise StaleCode(f"code rejected (invalid_grant: {resp['error_description']})")
        raise HubError(f"token exchange failed: {resp['error']} {resp['error_description']}".strip())

    refresh = resp.get("refresh_token")
    if not refresh:
        raise HubError("Google returned no refresh token (consent must include offline access)")
    granted = sorted(set(resp.get("scope", "").split()))
    missing = [s for s in SCOPES if s not in granted]

    with connect() as conn:
        t = now()
        conn.execute("INSERT OR REPLACE INTO oauth_token (id, refresh_token, scopes, created_at, updated_at) "
                     "VALUES (1, ?, ?, ?, ?)", (refresh, " ".join(granted), t, t))
        conn.execute("DELETE FROM oauth_pending WHERE id=1")
    return {"granted": granted, "missing": missing}


def credentials():
    """google.oauth2 Credentials built from the stored refresh token (refreshed)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with connect() as conn:
        row = conn.execute("SELECT refresh_token, scopes FROM oauth_token WHERE id=1").fetchone()
    if row is None:
        raise HubError("not signed in; run: python -m x16hub auth url")
    client = load_client()
    creds = Credentials(token=None, refresh_token=row[0], token_uri=client["token_uri"],
                        client_id=client["client_id"], client_secret=client["client_secret"],
                        scopes=row[1].split())
    try:
        creds.refresh(Request())
    except Exception as e:  # RefreshError text is Google's error code, no token material
        raise HubError(f"token refresh failed ({type(e).__name__}); re-run `auth url` to sign in again")
    with connect() as conn:
        conn.execute("UPDATE oauth_token SET updated_at=? WHERE id=1", (now(),))
    return creds


def revoke() -> None:
    with connect() as conn:
        row = conn.execute("SELECT refresh_token FROM oauth_token WHERE id=1").fetchone()
    if row is None:
        raise HubError("no stored token")
    resp = _post_form(REVOKE_ENDPOINT, {"token": row[0]})
    if "error" in resp and resp["error"] != "invalid_token":
        raise HubError(f"revoke failed: {resp['error']}")
    with connect() as conn:
        conn.execute("DELETE FROM oauth_token WHERE id=1")
