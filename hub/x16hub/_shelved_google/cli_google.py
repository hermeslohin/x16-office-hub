"""SHELVED old CLI. CLI: python -m x16hub auth {url|finish <pasted-url>|check|revoke}"""
import argparse
import datetime as dt
import sys

from . import auth
from .store_google import HubError


def cmd_url(_):
    print(auth.make_auth_url())


def cmd_finish(a):
    try:
        res = auth.finish(a.pasted_url)
    except auth.StaleCode as e:
        print(f"error: {e}", file=sys.stderr)
        print("A fresh sign-in URL was generated; open it and paste the new redirect URL:", file=sys.stderr)
        print(auth.make_auth_url())
        return 3
    print("signed in; refresh token stored in hub.db")
    for s in auth.SCOPES:
        print(f"  {'granted' if s in res['granted'] else 'MISSING'}  {s}")
    if res["missing"]:
        print("warning: some scopes were not granted (unticked on the consent screen?)", file=sys.stderr)
        return 2
    return 0


def cmd_check(_):
    from googleapiclient.discovery import build

    creds = auth.credentials()
    print("token refresh: ok")
    now = dt.datetime.now(dt.timezone.utc)
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    n, tok = 0, None
    while True:
        r = cal.events().list(calendarId="primary", timeMin=now.isoformat(),
                              timeMax=(now + dt.timedelta(days=7)).isoformat(),
                              singleEvents=True, maxResults=250, pageToken=tok,
                              fields="items(id),nextPageToken").execute()
        n += len(r.get("items", []))
        tok = r.get("nextPageToken")
        if not tok:
            break
    print(f"calendar primary, next 7 days: {n} events")

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    m, tok = 0, None
    while True:
        r = drive.files().list(pageSize=100, pageToken=tok, fields="files(id),nextPageToken").execute()
        m += len(r.get("files", []))
        tok = r.get("nextPageToken")
        if not tok:
            break
    print(f"drive files visible to hub (drive.file): {m}")
    return 0


def cmd_revoke(_):
    auth.revoke()
    print("token revoked at Google and removed from hub.db")


def main(argv=None):
    import os
    os.umask(0o077)  # sqlite journals etc. must never be group/world readable
    p = argparse.ArgumentParser(prog="python -m x16hub")
    sub = p.add_subparsers(dest="group", required=True)
    ap = sub.add_parser("auth", help="Google sign-in")
    asub = ap.add_subparsers(dest="cmd", required=True)
    asub.add_parser("url", help="print a sign-in URL").set_defaults(fn=cmd_url)
    f = asub.add_parser("finish", help="finish sign-in with the pasted redirect URL")
    f.add_argument("pasted_url")
    f.set_defaults(fn=cmd_finish)
    asub.add_parser("check", help="refresh token + read-only API test").set_defaults(fn=cmd_check)
    asub.add_parser("revoke", help="revoke the stored token and forget it").set_defaults(fn=cmd_revoke)
    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except HubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
