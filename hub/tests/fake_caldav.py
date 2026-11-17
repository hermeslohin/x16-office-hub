"""In-memory stand-in for OwnCloud's CalDAV methods (same signatures and errors as backend/owncloud.py).

It models sabre/ownCloud behaviour that calsync relies on: content-hash etags, If-Match /
If-None-Match -> 412, RFC 6578 tokens with a change log (deletes reported as 404).
"""
from __future__ import annotations

import hashlib

from x16hub.backend.owncloud import NotFound, OCError, PreconditionFailed


class FakeOC:
    def __init__(self, name: str = "x16"):
        self.cal = {"href": f"/remote.php/dav/calendars/x16hub/{name}/", "name": name}
        self.objs: dict[str, tuple[str, str]] = {}   # href -> (ics, etag)
        self.tok = 1
        self.log: list[tuple[int, str]] = []         # (token after change, href)
        self.calls: list[str] = []
        self.fail_after: str | None = None           # method name: raise after doing it (simulated crash)
        self.reject_tokens = False

    # ---- helpers for tests
    def _bump(self, href):
        self.tok += 1
        self.log.append((self.tok, href))

    @staticmethod
    def _etag(ics: str) -> str:
        return '"' + hashlib.md5(ics.encode()).hexdigest() + '"'

    def href_for(self, uid):
        return self.cal["href"] + uid + ".ics"

    def server_put(self, uid: str, ics) -> str:
        """A change made by someone in ownCloud (web UI / phone)."""
        ics = ics.decode() if isinstance(ics, bytes) else ics
        h = self.href_for(uid)
        self.objs[h] = (ics, self._etag(ics))
        self._bump(h)
        return h

    def server_delete(self, uid: str):
        h = self.href_for(uid)
        del self.objs[h]
        self._bump(h)

    def ics(self, uid):
        return self.objs[self.href_for(uid)][0]

    def _maybe_crash(self, name):
        if self.fail_after == name:
            self.fail_after = None
            raise OCError(f"simulated connection drop after {name}")

    # ---- OwnCloud API
    def ensure_calendar(self, name="x16", displayname="X16"):
        return self.cal, False

    def sync_collection(self, cal, token):
        self.calls.append("sync")
        if token is None:
            return {"token": f"http://sabre.io/ns/sync/{self.tok}",
                    "changed": [{"href": h, "etag": e} for h, (_, e) in self.objs.items()], "deleted": []}
        if self.reject_tokens:
            raise OCError("invalid sync token", 403)
        n = int(token.rsplit("/", 1)[-1])
        hrefs = {h for t, h in self.log if t > n}
        changed = [{"href": h, "etag": self.objs[h][1]} for h in sorted(hrefs) if h in self.objs]
        deleted = [h for h in sorted(hrefs) if h not in self.objs]
        return {"token": f"http://sabre.io/ns/sync/{self.tok}", "changed": changed, "deleted": deleted}

    def get_event(self, href):
        self.calls.append("get")
        if href not in self.objs:
            raise NotFound(f"GET {href} -> 404", 404)
        ics, et = self.objs[href]
        return {"href": href, "etag": et, "ics": ics}

    def create_event(self, cal, ics, uid=None):
        self.calls.append("create")
        h = self.href_for(uid)
        if h in self.objs:
            raise PreconditionFailed("412", 412)
        ics = ics.decode() if isinstance(ics, bytes) else ics
        self.objs[h] = (ics, self._etag(ics))
        self._bump(h)
        self._maybe_crash("create")
        return h, None          # ownCloud often omits ETag on PUT

    def update_event(self, href, ics, etag):
        self.calls.append("update")
        if href not in self.objs or self.objs[href][1] != etag:
            raise PreconditionFailed("412", 412)
        ics = ics.decode() if isinstance(ics, bytes) else ics
        self.objs[href] = (ics, self._etag(ics))
        self._bump(href)
        self._maybe_crash("update")
        return None

    def delete_event(self, href, etag=None):
        self.calls.append("delete")
        if href not in self.objs:
            raise NotFound("404", 404)
        if etag and self.objs[href][1] != etag:
            raise PreconditionFailed("412", 412)
        del self.objs[href]
        self._bump(href)
        self._maybe_crash("delete")
