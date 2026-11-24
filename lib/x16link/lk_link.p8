; lk_link.p8 - X16Link v1 transport (bank library X16LINK.BIN): key file, Zimodem dial,
; framing + CRC-16, retransmission, HELLO + mutual HMAC-SHA256 auth, TIME -> RTC, BYE/hangup.
; Payloads are exchanged through lkut.XB (golden RAM) so any app or bank library can use it.
; See ~/x16-office/PROTOCOL.md.

%import syslib
%import lk_util
%import lk_crypto
%import lk_uart

lk {
    const ubyte VERSION = 1

    ; status codes: 0 ok, 1..16 = hub ERR codes (PROTOCOL.md 3.1), 32.. = local
    const ubyte S_OK = 0
    const ubyte S_NOKEY = 32
    const ubyte S_NOUART = 33
    const ubyte S_NOMODEM = 34
    const ubyte S_NOCONNECT = 35
    const ubyte S_TIMEOUT = 36
    const ubyte S_HUBAUTH = 37
    const ubyte S_PROTO = 38
    const ubyte S_OFFLINE = 41
    const ubyte S_BADARG = 42

    const ubyte OP_HELLO = $01
    const ubyte OP_AUTH = $02
    const ubyte OP_TIME = $04
    const ubyte OP_BYE = $05
    const ubyte OP_ERR = $7f

    const uword MAXRX = 512             ; largest payload we accept (announced in HELLO)
    const uword T_REPLY = 600           ; 10 s

    uword rxb = memory("lk_rxb", 520, 0)
    uword rlen
    ubyte seq
    bool online

    ubyte[48] host
    ubyte[17] devid
    ubyte[32] key
    bool have_key
    ubyte[16] cnonce
    ubyte nonce_ctr
    ubyte[16] snonce
    &ubyte[32] tag = $05e0          ; golden RAM (XB+480), see lk_crypto ws
    ubyte[64] errmsg

    sub init() {
        online = false
        have_key = false
        errmsg[0] = 0
    }

    sub seterr(ubyte code, str msg) -> ubyte {
        lkut.scopy(msg, &errmsg, 64)
        return code
    }

    ; ---------------- framing ----------------
    ; frame = len16 chan seq op | payload (from XB) | crc16 ; the payload stays in XB for retransmits
    ubyte[5] hdr
    sub send_frame(ubyte chan, ubyte op, uword plen) {
        hdr[0] = lsb(plen)
        hdr[1] = msb(plen)
        hdr[2] = chan
        hdr[3] = seq
        hdr[4] = op
        lkc.crc16_init()
        lkc.crc16_buf(&hdr, 5)
        lkc.crc16_buf(lkut.XB, plen)
        lku.send(&hdr, 5)
        lku.send(lkut.XB, plen)
        lku.put(lkc.crclo)
        lku.put(lkc.crchi)
    }

    const ubyte R_OK = 0
    const ubyte R_TIMEOUT = 1
    const ubyte R_BAD = 2
    const ubyte R_HANGUP = 3

    ; "NO CARRIER" detector over every byte of a frame that turned out bad. A peer can send a
    ; partial header and hang up, so Zimodem's NO CARRIER lands inside the "frame": without this the
    ; X16 would retransmit into the modem's command mode (payload bytes parsed as AT commands).
    ubyte ncm
    bool nc_seen
    asmsub nc_feed(ubyte c @A) clobbers(A, X) {
        %asm {{
            ldx  p8v_ncm
            cmp  _nc,x
            bne  _miss
            inx
            cpx  #10
            bne  _keep
            lda  #1
            sta  p8v_nc_seen
            ldx  #0
_keep       stx  p8v_ncm
            rts
_miss       ldx  #0
            cmp  #$4e
            bne  _keep
            inx
            bra  _keep
_nc         .byte $4e, $4f, $20, $43, $41, $52, $52, $49, $45, $52     ; ISO "NO CARRIER"
            ; !notreached!
        }}
    }

    ; a frame went wrong: look at what we got + drain the line (<= 3 s) watching for NO CARRIER
    sub bad_frame(uword got) -> ubyte {
        uword i = 0
        while i < got {
            nc_feed(@(rxb + i))
            i++
        }
        uword t0 = cbm.RDTIM16()
        while lku.getb(18) {
            nc_feed(lku.getb_val)
            if cbm.RDTIM16() - t0 >= 180
                break
        }
        if nc_seen
            return R_HANGUP
        return R_BAD
    }

    sub recv_frame(uword timeout) -> ubyte {
        ncm = 0
        nc_seen = false
        if not lku.getb(timeout)
            return R_TIMEOUT
        ubyte lo = lku.getb_val
        @(rxb) = lo
        if not lku.getb(120)
            return bad_frame(1)
        ubyte hi = lku.getb_val
        @(rxb + 1) = hi
        if lo == 13 and hi == 10
            return R_HANGUP         ; "\r\nNO CARRIER": Zimodem is back in command mode
        rlen = mkword(hi, lo)
        if rlen > MAXRX
            return bad_frame(2)
        uword i = 2
        uword total = rlen + 7
        while i < total {
            if not lku.getb(120)
                return bad_frame(i)
            @(rxb + i) = lku.getb_val
            i++
        }
        lkc.crc16_init()
        lkc.crc16_buf(rxb, rlen + 5)
        if @(rxb + 5 + rlen) != lkc.crclo or @(rxb + 6 + rlen) != lkc.crchi
            return bad_frame(total)
        return R_OK
    }

    ; Send the plen-byte payload in XB as (chan, op), wait for the reply and copy its payload to XB
    ; (rlen bytes). A hub ERR reply (code, msg, extra) is copied to XB as well and its code returned,
    ; and the message goes to errmsg. Retransmits keep the same SEQ: the hub replays, never re-runs.
    sub request(ubyte chan, ubyte op, uword plen, uword timeout) -> ubyte {
        if not online
            return seterr(S_OFFLINE, iso:"not connected")
        if plen > MAXRX
            return seterr(S_BADARG, iso:"too long")
        seq++
        if seq == $ff
            seq = 0
        ubyte tries = 0
        ubyte junk = 0              ; frames that weren't our reply: a hostile peer can't stall us forever
        ubyte r
        ubyte code
        ubyte rseq
resend:
        send_frame(chan, op, plen)
        tries++
again:
        if junk >= 8 {
            drop()
            return seterr(S_PROTO, iso:"hub sends unexpected frames")
        }
        r = recv_frame(timeout)
        if r == R_OK {
            rseq = @(rxb + 3)
            if @(rxb + 4) == OP_ERR {
                code = @(rxb + 5)
                if rseq == $ff and (code == 2 or code == 3) and tries < 3
                    goto resend         ; our frame arrived damaged: send it again
                if rseq != seq and rseq != $ff {
                    junk++
                    goto again
                }
                sys.memcopy(rxb + 5, lkut.XB, rlen)
                lkut.g_begin(rlen)
                void lkut.g8()
                lkut.gstr(&errmsg, 64)
                if code == 6 or code == 15 or rseq == $ff
                    online = false
                if code == 0 or lkut.perr or rlen == 0
                    return seterr(S_PROTO, iso:"bad reply")
                return code
            }
            if rseq != seq or @(rxb + 4) != (op | $80) or @(rxb + 2) != chan {
                junk++
                goto again          ; late duplicate of an earlier reply
            }
            sys.memcopy(rxb + 5, lkut.XB, rlen)
            return S_OK
        }
        if r == R_HANGUP {
            online = false
            return seterr(S_OFFLINE, iso:"hub hung up")
        }
        if r == R_BAD
            junk++
        if tries < 3
            goto resend
        online = false
        return seterr(S_TIMEOUT, iso:"hub not answering")
    }

    ; ---------------- key file ----------------
    asmsub hexval(ubyte c @A) -> ubyte @A {
        %asm {{
            cmp  #$30
            bcc  _bad
            cmp  #$3a
            bcs  _alpha
            sbc  #$2f               ; C=0 here: A - $30
            rts
_alpha      ora  #$20
            cmp  #$61
            bcc  _bad
            cmp  #$67
            bcs  _bad
            sbc  #$56               ; C=0 here: A - $57 ('a' -> 10)
            rts
_bad        lda  #255
            rts
        }}
    }

    ; A-Z a-z 0-9 - _
    asmsub idchar(ubyte c @A) -> bool @A {
        %asm {{
            cmp  #$2d
            beq  _yes
            cmp  #$5f
            beq  _yes
            cmp  #$30
            bcc  _no
            cmp  #$3a
            bcc  _yes
            and  #$df
            cmp  #$41
            bcc  _no
            cmp  #$5b
            bcc  _yes
_no         lda  #0
            rts
_yes        lda  #1
            rts
        }}
    }

    ; HOST may only hold A-Z a-z 0-9 . - :  (nothing that could end ATD"..." or inject a command)
    sub is_tag(uword p, str t) -> bool {
        ubyte i = 0
        while t[i] != 0 {
            if @(p + i) != t[i]
                return false
            i++
        }
        return true
    }

    ; key file: HOST=host:port / DEV=id / KEY=64 hex   (one per line, LF or CR line ends)
    uword kp
    sub load_key(uword name) -> ubyte {
        have_key = false
        if not lkut.f_ropen(name)
            return seterr(S_NOKEY, iso:"no X16LINK.KEY on SD card")
        uword n = lkut.f_read(rxb, 400)
        lkut.f_rclose()
        @(rxb + n) = 0
        kp = rxb
        ubyte got = 0
        while @(kp) != 0 {
            if is_tag(kp, iso:"HOST=")
                got |= grab(5, &host, 47, 1)
            else if is_tag(kp, iso:"DEV=")
                got |= grab(4, &devid, 16, 2)
            else if is_tag(kp, iso:"KEY=")
                got |= grab(4, &key, 32, 4)
            while not eol(@(kp))
                kp++
            while @(kp) == 10 or @(kp) == 13
                kp++
        }
        lkut.fill(rxb, 420, 0)          ; don't leave the key text lying around
        if got != 7 {                   ; missing field, or $80 = a value too long / bad character
            forget()
            return seterr(S_NOKEY, iso:"bad X16LINK.KEY")
        }
        have_key = true
        return S_OK
    }

    ; read one key-file value at kp+skip into dst: bit 1 = HOST chars, 2 = DEV chars, 4 = 64 hex digits.
    ; The value must fill the line; returns bit, or $80 if it's empty, too long or has a bad character.
    sub grab(ubyte skip, uword dst, ubyte maxn, ubyte bit) -> ubyte {
        kp += skip
        ubyte i = 0
        ubyte c
        ubyte h
        repeat {
            c = @(kp)
            if bit == 4 {
                h = hexval(c)
                c = hexval(@(kp + 1))
                if h == 255 or c == 255
                    break
                c |= h << 4
                kp++
            } else if not idchar(c) {
                if bit == 2 or (c != $2e and c != $3a)
                    break                   ; HOST also allows . and :  (never " or CR: no ATD injection)
            }
            if i == maxn
                return $80
            @(dst + i) = c
            i++
            kp++
        }
        if bit != 4
            @(dst + i) = 0
        if i == 0 or not eol(@(kp)) or (bit == 4 and i != 32)
            return $80
        return bit
    }

    sub eol(ubyte c) -> bool {
        return c == 0 or c == 10 or c == 13
    }

    ; wipe the key material (call when the app is done with the link)
    sub forget() {
        lkut.fill(&key, 32, 0)
        have_key = false
    }

    ; ---------------- connect / auth / hang up ----------------
    sub modem_ok() -> bool {
        lku.puts(iso:"AT\r")
        return lku.expect(iso:"OK", 0, 120)
    }

    sub transcript() {
        lkc.sha_update(&devid, lkut.slen(&devid))
        lkc.sha_update(&cnonce, 16)
        lkc.sha_update(&snonce, 16)
        lkc.sha_byte(VERSION)
    }

    sub connect() -> ubyte {
        ubyte st
        ubyte i
        if not have_key
            return seterr(S_NOKEY, iso:"no key loaded")
        if not lku.init()
            return seterr(S_NOUART, iso:"no serial card at $9FE0")
        online = false
        sys.wait(6)
        lku.drain(6)
        if not modem_ok() {
            lku.hangup_modem()          ; maybe still in a data call from earlier
            if not modem_ok()
                return seterr(S_NOMODEM, iso:"modem not answering")
        }
        ; E0 = no echo; F0 = force RTS/CTS flow control: under XON/XOFF (or none) Zimodem eats
        ; $11/$13 or the UART overruns, so replies holding those bytes never arrive intact
        lku.puts(iso:"ATE0F0\r")
        void lku.expect(iso:"OK", 0, 120)
        lku.drain(6)
        lku.puts(iso:"ATD\"")
        lku.puts(&host)
        lku.puts(iso:"\"\r")
        if not lku.expect(iso:"CONNECT", iso:"NO CARRIER", 900)
            return seterr(S_NOCONNECT, iso:"cannot reach the hub")
        lku.drain(15)
        online = true
        seq = 0
        ; HELLO: magic, version, maxrx, devid, client nonce
        ubyte e1
        ubyte e2
        ubyte e3
        ; cnonce = previous cnonce XOR fresh entropy, byte 0 also + a per-run counter: unique within a
        ; run even if entropy_get is weak (the hub's snonce comes from a CSPRNG either way)
        nonce_ctr++
        cnonce[0] += nonce_ctr
        for i in 0 to 15 {
            e1, e2, e3 = cx16.entropy_get()
            cnonce[i] ^= e1 ^ e2 ^ e3 ^ lsb(cbm.RDTIM16()) ^ @($9f28)
        }
        lkut.p_begin()
        lkut.pmem(iso:"X16L", 4)
        lkut.p8(VERSION)
        lkut.p16(MAXRX)
        lkut.pstr(&devid)
        lkut.pmem(&cnonce, 16)
        st = request(0, OP_HELLO, lkut.tlen, T_REPLY)
        if st != 0 {
            drop()
            return st
        }
        lkut.g_begin(rlen)
        if lkut.g8() != VERSION {
            drop()
            return seterr(S_PROTO, iso:"wrong hub version")
        }
        void lkut.g16()
        lkut.gmem(&snonce, 16)
        if lkut.perr {
            drop()
            return seterr(S_PROTO, iso:"bad reply")
        }
        ; AUTH: HMAC-SHA256(K, "X16L1C" | devid | cnonce | snonce | ver)
        lkc.hmac_begin(&key)
        lkc.sha_update(iso:"X16L1C", 6)
        transcript()
        lkc.hmac_end(lkut.XB)
        st = request(0, OP_AUTH, 32, T_REPLY)
        if st != 0 {
            drop()
            return st
        }
        ; the hub must prove it knows K too (no fake hubs)
        lkc.hmac_begin(&key)
        lkc.sha_update(iso:"X16L1S", 6)
        transcript()
        lkc.hmac_end(&tag)
        bool same = rlen == 32
        for i in 0 to 31 {
            if @(lkut.XB + i) != tag[i]
                same = false
        }
        if not same {
            drop()
            return seterr(S_HUBAUTH, iso:"hub failed auth")
        }
        ; TIME -> RTC (America/New_York local time)
        if request(0, OP_TIME, 0, T_REPLY) == 0 {
            lkut.g_begin(rlen)
            uword year = lkut.g16()
            ubyte month = lkut.g8()
            ubyte day = lkut.g8()
            ubyte hour = lkut.g8()
            ubyte minute = lkut.g8()
            ubyte sec = lkut.g8()
            ubyte wday = lkut.g8()
            if not lkut.perr and year >= 2000 and year < 2155
                cx16.clock_set_date_time(mkword(month, lsb(year - 1900)), mkword(hour, day), mkword(sec, minute), mkword(wday + 1, 0))
        }
        errmsg[0] = 0
        return S_OK
    }

    sub drop() {
        online = false
        lku.hangup_modem()
    }

    sub hangup() {
        if online {
            void request(0, OP_BYE, 0, 120)
            online = false
            if lku.expect(iso:"NO CARRIER", 0, 180)
                return
        }
        lku.hangup_modem()
    }
}
