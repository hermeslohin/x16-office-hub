; x16link.p8 - X16Link v1 transport library, built as a bank blob: X16LINK.BIN (load at $A000 in
; RAM bank 60, see x16link_api.p8). Arguments in cx16 r0..r3, status in A. Payloads in lkut.XB ($0400).

%address  $A000
%memtop   $C000
%output   library

%import lk_link

main {
    %jmptable (
        lkx.load_key,       ; $A003  r0=filename (main RAM)                         -> A status
        lkx.connect,        ; $A006                                                 -> A status
        lkx.hangup,         ; $A009
        lkx.get_error,      ; $A00C  r0=dst, r1L=max     (0-terminated ISO text)
        lkx.request,        ; $A00F  r0L=chan r0H=op r1=payload len in XB r2=timeout -> A status
        lkx.reply_len,      ; $A012                                                 -> AY
        lkx.is_online,      ; $A015                                                 -> A
        lkx.version,        ; $A018                                                 -> A
        lkx.forget,         ; $A01B  wipe key/host from RAM (load_key again before connect)
    )

    sub start() {
        lk.init()
    }
}

lkx {
    sub load_key() -> ubyte {
        return lk.load_key(cx16.r0)
    }
    sub connect() -> ubyte {
        return lk.connect()
    }
    sub hangup() {
        lk.hangup()
    }
    sub get_error() {
        lkut.scopy(&lk.errmsg, cx16.r0, cx16.r1L)
    }
    sub request() -> ubyte {
        return lk.request(cx16.r0L, cx16.r0H, cx16.r1, cx16.r2)
    }
    sub reply_len() -> uword {
        return lk.rlen
    }
    sub is_online() -> bool {
        return lk.online
    }
    sub forget() {
        lk.forget()
    }
    sub version() -> ubyte {
        return lk.VERSION
    }
}
