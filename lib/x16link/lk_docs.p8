; lk_docs.p8 - X16Link docs channel client (bank library X16DOCS.BIN): LIST with a page cache and
; ready-made picker rows, download (OPEN + READ chunks -> SD file, CRC-32, resume after a dropped
; link) and upload (SD file -> SAVE + WRITE chunks + COMMIT, resume, conflict info).
; Talks to the hub through the transport library X16LINK.BIN (bank lkd.LINKBANK) via JSRFAR;
; payloads go through lkut.XB. See ~/x16-office/PROTOCOL.md section 3.4.

%import syslib
%import lk_util
%import lk_crypto

lkd {
    const ubyte LINKBANK = 60

    extsub @bank LINKBANK $A006 = link_connect() clobbers(X, Y) -> ubyte @A
    extsub @bank LINKBANK $A00C = link_get_error(uword dst @R0, ubyte maxn @R1) clobbers(A, X, Y)
    extsub @bank LINKBANK $A00F = link_request(uword chanop @R0, uword plen @R1, uword timeout @R2) clobbers(X, Y) -> ubyte @A
    extsub @bank LINKBANK $A012 = link_reply_len() clobbers(X) -> uword @AY

    const ubyte S_OK = 0
    const ubyte S_CONFLICT = 8
    const ubyte S_TOOLARGE = 16
    const ubyte S_TIMEOUT = 36
    const ubyte S_PROTO = 38
    const ubyte S_DISK = 39
    const ubyte S_CRC = 40
    const ubyte S_OFFLINE = 41
    const ubyte S_BADARG = 42

    const ubyte CH_DOCS = 1
    const ubyte OP_LIST = $10
    const ubyte OP_OPEN = $11
    const ubyte OP_READ = $12
    const ubyte OP_SAVE = $13
    const ubyte OP_WRITE = $14
    const ubyte OP_COMMIT = $15
    const uword CHUNK = 508
    const uword T_REPLY = 600
    const uword T_SLOW = 1800

    ubyte[64] errmsg            ; local errors; empty = ask the link library
    ubyte[48] etag
    ubyte[48] etag2
    ubyte conflict_kind
    uword progress_cb

    sub init() {
        errmsg[0] = 0
        etag[0] = 0
        progress_cb = 0
        ctotal = 0
        cn = 0
    }

    sub seterr(ubyte code, str msg) -> ubyte {
        lkut.scopy(msg, &errmsg, 64)
        return code
    }

    sub get_error(uword dst, ubyte maxn) {
        if errmsg[0] != 0
            lkut.scopy(&errmsg, dst, maxn)
        else
            link_get_error(dst, maxn)
    }

    ; one docs request: payload already built in XB (lkut.tlen); reply parsed from XB
    sub req(ubyte op, uword timeout) -> ubyte {
        errmsg[0] = 0
        ubyte st = link_request(mkword(op, CH_DOCS), lkut.tlen, timeout)
        lkut.g_begin(link_reply_len())
        if st == S_CONFLICT {
            void lkut.g8()
            void lkut.gstr(&etag2, 2)       ; skip msg
            conflict_kind = lkut.g8()
            lkut.gstr(&etag2, 48)
        }
        return st
    }

    ; ---- 32-bit little-endian counters
    sub add32(uword v, uword n) {
        uword lo = mkword(@(v + 1), @(v)) + n
        if lo < n {
            @(v + 2)++
            if @(v + 2) == 0
                @(v + 3)++
        }
        @(v) = lsb(lo)
        @(v + 1) = msb(lo)
    }

    sub lt32(uword a, uword b) -> bool {
        ubyte i = 3
        repeat 4 {
            if @(a + i) != @(b + i)
                return @(a + i) < @(b + i)
            i--
        }
        return false
    }

    sub eq32(uword a, uword b) -> bool {
        ubyte i
        for i in 0 to 3 {
            if @(a + i) != @(b + i)
                return false
        }
        return true
    }

    ; min(b - a, CHUNK) for 32-bit counters a <= b
    ubyte[4] dif
    asmsub remaining(uword a @R0, uword b @R1) clobbers(X) -> uword @AY {
        %asm {{
            ldy  #0
            sec                         ; (iny keeps the borrow; no cpy in this chain)
            lda  (cx16.r1),y
            sbc  (cx16.r0),y
            sta  p8v_dif
            iny
            lda  (cx16.r1),y
            sbc  (cx16.r0),y
            sta  p8v_dif+1
            iny
            lda  (cx16.r1),y
            sbc  (cx16.r0),y
            sta  p8v_dif+2
            iny
            lda  (cx16.r1),y
            sbc  (cx16.r0),y
            sta  p8v_dif+3
            lda  p8v_dif+2
            ora  p8v_dif+3
            bne  _chunk
            lda  p8v_dif+1
            cmp  #>508
            bcc  _diff
            bne  _chunk
            lda  p8v_dif
            cmp  #<508+1
            bcs  _chunk
_diff       lda  p8v_dif
            ldy  p8v_dif+1
            rts
_chunk      lda  #<508
            ldy  #>508
            rts
        }}
    }

    ; app callback: r0 = done >> 8, r1 = total >> 8 (main RAM routine, must not switch RAM banks)
    sub progress(uword done, uword total) {
        if progress_cb != 0 {
            cx16.r0 = mkword(@(done + 2), @(done + 1))
            cx16.r1 = mkword(@(total + 2), @(total + 1))
            void call(progress_cb)
        }
    }

    ; ---------------- LIST with a page cache ----------------
    const ubyte CMAX = 16
    const uword CBYTES = 768
    uword cache = memory("lkd_cache", 768, 0)
    uword[CMAX] coff
    uword cfirst
    ubyte cn
    uword ctotal

    sub list_fetch(uword start) -> ubyte {
        uword used = 0
        ubyte st
        ubyte n
        uword elen
        cfirst = start
        cn = 0
        repeat {
            lkut.p_begin()
            lkut.p16(start + cn)
            lkut.p8(CMAX - cn)
            st = req(OP_LIST, T_SLOW)
            if st != 0
                return st
            ctotal = lkut.g16()
            void lkut.g16()
            n = lkut.g8()
            if lkut.perr
                return seterr(S_PROTO, iso:"bad reply")
            if n == 0
                return S_OK
            while n != 0 {
                if lkut.rpos + 16 > lkut.rlen
                    return seterr(S_PROTO, iso:"bad reply")
                elen = 16 + @(lkut.XB + lkut.rpos + 15)
                if lkut.rpos + elen > lkut.rlen
                    return seterr(S_PROTO, iso:"bad reply")
                if cn >= CMAX or used + elen > CBYTES
                    return S_OK
                coff[cn] = used
                sys.memcopy(lkut.XB + lkut.rpos, cache + used, elen)
                used += elen
                lkut.rpos += elen
                cn++
                n--
            }
            if cfirst + cn >= ctotal
                return S_OK
        }
    }

    ubyte lerr
    sub entry(uword i) -> uword {
        lerr = 0
        if i >= ctotal {
            lerr = seterr(S_BADARG, iso:"no such entry")
            return 0
        }
        if i < cfirst or i >= cfirst + cn {
            uword s = 0
            if i >= 8
                s = i - 8
            lerr = list_fetch(s)
            if lerr != 0
                return 0
            if i < cfirst or i >= cfirst + cn {
                lerr = seterr(S_PROTO, iso:"list changed")
                return 0
            }
        }
        return cache + coff[lsb(i - cfirst)]
    }

    ; 2-digit number (0..99)
    sub put2(uword dst, ubyte v) {
        ubyte t = $30
        while v >= 10 {
            v -= 10
            t++
        }
        @(dst) = t
        @(dst + 1) = $30 + v
    }

    ; right-aligned decimal ending just before endp (no division: subtract powers of ten)
    uword[] pow10 = [10000, 1000, 100, 10, 1]
    sub put_dec(uword endp, uword v) {
        ubyte[5] dg
        ubyte i
        ubyte first = 4
        for i in 0 to 4 {
            dg[i] = 0
            while v >= pow10[i] {
                v -= pow10[i]
                dg[i]++
            }
            if dg[i] != 0 and first == 4
                first = i
        }
        for i in first to 4 {
            @(endp - 5 + i) = $30 + dg[i]
        }
    }

    ; dst[0]=type (1 RTF 2 TXT 3 DOCX 4 XLSX 5 CSV) dst[1]=flags, dst+2: 68-char ISO row + 0 = name(38) size(8) date(16) type(3)
    str tnames = iso:"---RTFTXTDOCXLSCSV"
    sub list_row(uword i, uword dst) -> ubyte {
        uword e = entry(i)
        if e == 0
            return lerr
        @(dst) = @(e)
        @(dst + 1) = @(e + 1)
        uword r = dst + 2
        lkut.fill(r, 68, $20)
        @(r + 68) = 0
        ubyte nl = @(e + 15)
        ubyte k
        for k in 0 to 37 {
            if k < nl
                @(r + k) = @(e + 16 + k)
        }
        if nl > 38
            @(r + 37) = $7e         ; '~' = name continues
        if @(e + 4) == 0 and @(e + 5) == 0 {
            put_dec(r + 46, mkword(@(e + 3), @(e + 2)))
        } else {
            @(r + 45) = $4b         ; K
            put_dec(r + 45, mkword(@(e + 4), @(e + 3)) >> 2)
        }
        if @(e + 11) != 0 {         ; local modification time
            @(r + 48) = $32
            @(r + 49) = $30
            put2(r + 50, @(e + 10))
            @(r + 52) = $2d
            put2(r + 53, @(e + 11))
            @(r + 55) = $2d
            put2(r + 56, @(e + 12))
            put2(r + 59, @(e + 13))
            @(r + 61) = $3a
            put2(r + 62, @(e + 14))
        }
        ubyte ty = @(e)
        if @(e + 1) & 1 != 0 or ty > 5
            ty = 0
        sys.memcopy(&tnames + ty * 3, r + 65, 3)
        return S_OK
    }

    sub list_name(uword i, uword dst, ubyte maxn) -> ubyte {
        uword e = entry(i)
        if e == 0
            return lerr
        ubyte nl = @(e + 15)
        if nl >= maxn
            return seterr(S_BADARG, iso:"name too long")
        sys.memcopy(e + 16, dst, nl)
        @(dst + nl) = 0
        return S_OK
    }

    sub list_begin() -> ubyte {
        ctotal = 0
        cn = 0
        return list_fetch(0)
    }

    ; ---------------- download ----------------
    ubyte[4] dsize
    ubyte[4] dcrc
    ubyte[4] mycrc
    ubyte[4] off
    ubyte[4] osize
    ubyte[4] ocrc

    sub open_remote(uword name, ubyte conv) -> ubyte {
        lkut.p_begin()
        lkut.p8(conv)
        lkut.pstr(name)
        ubyte st = req(OP_OPEN, T_SLOW)
        if st != 0
            return st
        void lkut.g16()
        lkut.gmem(&dsize, 4)
        lkut.gmem(&dcrc, 4)
        void lkut.g16()
        void lkut.g16()
        lkut.gstr(&etag, 48)
        if lkut.perr
            return seterr(S_PROTO, iso:"bad reply")
        return S_OK
    }

    ; after a dropped link: reconnect, OPEN again, go on only if size, CRC-32 and etag are unchanged
    sub reopen(uword name, ubyte conv) -> bool {
        sys.memcopy(&dsize, &osize, 4)
        sys.memcopy(&dcrc, &ocrc, 4)
        lkut.scopy(&etag, &etag2, 48)
        if link_connect() != 0
            return false
        if open_remote(name, conv) != 0
            return false
        return eq32(&dsize, &osize) and eq32(&dcrc, &ocrc) and lkut.seq_str(&etag, &etag2)
    }

    sub download(uword name, uword local, ubyte conv) -> ubyte {
        ubyte st = open_remote(name, conv)
        if st != 0
            return st
        if dsize[2] >= $10 or dsize[3] != 0
            return seterr(S_TOOLARGE, iso:"file is over 1 MiB")
        if not lkut.f_wopen(local)
            return seterr(S_DISK, iso:"temp file error")
        lkc.crc32_init()
        lkut.fill(&off, 4, 0)
        ubyte reconnects = 0
        uword n
        while lt32(&off, &dsize) {
            lkut.p_begin()
            lkut.pmem(&off, 4)
            lkut.p16(CHUNK)
            st = req(OP_READ, T_REPLY)
            if (st == S_TIMEOUT or st == S_OFFLINE) and reconnects < 2 {
                reconnects++
                if reopen(name, conv)
                    continue
                st = seterr(S_TIMEOUT, iso:"link lost")
            }
            if st != 0 {
                lkut.f_wclose()
                return st
            }
            if lkut.rlen <= 4 or not eq32(lkut.XB, &off) {
                lkut.f_wclose()
                return seterr(S_PROTO, iso:"bad reply")
            }
            n = lkut.rlen - 4
            if n > remaining(&off, &dsize) {        ; never accept data past the announced size
                lkut.f_wclose()
                return seterr(S_PROTO, iso:"bad reply")
            }
            if not lkut.f_write(lkut.XB + 4, n) {
                lkut.f_wclose()
                return seterr(S_DISK, iso:"SD write failed")
            }
            lkc.crc32_buf(lkut.XB + 4, n)
            add32(&off, n)
            progress(&off, &dsize)
        }
        lkut.f_wclose()
        lkc.crc32_get(&mycrc)
        if not eq32(&mycrc, &dcrc)
            return seterr(S_CRC, iso:"CRC-32 mismatch")
        return S_OK
    }

    ; ---------------- upload ----------------
    ; mode 0 = create only, 1 = overwrite the version whose etag is in `etag` (set_etag)
    ubyte[4] usize
    ubyte[4] have
    ubyte[4] skip
    ubyte[4] want

    sub upload(uword local, uword remote, ubyte mode, ubyte conv) -> ubyte {
        uword n
        ; pass 1: size + CRC-32 of the local file
        if not lkut.f_ropen(local)
            return seterr(S_DISK, iso:"temp file error")
        lkc.crc32_init()
        lkut.fill(&usize, 4, 0)
        repeat {
            n = lkut.f_read(lkut.XB, 512)
            if n == 0
                break
            lkc.crc32_buf(lkut.XB, n)
            add32(&usize, n)
        }
        lkut.f_rclose()
        lkc.crc32_get(&mycrc)
        if usize[2] >= $10 or usize[3] != 0
            return seterr(S_TOOLARGE, iso:"file is over 1 MiB")
        ubyte reconnects = 0
        ubyte st
restart:
        lkut.p_begin()
        lkut.p8(mode)
        lkut.p8(conv)
        lkut.pmem(&usize, 4)
        lkut.pmem(&mycrc, 4)
        lkut.pstr(remote)
        lkut.pstr(&etag)
        st = req(OP_SAVE, T_REPLY)
        if (st == S_TIMEOUT or st == S_OFFLINE) and reconnects < 2 {
            reconnects++
            if link_connect() == 0
                goto restart
        }
        if st != 0
            return st
        lkut.gmem(&have, 4)
        if lkut.perr or lt32(&usize, &have)
            return seterr(S_PROTO, iso:"bad reply")
        if lt32(&have, &usize) {
            if not lkut.f_ropen(local)
                return seterr(S_DISK, iso:"temp file error")
            lkut.fill(&skip, 4, 0)          ; skip what the hub already has (resume)
            while lt32(&skip, &have) {
                n = lkut.f_read(lkut.XB, remaining(&skip, &have))
                if n == 0
                    break
                add32(&skip, n)
            }
            while lt32(&have, &usize) {
                n = lkut.f_read(lkut.XB + 4, remaining(&have, &usize))
                if n == 0 {
                    lkut.f_rclose()
                    return seterr(S_DISK, iso:"SD read failed")
                }
                sys.memcopy(&have, lkut.XB, 4)
                sys.memcopy(&have, &want, 4)
                add32(&want, n)
                lkut.tlen = n + 4
                st = req(OP_WRITE, T_REPLY)
                if st != 0 {
                    lkut.f_rclose()
                    if (st == S_TIMEOUT or st == S_OFFLINE or st == 12) and reconnects < 2 {
                        reconnects++
                        if st == 12 or link_connect() == 0
                            goto restart        ; SAVE again: the hub says how much it has
                    }
                    return st
                }
                lkut.gmem(&have, 4)
                if lkut.perr or not eq32(&have, &want) {   ; the hub must have taken exactly this chunk
                    lkut.f_rclose()
                    return seterr(S_PROTO, iso:"bad reply")
                }
                progress(&have, &usize)
            }
            lkut.f_rclose()
        }
        lkut.p_begin()
        st = req(OP_COMMIT, T_SLOW)
        if st != 0
            return st
        lkut.gstr(&etag, 48)
        return S_OK
    }
}
