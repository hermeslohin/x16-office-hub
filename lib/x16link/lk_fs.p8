; lk_fs.p8 - folder operations of the X16Link docs channel: DIR (one folder, paged, with a small
; entry cache), MKDIR, DELETE, MOVE, COPY (PROTOCOL.md 3.4). X16DOCS.BIN is full, so this module is
; compiled into the program that uses it (an app, or an app's bank library) and talks to the
; transport X16LINK.BIN (bank 60) directly through lkapi.request. Payloads go through XB ($0400).
; The caller loads the key and connects first (lkapi.load_key + lkapi.connect); if the hub dropped
; an idle session, each op reconnects once by itself.
;
; Paths are relative to ownCloud Documents/, '/'-separated, ISO-8859-15, at most 63 bytes
; ("" = Documents/ itself, only for dir_begin). Status codes as lkapi (0 ok, 1-16 hub, 32+ local).

%import lk_util
%import x16link_ext

lkfs {
    %option ignore_unused

    const ubyte CH_DOCS = 1
    const ubyte OP_DIR = $16
    const ubyte OP_MKDIR = $17
    const ubyte OP_DELETE = $18
    const ubyte OP_MOVE = $19
    const ubyte OP_COPY = $1a
    const ubyte S_PROTO = 38
    const ubyte S_OFFLINE = 41
    const ubyte S_TIMEOUT = 36
    const uword T_REPLY = 600
    const uword T_SLOW = 1800

    ; entry layout (as on the wire): type, flags, size u32, mtime u32, y-2000, mo, d, h, mi, nlen, name
    const ubyte E_TYPE = 0
    const ubyte E_FLAGS = 1
    const ubyte E_SIZE = 2
    const ubyte E_DATE = 10
    const ubyte E_NLEN = 15
    const ubyte E_NAME = 16
    const ubyte T_DIR = 6           ; type of a folder
    const ubyte FL_UNSUPPORTED = 1  ; can't be transferred (name too long/lossy, file over 1 MiB)

    const ubyte CMAX = 16           ; entries cached (one DIR reply's worth, at most CBYTES)
    const uword CBYTES = 320        ; small: the module also lives in apps' 8 KB bank libraries
    uword cache = memory("lkfs_cache", CBYTES, 0)
    uword[CMAX] coff
    uword cfirst
    ubyte cn
    uword total                     ; entries in the folder last listed
    ubyte ckind                     ; CONFLICT kind after status 8 (1 = exists, 2 = changed)
    ubyte lerr
    ubyte[64] dpath                 ; folder last listed (pages are fetched from the hub's snapshot)

    ; one docs request with the payload built in XB; reconnects once if the hub dropped the session
    sub req(ubyte op, uword timeout) -> ubyte {
        uword plen = lkut.tlen
        ubyte st
        if not lkapi.is_online() {
            st = lkapi.connect()
            if st != 0
                return st
        }
        st = lkapi.request(mkword(op, CH_DOCS), plen, timeout)
        if st == S_OFFLINE or st == S_TIMEOUT {
            if lkapi.connect() == 0
                st = lkapi.request(mkword(op, CH_DOCS), plen, timeout)
        }
        lkut.g_begin(lkapi.reply_len())
        ckind = 0
        if st == 8 {
            void lkut.g8()                  ; code
            lkut.rpos += lkut.g8()          ; msg
            ckind = lkut.g8()
        }
        return st
    }

    ; ---------------- DIR ----------------
    sub fetch(uword start) -> ubyte {
        lkut.p_begin()
        lkut.p16(start)
        lkut.p8(CMAX)
        lkut.pstr(&dpath)
        ubyte st = req(OP_DIR, T_SLOW)
        cn = 0
        cfirst = start
        if st != 0
            return st
        total = lkut.g16()
        void lkut.g16()
        ubyte n = lkut.g8()
        if lkut.perr or n > CMAX
            return S_PROTO
        uword used = 0
        uword elen
        while n != 0 {
            if lkut.rpos + E_NAME > lkut.rlen
                return S_PROTO
            elen = E_NAME + @(lkut.XB + lkut.rpos + E_NLEN)
            if lkut.rpos + elen > lkut.rlen
                return S_PROTO
            if used + elen > CBYTES
                break                       ; cache full: the rest comes with the next fetch
            coff[cn] = used
            sys.memcopy(lkut.XB + lkut.rpos, cache + used, elen)
            used += elen
            lkut.rpos += elen
            cn++
            n--
        }
        if cn == 0 and total != 0 and start < total
            return S_PROTO
        return 0
    }

    ; list folder `path` ("" = Documents/): total entries in lkfs.total, folders first
    sub dir_begin(uword path) -> ubyte {
        lkut.scopy(path, &dpath, 64)
        total = 0
        return fetch(0)
    }

    ; pointer to entry i of the folder (valid until the next lkfs call), 0 = error (status in lerr)
    sub entry(uword i) -> uword {
        lerr = 0
        if i >= total {
            lerr = 42
            return 0
        }
        if i < cfirst or i >= cfirst + cn {
            uword s = 0
            if i >= 8
                s = i - 8
            lerr = fetch(s)
            if lerr == 0 and (i < cfirst or i >= cfirst + cn)
                lerr = S_PROTO
            if lerr != 0
                return 0
        }
        return cache + coff[lsb(i - cfirst)]
    }

    sub is_dir(uword e) -> bool {
        return @(e + E_TYPE) == T_DIR
    }

    ; entry name -> dst (0-terminated, cut to maxn-1)
    sub name(uword e, uword dst, ubyte maxn) {
        ubyte n = @(e + E_NLEN)
        if n >= maxn
            n = maxn - 1
        sys.memcopy(e + E_NAME, dst, n)
        @(dst + n) = 0
    }

    ; ---------------- MKDIR / DELETE / MOVE / COPY ----------------
    sub one(ubyte op, uword path) -> ubyte {
        lkut.p_begin()
        lkut.pstr(path)
        return req(op, T_SLOW)
    }

    sub mkdir(uword path) -> ubyte {
        return one(OP_MKDIR, path)
    }

    ; deletes a file, or a folder with everything in it
    sub delete(uword path) -> ubyte {
        return one(OP_DELETE, path)
    }

    ; mode 0 = fail with status 8 (CONFLICT) if dst exists, 1 = replace it
    sub two(ubyte op, ubyte mode, uword src, uword dst) -> ubyte {
        lkut.p_begin()
        lkut.p8(mode)
        lkut.pstr(src)
        lkut.pstr(dst)
        return req(op, T_SLOW)
    }

    sub move(ubyte mode, uword src, uword dst) -> ubyte {
        return two(OP_MOVE, mode, src, dst)
    }

    sub copy(ubyte mode, uword src, uword dst) -> ubyte {
        return two(OP_COPY, mode, src, dst)
    }

    ; ---------------- path helpers ----------------
    ; dst = dir + "/" + leaf (or just leaf when dir is ""); false if longer than 63 bytes
    sub join(uword dir, uword leaf, uword dst) -> bool {
        ubyte a = lkut.slen(dir)
        ubyte b = lkut.slen(leaf)
        ubyte sep = 0
        if a != 0
            sep = 1
        if a + sep + b > 63 or b == 0
            return false
        sys.memcopy(dir, dst, a)
        if sep != 0
            @(dst + a) = iso:'/'
        sys.memcopy(leaf, dst + a + sep, b)
        @(dst + a + sep + b) = 0
        return true
    }

    ; cut the last segment off a path in place ("a/b" -> "a", "a" -> "")
    sub up(uword path) {
        ubyte n = lkut.slen(path)
        while n != 0 {
            n--
            if @(path + n) == iso:'/'
                break
        }
        @(path + n) = 0
    }

    ; pointer to the last segment of a path
    sub leaf(uword path) -> uword {
        ubyte n = lkut.slen(path)
        while n != 0 {
            if @(path + n - 1) == iso:'/'
                return path + n
            n--
        }
        return path
    }
}
