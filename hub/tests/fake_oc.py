"""In-memory stand-in for backend.owncloud.OwnCloud (WebDAV part) used by link tests."""
from __future__ import annotations

import itertools
from email.utils import formatdate

from x16hub.backend.owncloud import NotFound, PreconditionFailed

_etags = itertools.count(1)


class FakeOC:
    def __init__(self):
        self.files: dict[str, tuple[bytes, str, int]] = {}
        self.dirs = {""}
        self.clock = 1_763_400_000

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def add(self, path: str, data: bytes) -> str:
        parts = path.split("/")
        for i in range(1, len(parts)):
            self.dirs.add("/".join(parts[:i]))
        e = f'"e{next(_etags)}"'
        self.files[path] = (data, e, self._tick())
        return e

    def ensure_folder(self, path: str) -> bool:
        if path in self.dirs:
            return False
        self.dirs.add(path)
        return True

    def _item(self, p, is_dir):
        if is_dir:
            return {"path": p + "/", "etag": '"d"', "last_modified": formatdate(self.clock, usegmt=True),
                    "size": 0, "is_dir": True}
        data, e, mt = self.files[p]
        return {"path": p, "etag": e, "last_modified": formatdate(mt, usegmt=True), "size": len(data), "is_dir": False}

    def list(self, path: str = "", depth: int = 1):
        path = path.rstrip("/")
        if path not in self.dirs:
            if path in self.files and depth == 0:
                return [self._item(path, False)]
            raise NotFound("404", 404)
        out = [self._item(path, True)]
        if depth == 0:
            return out
        pre = path + "/"
        for d in sorted(self.dirs):
            if d.startswith(pre) and "/" not in d[len(pre):]:
                out.append(self._item(d, True))
        for f in sorted(self.files):
            if f.startswith(pre) and "/" not in f[len(pre):]:
                out.append(self._item(f, False))
        return out

    def stat(self, path):
        try:
            return self.list(path, depth=0)[0]
        except NotFound:
            return None

    def get(self, path, max_bytes=None):
        if path not in self.files:
            raise NotFound("404", 404)
        return self.files[path][0], self.files[path][1]

    def put(self, path, data, *, if_match=None, create_only=False):
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent not in self.dirs:
            from x16hub.backend.owncloud import OCError
            raise OCError("409", 409)
        cur = self.files.get(path)
        if create_only and cur is not None:
            raise PreconditionFailed("412", 412)
        if if_match and (cur is None or cur[1] != if_match):
            raise PreconditionFailed("412", 412)
        return self.add(path, data)

    # ---- folders / delete / move / copy (WebDAV semantics as ownCloud 11 answers them)
    def _parent_ok(self, path):
        from x16hub.backend.owncloud import OCError
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent not in self.dirs:
            raise OCError("409", 409)

    def mkcol(self, path):
        from x16hub.backend.owncloud import OCError
        if path in self.dirs or path in self.files:
            raise OCError("405", 405)
        self._parent_ok(path)
        self.dirs.add(path)

    def delete(self, path, if_match=None):
        if path in self.files:
            del self.files[path]
            return
        if path not in self.dirs or path == "":
            raise NotFound("404", 404)
        pre = path + "/"
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(pre)}
        self.files = {f: v for f, v in self.files.items() if not f.startswith(pre)}

    def _transfer(self, src, dst, overwrite, keep):
        if src not in self.files and src not in self.dirs:
            raise NotFound("404", 404)
        self._parent_ok(dst)
        if dst in self.files or dst in self.dirs:
            if not overwrite:
                raise PreconditionFailed("412", 412)
            self.delete(dst)
        if src in self.files:
            data = self.files[src][0]
            if not keep:
                del self.files[src]
            self.add(dst, data)
            return
        pre = src + "/"
        subdirs = [d for d in self.dirs if d == src or d.startswith(pre)]
        subfiles = [f for f in self.files if f.startswith(pre)]
        for d in subdirs:
            self.dirs.add(dst + d[len(src):])
        for f in subfiles:
            data = self.files[f][0]
            self.add(dst + f[len(src):], data)
        if not keep:
            for f in subfiles:
                del self.files[f]
            self.dirs -= set(subdirs)

    def move(self, src, dst, *, overwrite=False, is_dir=False):
        self._transfer(src, dst, overwrite, keep=False)

    def copy(self, src, dst, *, overwrite=False, is_dir=False):
        self._transfer(src, dst, overwrite, keep=True)
