; lk_util.p8 - helpers shared by the X16Link bank libraries (each bank gets its own copy).
; No strings/conv/diskio/math here on purpose: the bank libraries must fit in 8 KB.
;
; XB ($0400-$0607, golden RAM) is the payload exchange buffer between the app, the docs library
; and the link library: every request payload is built there and every reply payload lands there.
; Apps using X16Link must leave $0400-$0607 alone.

%import syslib

lkut {
    const uword XB = $0400
    const uword XBSIZE = 520

    uword tlen          ; payload length being built in XB
    uword rlen          ; reply payload length in XB
    uword rpos
    bool perr

    sub slen(uword s) -> ubyte {
        ubyte n = 0
        while @(s + n) != 0
            n++
        return n
    }

    ; copy a 0-terminated string, at most maxn-1 chars, always terminated
    sub scopy(uword src, uword dst, ubyte maxn) {
        ubyte i = 0
        while @(src + i) != 0 and i < maxn - 1 {
            @(dst + i) = @(src + i)
            i++
        }
        @(dst + i) = 0
    }

    sub seq_str(uword a, uword b) -> bool {
        ubyte i = 0
        repeat {
            if @(a + i) != @(b + i)
                return false
            if @(a + i) == 0
                return true
            i++
        }
    }

    sub fill(uword p, uword n, ubyte v) {
        while n != 0 {
            @(p) = v
            p++
            n--
        }
    }

    ; ---- build a request payload in XB
    sub p_begin() {
        tlen = 0
    }
    sub p8(ubyte b) {
        @(XB + tlen) = b
        tlen++
    }
    sub p16(uword w) {
        p8(lsb(w))
        p8(msb(w))
    }
    sub pmem(uword p, ubyte n) {
        while n != 0 {
            p8(@(p))
            p++
            n--
        }
    }
    sub pstr(uword s) {
        ubyte n = slen(s)
        p8(n)
        pmem(s, n)
    }

    ; ---- parse the reply payload in XB (bounds-checked: overruns set perr)
    sub g_begin(uword n) {
        rlen = n
        rpos = 0
        perr = false
    }
    sub g8() -> ubyte {
        if rpos >= rlen {
            perr = true
            return 0
        }
        ubyte b = @(XB + rpos)
        rpos++
        return b
    }
    sub g16() -> uword {
        ubyte lo = g8()
        return mkword(g8(), lo)
    }
    sub gmem(uword dst, ubyte n) {
        while n != 0 {
            @(dst) = g8()
            dst++
            n--
        }
    }
    sub gstr(uword dst, ubyte maxn) {
        ubyte n = g8()
        ubyte i = 0
        ubyte c
        while n != 0 {
            c = g8()
            if i < maxn - 1 {
                @(dst + i) = c
                i++
            }
            n--
        }
        @(dst + i) = 0
    }

    ; ---- raw KERNAL sequential files on device 8 (diskio is too big for a bank library)
    const ubyte LFN_R = 12
    const ubyte LFN_W = 13
    bool f_eof
    ubyte[72] fname

    ; DOS status is "00"?
    sub dos_ok() -> bool {
        cbm.SETNAM(0, 0)
        cbm.SETLFS(15, 8, 15)
        void cbm.OPEN()
        void cbm.CHKIN(15)
        ubyte c1 = cbm.CHRIN()
        ubyte c2 = cbm.CHRIN()
        while cbm.READST() == 0 {
            if cbm.CHRIN() == 13
                break
        }
        cbm.CLRCHN()
        cbm.CLOSE(15)
        return c1 == $30 and c2 == $30
    }

    sub f_ropen(uword name) -> bool {
        f_eof = false
        cbm.SETNAM(slen(name), name)
        cbm.SETLFS(LFN_R, 8, 2)
        void cbm.OPEN()
        if dos_ok()
            return true
        cbm.CLOSE(LFN_R)
        return false
    }

    ; read up to n bytes; returns the count (0 = end of file or error)
    sub f_read(uword dst, uword n) -> uword {
        uword got = 0
        ubyte st
        if f_eof
            return 0
        void cbm.CHKIN(LFN_R)
        while got < n {
            @(dst + got) = cbm.CHRIN()
            st = cbm.READST()
            if st & $bf != 0 {
                f_eof = true
                break
            }
            got++
            if st != 0 {
                f_eof = true
                break
            }
        }
        cbm.CLRCHN()
        return got
    }

    sub f_rclose() {
        cbm.CLRCHN()
        cbm.CLOSE(LFN_R)
    }

    ; scratch the old file, then open "name,s,w"
    sub f_wopen(uword name) -> bool {
        fname[0] = $53
        fname[1] = $3a
        scopy(name, &fname + 2, 60)
        cbm.SETNAM(slen(&fname), &fname)
        cbm.SETLFS(15, 8, 15)
        void cbm.OPEN()
        cbm.CLOSE(15)
        scopy(name, &fname, 60)
        ubyte n = slen(&fname)
        fname[n] = $2c
        fname[n + 1] = $53
        fname[n + 2] = $2c
        fname[n + 3] = $57
        fname[n + 4] = 0
        cbm.SETNAM(n + 4, &fname)
        cbm.SETLFS(LFN_W, 8, 1)
        void cbm.OPEN()
        if dos_ok()
            return true
        cbm.CLOSE(LFN_W)
        return false
    }

    sub f_write(uword src, uword n) -> bool {
        void cbm.CHKOUT(LFN_W)
        while n != 0 {
            cbm.CHROUT(@(src))
            src++
            n--
        }
        bool ok = cbm.READST() == 0
        cbm.CLRCHN()
        return ok
    }

    sub f_wclose() {
        cbm.CLRCHN()
        cbm.CLOSE(LFN_W)
    }
}
