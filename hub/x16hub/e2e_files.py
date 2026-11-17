"""Seed / verify / clean the e2e test files in ownCloud Documents/ (used by the X16 e2e tests).

  python -m x16hub.e2e_files seed <rtf-file>     put LKTEST.RTF (given file) + LKTEST.TXT (UTF-8 sample)
  python -m x16hub.e2e_files compare <name> <local-file> [--txt-from-rtf]
  python -m x16hub.e2e_files clean               delete every Documents/LKTEST* file
"""
import sys
from pathlib import Path

from .backend import owncloud as oc
from .link.rtfconv import WP_CHARSET, rtf_to_text

TXT_SAMPLE = "X16 Office e2e test\nGr\u00fc\u00dfe aus Springfield \u2013 price 5 \u20ac\n{braces} and \\backslash\\\n\tTabbed line\n"


def client():
    return oc.OwnCloud(oc.load_config())


def main(argv):
    c = client()
    if argv[0] == "seed":
        c.ensure_folder("Documents")
        for name, data in (("LKTEST.RTF", Path(argv[1]).read_bytes()), ("LKTEST.TXT", TXT_SAMPLE.encode())):
            st = c.stat(f"Documents/{name}")
            c.put(f"Documents/{name}", data, if_match=st["etag"] if st else None, create_only=st is None)
            print(f"seeded Documents/{name} ({len(data)} bytes)")
    elif argv[0] == "compare":
        remote, _ = c.get(f"Documents/{argv[1]}")
        local = Path(argv[2]).read_bytes()
        if "--txt-from-rtf" in argv:
            local = rtf_to_text(local, WP_CHARSET).encode("utf-8")
        ok = remote == local
        print(f"{'PASS' if ok else 'FAIL'} byte-compare Documents/{argv[1]}: remote {len(remote)} bytes, expected {len(local)} bytes")
        return 0 if ok else 1
    elif argv[0] == "clean":
        n = 0
        for it in c.list("Documents"):
            p = it["path"].rstrip("/")
            if p.split("/")[-1].upper().startswith("LKTEST") and not it["is_dir"]:
                c.delete(p)
                n += 1
                print(f"deleted {p}")
        print(f"{n} test file(s) removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
