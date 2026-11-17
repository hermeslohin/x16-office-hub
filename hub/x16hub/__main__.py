"""CLI: python -m x16hub {oc ...|serve|link ...|config ...}"""
import argparse
import logging
import os
import sys

from .store import HubError


def cmd_check(_):
    from .selftest import run_check
    return run_check()


def _client():
    from .backend import owncloud as oc
    return oc.OwnCloud(oc.load_config())


def cmd_calendars(_):
    for c in _client().calendars():
        print(f"{c['name']:<12} {c['displayname'] or '':<16} ctag={c['ctag']} sync-token={c['sync_token']} "
              f"comps={','.join(c['components'])}")


def cmd_ls(a):
    for i in _client().list(a.path):
        print(f"{'d' if i['is_dir'] else '-'} {i['size']:>10} {i['last_modified'] or '':<31} {i['etag'] or '':<36} {i['path']}")


def cmd_serve(a):
    import asyncio
    from .link import keys as K
    from .link.server import serve_forever
    cfg = K.get_config()
    host = a.bind or cfg["bind"]
    port = a.port or int(cfg["port"])
    K.validate_config("bind", host)
    if not K.load_keys():
        print(f"warning: no device keys in {K.keys_path()} (python -m x16hub link key new <devid>)", file=sys.stderr)
    try:
        asyncio.run(serve_forever(host, port, allow=K.parse_allow(cfg["allow"]), hubname=cfg["hubname"],
                                  calendar=cfg["calendar"]))
    except KeyboardInterrupt:
        pass


def cmd_key_new(a):
    from .link import keys as K
    from pathlib import Path
    key = K.new_key(a.devid)
    print(f"key for {a.devid} stored in {K.keys_path()}")
    if a.sdcard:
        cfg = K.get_config()
        host = a.host or f"{cfg['bind']}:{cfg['port']}"
        out = K.write_sd_keyfile(Path(a.sdcard), host, a.devid, key)
        print(f"wrote {out} (HOST={host}); copy it to the X16 SD card next to the app")


def cmd_key_list(_):
    from .link import keys as K
    for d in sorted(K.load_keys()):
        print(d)


def cmd_key_rm(a):
    from .link import keys as K
    print("removed" if K.remove_key(a.devid) else "no such device")


def cmd_config(a):
    from .link import keys as K
    if a.action == "set":
        K.set_config(a.key, a.value)
    elif a.action == "unset":
        K.set_config(a.key, None)
    for k, v in K.get_config().items():
        print(f"{k}={v}")


def _link_client(a):
    from .link import keys as K
    from .link.client import Client
    cfg = K.get_config()
    keys = K.load_keys()
    dev = a.device or (sorted(keys)[0] if keys else None)
    if not dev or dev not in keys:
        raise HubError("no such device key (python -m x16hub link key new <devid>)")
    host, port = (a.hub.rsplit(":", 1) if a.hub else (cfg["bind"], cfg["port"]))
    return Client.connect(host, int(port), dev, keys[dev])


def cmd_link_ls(a):
    import asyncio

    async def go():
        c = await _link_client(a)
        total, items = await c.list()
        await c.close()
        kinds = {0: "other", 1: "rtf", 2: "txt"}
        for e in items:
            y, mo, d, h, mi = e.local
            print(f"{kinds[e.type]:<5} {'U' if e.flags & 1 else ' '} {e.size:>8} 20{y:02d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d} {e.name}")
        print(f"{total} file(s)")
    asyncio.run(go())


def main(argv=None):
    os.umask(0o077)  # sqlite journals etc. must never be group/world readable
    from .backend.owncloud import install_log_redaction
    logging.basicConfig(level=os.environ.get("X16HUB_LOG", "WARNING"), format="%(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    p = argparse.ArgumentParser(prog="python -m x16hub")
    sub = p.add_subparsers(dest="group", required=True)
    op = sub.add_parser("oc", help="ownCloud backend (WebDAV + CalDAV)")
    osub = op.add_subparsers(dest="cmd", required=True)
    osub.add_parser("check", help="end-to-end self-test (cleans up after itself)").set_defaults(fn=cmd_check)
    osub.add_parser("calendars", help="list calendars").set_defaults(fn=cmd_calendars)
    ls = osub.add_parser("ls", help="list a WebDAV folder")
    ls.add_argument("path", nargs="?", default="X16")
    ls.set_defaults(fn=cmd_ls)
    sp = sub.add_parser("serve", help="run the X16Link TCP server (port 6502)")
    sp.add_argument("--bind", help="override the configured bind address")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cmd_serve)
    lp = sub.add_parser("link", help="X16Link keys and test client")
    lsub = lp.add_subparsers(dest="cmd", required=True)
    kp = lsub.add_parser("key", help="device pre-shared keys")
    ksub = kp.add_subparsers(dest="kcmd", required=True)
    kn = ksub.add_parser("new", help="create/replace a device key")
    kn.add_argument("devid")
    kn.add_argument("--sdcard", help="also write X16LINK.KEY into this directory")
    kn.add_argument("--host", help="HOST=host:port to put in X16LINK.KEY (default: configured bind:port)")
    kn.set_defaults(fn=cmd_key_new)
    ksub.add_parser("list").set_defaults(fn=cmd_key_list)
    kr = ksub.add_parser("rm")
    kr.add_argument("devid")
    kr.set_defaults(fn=cmd_key_rm)
    ll = lsub.add_parser("ls", help="list Documents/ through a running hub (reference client)")
    ll.add_argument("--hub", help="host:port (default: configured bind:port)")
    ll.add_argument("--device")
    ll.set_defaults(fn=cmd_link_ls)
    cp = sub.add_parser("config", help="hub link config (stored in hub.db)")
    cp.add_argument("action", choices=["show", "set", "unset"])
    cp.add_argument("key", nargs="?")
    cp.add_argument("value", nargs="?")
    cp.set_defaults(fn=cmd_config)
    a = p.parse_args(argv)
    try:
        return a.fn(a) or 0
    except HubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
