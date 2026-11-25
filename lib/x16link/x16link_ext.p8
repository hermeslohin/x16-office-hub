; x16link_ext.p8 - entry points of the X16Link bank libraries (no loader; usable from other bank libraries).
; X16LINK.BIN in RAM bank 60, X16DOCS.BIN in RAM bank 61. See x16link_api.p8.
; Every pointer passed in (names, destination buffers) must be in main RAM or golden RAM, never in
; banked RAM: the library runs with its own bank mapped at $A000.
; Golden RAM map: $0400-$0607 XB payload buffer, $0608-$06FB free for callers ($06FC-$06FF: the UART read stub) (X16 Write's cloud
; library uses $0608-$06F3), $0700-$07FF UART receive ring.

lkapi {
    const ubyte LINKBANK = 60
    const ubyte DOCSBANK = 61
    const ubyte S_CONFLICT = 8

    extsub @bank LINKBANK $A000 = link_init() clobbers(A, X, Y)
    extsub @bank LINKBANK $A003 = load_key(str fname @R0) clobbers(X, Y) -> ubyte @A
    extsub @bank LINKBANK $A006 = connect() clobbers(X, Y) -> ubyte @A
    extsub @bank LINKBANK $A009 = hangup() clobbers(A, X, Y)
    extsub @bank LINKBANK $A00C = link_error(uword dst @R0, ubyte maxn @R1) clobbers(A, X, Y)
    extsub @bank LINKBANK $A00F = request(uword chanop @R0, uword plen @R1, uword timeout @R2) clobbers(X, Y) -> ubyte @A
    extsub @bank LINKBANK $A012 = reply_len() clobbers(X) -> uword @AY
    extsub @bank LINKBANK $A015 = is_online() clobbers(X, Y) -> bool @A
    extsub @bank LINKBANK $A01B = forget() clobbers(A, X, Y)

    extsub @bank DOCSBANK $A000 = docs_init() clobbers(A, X, Y)
    extsub @bank DOCSBANK $A003 = list_begin() clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A006 = list_total() clobbers(X) -> uword @AY
    extsub @bank DOCSBANK $A009 = list_row(uword index @R0, uword dst @R1) clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A00C = list_name(uword index @R0, uword dst @R1, ubyte maxn @R2) clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A00F = download(str remote @R0, str local @R1, ubyte conv @R2) clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A012 = upload(str local @R0, str remote @R1, ubyte mode @R2, ubyte conv @R3) clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A015 = get_etag(uword dst @R0, ubyte maxn @R1) clobbers(A, X, Y)
    extsub @bank DOCSBANK $A018 = set_etag(str src @R0) clobbers(A, X, Y)
    extsub @bank DOCSBANK $A01B = conflict(uword dst @R0, ubyte maxn @R1) clobbers(X, Y) -> ubyte @A
    extsub @bank DOCSBANK $A01E = set_progress(uword callback @R0) clobbers(A, X, Y)
    extsub @bank DOCSBANK $A021 = get_error(uword dst @R0, ubyte maxn @R1) clobbers(A, X, Y)
}
