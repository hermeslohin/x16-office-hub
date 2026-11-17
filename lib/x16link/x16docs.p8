; x16docs.p8 - X16Link docs-channel library, built as a bank blob: X16DOCS.BIN (load at $A000 in
; RAM bank 61; needs X16LINK.BIN in bank 60). Arguments in cx16 r0..r3, status in A.

%address  $A000
%memtop   $C000
%output   library

%import lk_docs

main {
    %jmptable (
        lkdx.list_begin,    ; $A003                                              -> A status
        lkdx.list_total,    ; $A006                                              -> AY
        lkdx.list_row,      ; $A009  r0=index r1=dst (72 bytes)                  -> A status
        lkdx.list_name,     ; $A00C  r0=index r1=dst r2L=max                     -> A status
        lkdx.download,      ; $A00F  r0=remote r1=local r2L=conv                 -> A status
        lkdx.upload,        ; $A012  r0=local r1=remote r2L=mode r3L=conv        -> A status
        lkdx.get_etag,      ; $A015  r0=dst r1L=max
        lkdx.set_etag,      ; $A018  r0=src ("" = none)
        lkdx.conflict,      ; $A01B  r0=dst r1L=max: server etag of the conflict -> A kind (1 exists, 2 changed)
        lkdx.set_progress,  ; $A01E  r0=callback (gets r0=done>>8, r1=total>>8)
        lkdx.get_error,     ; $A021  r0=dst r1L=max
    )

    sub start() {
        lkd.init()
    }
}

lkdx {
    sub list_begin() -> ubyte {
        return lkd.list_begin()
    }
    sub list_total() -> uword {
        return lkd.ctotal
    }
    sub list_row() -> ubyte {
        return lkd.list_row(cx16.r0, cx16.r1)
    }
    sub list_name() -> ubyte {
        return lkd.list_name(cx16.r0, cx16.r1, cx16.r2L)
    }
    sub download() -> ubyte {
        return lkd.download(cx16.r0, cx16.r1, cx16.r2L)
    }
    sub upload() -> ubyte {
        return lkd.upload(cx16.r0, cx16.r1, cx16.r2L, cx16.r3L)
    }
    sub get_etag() {
        lkut.scopy(&lkd.etag, cx16.r0, cx16.r1L)
    }
    sub set_etag() {
        lkut.scopy(cx16.r0, &lkd.etag, 48)
    }
    sub conflict() -> ubyte {
        lkut.scopy(&lkd.etag2, cx16.r0, cx16.r1L)
        return lkd.conflict_kind
    }
    sub set_progress() {
        lkd.progress_cb = cx16.r0
    }
    sub get_error() {
        lkd.get_error(cx16.r0, cx16.r1L)
    }
}
