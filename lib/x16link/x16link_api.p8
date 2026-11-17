; x16link_api.p8 - app side of X16Link: loads the two bank libraries and declares their entry points.
;   X16LINK.BIN -> RAM bank 60 (transport: key file, dial, auth, request)
;   X16DOCS.BIN -> RAM bank 61 (docs channel: list/download/upload)
; Apps must leave golden RAM $0400-$07FF alone (payload buffer XB $0400-$0607, UART ring $0700-$07FF)
; and must not use RAM banks 60/61. Status codes: 0 ok, 1..16 hub errors (PROTOCOL.md 3.1),
; 32.. local (see lk_link.p8). Strings passed in must be in main RAM.

%import diskio
%import x16link_ext

lkload {
    bool loaded = false
    bool link_loaded = false
    ; load only the transport library (apps that use their own channel code, e.g. lkcal)
    sub load_link() -> bool {
        if link_loaded or loaded
            return true
        ubyte old = cx16.getrambank()
        cx16.rambank(lkapi.LINKBANK)
        uword e1 = diskio.load_raw(iso:"X16LINK.BIN", $a000)
        cx16.rambank(old)
        if e1 == 0
            return false
        lkapi.link_init()
        link_loaded = true
        return true
    }

    ; load both libraries from the SD card into their banks and initialise them
    sub load() -> bool {
        if loaded
            return true
        ubyte old = cx16.getrambank()
        cx16.rambank(lkapi.LINKBANK)
        uword e1 = diskio.load_raw(iso:"X16LINK.BIN", $a000)
        cx16.rambank(lkapi.DOCSBANK)
        uword e2 = diskio.load_raw(iso:"X16DOCS.BIN", $a000)
        cx16.rambank(old)
        if e1 == 0 or e2 == 0
            return false
        lkapi.link_init()
        lkapi.docs_init()
        loaded = true
        return true
    }
}
