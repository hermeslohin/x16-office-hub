; lk_crypto.p8 - X16Link crypto/checksums for the 65C02:
;   CRC-16/CCITT-FALSE (frames), CRC-32/IEEE (whole files), SHA-256 + HMAC-SHA256 (auth).
; Hot loops are hand-written 6502; SHA-256 round logic is Prog8 calling 32-bit asm primitives
; that work on a 160-byte workspace (big-endian words, as SHA-256 defines them).
; Verified against Python (hashlib/hmac/zlib) by lib/x16link/test/run_cryptotest.sh.

lkc {
    ; ---------------- CRC-16/CCITT-FALSE (poly $1021, init $FFFF) ----------------
    ubyte crclo
    ubyte crchi

    sub crc16_init() {
        crclo = $ff
        crchi = $ff
    }

    ; Greg Cook's table-less byte-wise CRC-16/CCITT
    asmsub crc16_byte(ubyte b @A) clobbers(A, X, Y) {
        %asm {{
            eor  p8v_crchi
            sta  p8v_crchi
            lsr  a
            lsr  a
            lsr  a
            lsr  a
            tax
            asl  a
            eor  p8v_crclo
            sta  p8v_crclo
            txa
            eor  p8v_crchi
            sta  p8v_crchi
            asl  a
            asl  a
            asl  a
            tax
            asl  a
            asl  a
            eor  p8v_crchi
            tay
            txa
            rol  a
            eor  p8v_crclo
            sta  p8v_crchi
            sty  p8v_crclo
            rts
        }}
    }

    sub crc16_buf(uword p, uword n) {
        while n != 0 {
            crc16_byte(@(p))
            p++
            n--
        }
    }

    ; ---------------- CRC-32 (reflected, poly $EDB88320) ----------------
    ubyte[4] c32            ; little-endian running value

    sub crc32_init() {
        c32[0] = $ff
        c32[1] = $ff
        c32[2] = $ff
        c32[3] = $ff
    }

    asmsub crc32_byte(ubyte b @A) clobbers(A, X) {
        %asm {{
            eor  p8v_c32
            sta  p8v_c32
            ldx  #8
_bit        lsr  p8v_c32+3
            ror  p8v_c32+2
            ror  p8v_c32+1
            ror  p8v_c32
            bcc  _next
            lda  p8v_c32+3
            eor  #$ed
            sta  p8v_c32+3
            lda  p8v_c32+2
            eor  #$b8
            sta  p8v_c32+2
            lda  p8v_c32+1
            eor  #$83
            sta  p8v_c32+1
            lda  p8v_c32
            eor  #$20
            sta  p8v_c32
_next       dex
            bne  _bit
            rts
        }}
    }

    sub crc32_buf(uword p, uword n) {
        while n != 0 {
            crc32_byte(@(p))
            p++
            n--
        }
    }

    ; final CRC-32 (xor $FFFFFFFF) into 4 bytes little-endian at dst
    sub crc32_get(uword dst) {
        @(dst) = c32[0] ^ $ff
        @(dst + 1) = c32[1] ^ $ff
        @(dst + 2) = c32[2] ^ $ff
        @(dst + 3) = c32[3] ^ $ff
    }

    ; ---------------- SHA-256 ----------------
    ; workspace layout (byte offsets in ws)
    const ubyte V = 0       ; a b c d e f g h  (8 x 4)
    const ubyte T1 = 32
    const ubyte T2 = 36
    const ubyte X1 = 40
    const ubyte X2 = 44
    const ubyte X3 = 48
    const ubyte KT = 52
    const ubyte W = 64      ; 16-word rolling message schedule / input block
    const ubyte H = 128     ; hash state
    ; SHA/HMAC scratch lives in golden RAM inside the X16Link payload buffer XB ($0400-$0607), above
    ; the 32-byte AUTH reply: it is only used inside lk.connect(), between requests (saves 256 bytes
    ; of the 8 KB bank). Never run SHA/HMAC while XB[256..] holds something you need.
    &ubyte[160] ws = $0500

    ubyte[] ktab = [
        $42,$8a,$2f,$98, $71,$37,$44,$91, $b5,$c0,$fb,$cf, $e9,$b5,$db,$a5,
        $39,$56,$c2,$5b, $59,$f1,$11,$f1, $92,$3f,$82,$a4, $ab,$1c,$5e,$d5,
        $d8,$07,$aa,$98, $12,$83,$5b,$01, $24,$31,$85,$be, $55,$0c,$7d,$c3,
        $72,$be,$5d,$74, $80,$de,$b1,$fe, $9b,$dc,$06,$a7, $c1,$9b,$f1,$74,
        $e4,$9b,$69,$c1, $ef,$be,$47,$86, $0f,$c1,$9d,$c6, $24,$0c,$a1,$cc,
        $2d,$e9,$2c,$6f, $4a,$74,$84,$aa, $5c,$b0,$a9,$dc, $76,$f9,$88,$da,
        $98,$3e,$51,$52, $a8,$31,$c6,$6d, $b0,$03,$27,$c8, $bf,$59,$7f,$c7,
        $c6,$e0,$0b,$f3, $d5,$a7,$91,$47, $06,$ca,$63,$51, $14,$29,$29,$67,
        $27,$b7,$0a,$85, $2e,$1b,$21,$38, $4d,$2c,$6d,$fc, $53,$38,$0d,$13,
        $65,$0a,$73,$54, $76,$6a,$0a,$bb, $81,$c2,$c9,$2e, $92,$72,$2c,$85,
        $a2,$bf,$e8,$a1, $a8,$1a,$66,$4b, $c2,$4b,$8b,$70, $c7,$6c,$51,$a3,
        $d1,$92,$e8,$19, $d6,$99,$06,$24, $f4,$0e,$35,$85, $10,$6a,$a0,$70,
        $19,$a4,$c1,$16, $1e,$37,$6c,$08, $27,$48,$77,$4c, $34,$b0,$bc,$b5,
        $39,$1c,$0c,$b3, $4e,$d8,$aa,$4a, $5b,$9c,$ca,$4f, $68,$2e,$6f,$f3,
        $74,$8f,$82,$ee, $78,$a5,$63,$6f, $84,$c8,$78,$14, $8c,$c7,$02,$08,
        $90,$be,$ff,$fa, $a4,$50,$6c,$eb, $be,$f9,$a3,$f7, $c6,$71,$78,$f2]
    ubyte[] h0 = [
        $6a,$09,$e6,$67, $bb,$67,$ae,$85, $3c,$6e,$f3,$72, $a5,$4f,$f5,$3a,
        $51,$0e,$52,$7f, $9b,$05,$68,$8c, $1f,$83,$d9,$ab, $5b,$e0,$cd,$19]

    asmsub cp4(ubyte dst @X, ubyte src @Y) clobbers(A) {
        %asm {{
            lda  p8v_ws,y
            sta  p8v_ws,x
            lda  p8v_ws+1,y
            sta  p8v_ws+1,x
            lda  p8v_ws+2,y
            sta  p8v_ws+2,x
            lda  p8v_ws+3,y
            sta  p8v_ws+3,x
            rts
        }}
    }

    asmsub xor4(ubyte dst @X, ubyte src @Y) clobbers(A) {
        %asm {{
            lda  p8v_ws,x
            eor  p8v_ws,y
            sta  p8v_ws,x
            lda  p8v_ws+1,x
            eor  p8v_ws+1,y
            sta  p8v_ws+1,x
            lda  p8v_ws+2,x
            eor  p8v_ws+2,y
            sta  p8v_ws+2,x
            lda  p8v_ws+3,x
            eor  p8v_ws+3,y
            sta  p8v_ws+3,x
            rts
        }}
    }

    asmsub and4(ubyte dst @X, ubyte src @Y) clobbers(A) {
        %asm {{
            lda  p8v_ws,x
            and  p8v_ws,y
            sta  p8v_ws,x
            lda  p8v_ws+1,x
            and  p8v_ws+1,y
            sta  p8v_ws+1,x
            lda  p8v_ws+2,x
            and  p8v_ws+2,y
            sta  p8v_ws+2,x
            lda  p8v_ws+3,x
            and  p8v_ws+3,y
            sta  p8v_ws+3,x
            rts
        }}
    }

    ; big-endian 32-bit add: dst += src
    asmsub add4(ubyte dst @X, ubyte src @Y) clobbers(A) {
        %asm {{
            clc
            lda  p8v_ws+3,x
            adc  p8v_ws+3,y
            sta  p8v_ws+3,x
            lda  p8v_ws+2,x
            adc  p8v_ws+2,y
            sta  p8v_ws+2,x
            lda  p8v_ws+1,x
            adc  p8v_ws+1,y
            sta  p8v_ws+1,x
            lda  p8v_ws,x
            adc  p8v_ws,y
            sta  p8v_ws,x
            rts
        }}
    }

    asmsub not4(ubyte dst @X) clobbers(A) {
        %asm {{
            lda  p8v_ws,x
            eor  #$ff
            sta  p8v_ws,x
            lda  p8v_ws+1,x
            eor  #$ff
            sta  p8v_ws+1,x
            lda  p8v_ws+2,x
            eor  #$ff
            sta  p8v_ws+2,x
            lda  p8v_ws+3,x
            eor  #$ff
            sta  p8v_ws+3,x
            rts
        }}
    }

    ; rotate right by n (0..31)
    asmsub rotr4(ubyte dst @X, ubyte n @A) clobbers(A, Y) {
        %asm {{
_bytes      cmp  #8
            bcc  _bits
            sbc  #8
            pha
            ldy  p8v_ws+3,x
            lda  p8v_ws+2,x
            sta  p8v_ws+3,x
            lda  p8v_ws+1,x
            sta  p8v_ws+2,x
            lda  p8v_ws,x
            sta  p8v_ws+1,x
            tya
            sta  p8v_ws,x
            pla
            jmp  _bytes
_bits       tay
            beq  _done
_loop       lda  p8v_ws+3,x
            lsr  a
            ror  p8v_ws,x
            ror  p8v_ws+1,x
            ror  p8v_ws+2,x
            ror  p8v_ws+3,x
            dey
            bne  _loop
_done       rts
        }}
    }

    ; logical shift right by n (1..31)
    asmsub shr4(ubyte dst @X, ubyte n @A) clobbers(A, Y) {
        %asm {{
            tay
_loop       lsr  p8v_ws,x
            ror  p8v_ws+1,x
            ror  p8v_ws+2,x
            ror  p8v_ws+3,x
            dey
            bne  _loop
            rts
        }}
    }

    ; ws[4..31] = ws[0..27]  (h=g, g=f, ... b=a)
    asmsub shiftv() clobbers(A, X) {
        %asm {{
            ldx  #27
-           lda  p8v_ws,x
            sta  p8v_ws+4,x
            dex
            bpl  -
            rts
        }}
    }

    asmsub ldk(ubyte t4 @Y) clobbers(A) {
        %asm {{
            lda  p8v_ktab,y
            sta  p8v_ws+52
            lda  p8v_ktab+1,y
            sta  p8v_ws+53
            lda  p8v_ktab+2,y
            sta  p8v_ws+54
            lda  p8v_ktab+3,y
            sta  p8v_ws+55
            rts
        }}
    }

    ; sigma helpers: X1 = f(src) using X2/X3 as scratch
    sub sig(ubyte src, ubyte r1, ubyte r2, ubyte r3, bool shift3) {
        cp4(X1, src)
        rotr4(X1, r1)
        cp4(X2, src)
        rotr4(X2, r2)
        xor4(X1, X2)
        cp4(X2, src)
        if shift3
            shr4(X2, r3)
        else
            rotr4(X2, r3)
        xor4(X1, X2)
    }

    sub compress() {
        ubyte i
        for i in 0 to 31
            ws[V + i] = ws[H + i]
        ubyte t
        ubyte wi
        for t in 0 to 63 {
            wi = W + ((t & 15) << 2)
            if t >= 16 {
                sig(W + (((t + 1) & 15) << 2), 7, 18, 3, true)          ; s0(W[t-15])
                add4(wi, X1)
                sig(W + (((t + 14) & 15) << 2), 17, 19, 10, true)       ; s1(W[t-2])
                add4(wi, X1)
                add4(wi, W + (((t + 9) & 15) << 2))                      ; W[t-7]
            }
            ; T1 = h + S1(e) + ch(e,f,g) + K[t] + W[t]
            cp4(T1, 28)
            sig(16, 6, 11, 25, false)
            add4(T1, X1)
            cp4(X1, 16)
            and4(X1, 20)
            cp4(X2, 16)
            not4(X2)
            and4(X2, 24)
            xor4(X1, X2)
            add4(T1, X1)
            ldk(t << 2)
            add4(T1, KT)
            add4(T1, wi)
            ; T2 = S0(a) + maj(a,b,c)
            sig(0, 2, 13, 22, false)
            cp4(T2, X1)
            cp4(X1, 0)
            and4(X1, 4)
            cp4(X2, 0)
            and4(X2, 8)
            xor4(X1, X2)
            cp4(X2, 4)
            and4(X2, 8)
            xor4(X1, X2)
            add4(T2, X1)
            shiftv()
            add4(16, T1)            ; e = d + T1
            cp4(0, T1)
            add4(0, T2)             ; a = T1 + T2
        }
        for i in 0 to 7
            add4(H + (i << 2), i << 2)
    }

    uword sha_len
    ubyte sha_n

    sub sha_init() {
        ubyte i
        for i in 0 to 31
            ws[H + i] = h0[i]
        sha_len = 0
        sha_n = 0
    }

    sub sha_raw(ubyte b) {
        ws[W + sha_n] = b
        sha_n++
        if sha_n == 64 {
            compress()
            sha_n = 0
        }
    }

    sub sha_byte(ubyte b) {
        sha_raw(b)
        sha_len++
    }

    sub sha_update(uword p, uword n) {
        while n != 0 {
            sha_byte(@(p))
            p++
            n--
        }
    }

    sub sha_final(uword out) {
        uword bits = sha_len << 3
        ubyte top = msb(sha_len) >> 5
        sha_raw($80)
        while sha_n != 56
            sha_raw(0)
        repeat 5
            sha_raw(0)
        sha_raw(top)
        sha_raw(msb(bits))
        sha_raw(lsb(bits))
        ubyte i
        for i in 0 to 31
            @(out + i) = ws[H + i]
    }

    ; ---------------- HMAC-SHA256 with a 32-byte key ----------------
    &ubyte[32] hkey = $05a0
    &ubyte[32] inner = $05c0

    sub hmac_pad(ubyte pad) {
        ubyte i
        sha_init()
        for i in 0 to 31
            sha_byte(hkey[i] ^ pad)
        repeat 32
            sha_byte(pad)
    }

    sub hmac_begin(uword key) {
        ubyte i
        for i in 0 to 31
            hkey[i] = @(key + i)
        hmac_pad($36)
    }

    ; message bytes are fed with sha_update()/sha_byte() between begin and end
    sub hmac_end(uword out) {
        ubyte i
        sha_final(&inner)
        hmac_pad($5c)
        sha_update(&inner, 32)
        sha_final(out)
        for i in 0 to 31
            hkey[i] = 0
    }
}
