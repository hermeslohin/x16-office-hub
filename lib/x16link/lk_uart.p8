; lk_uart.p8 - TexElec TL16C2550 port 1 ($9FE0) driver for X16Link + Zimodem helpers.
; 115200 8N1, FIFO on (RX trigger 8), DTR+RTS+AFE (automatic RTS/CTS flow control).
; Receive is polled into a 256-byte ring buffer: the ring is only filled while it has room,
; so when the X16 is busy the UART FIFO fills, AFE drops RTS and the ESP32 pauses (no loss).

lku {
    &ubyte RBR = $9fe0      ; THR/RBR, DLL when DLAB=1
    &ubyte IER = $9fe1      ; DLM when DLAB=1
    &ubyte FCR = $9fe2      ; IIR on read
    &ubyte LCR = $9fe3
    &ubyte MCR = $9fe4
    &ubyte LSR = $9fe5
    &ubyte SCR = $9fe7
    ; Read RBR only from code in main/golden RAM: on some real X16s an "lda $9fe0" executed from
    ; banked RAM ($A000-$BFFF) reads a received $FF as $00 (seen on hardware, not in x16emu). init()
    ; puts "lda $9fe0 / rts" here ($06FC-$06FF, golden RAM no app uses); poll and rx_direct jsr it.
    const uword RBRSTUB = $06fc

    const ubyte DIV_115200 = 8      ; 921600 / 115200 (14.7456 MHz crystal)

    const uword ring = $0700        ; 256-byte receive ring in golden RAM ($0700-$07FF)
    ubyte rhead
    ubyte rtail

    ; returns false if no UART answers at $9FE0 (scratch register test)
    sub init() -> bool {
        %asm {{
            ldx  #3
-           lda  _stub,x
            sta  p8c_RBRSTUB,x
            dex
            bpl  -
            bra  +
_stub       lda  $9fe0
            rts
+
        }}
        SCR = $a5
        if SCR != $a5
            return false
        SCR = $5a
        if SCR != $5a
            return false
        LCR = $80
        RBR = DIV_115200
        IER = 0
        LCR = $03
        IER = 0
        FCR = $87
        MCR = $23
        rhead = 0
        rtail = 0
        return true
    }

    ; move bytes from the UART FIFO into the ring while there is room
    asmsub poll() clobbers(A, X) {
        %asm {{
_more       lda  $9fe5
            lsr  a
            bcc  _done
            ldx  p8v_rhead
            inx
            cpx  p8v_rtail
            beq  _done              ; ring full: leave it in the FIFO (RTS/CTS will throttle)
            dex
            jsr  p8c_RBRSTUB            ; lda $9fe0 from main RAM, see init()
            sta  $0700,x
            inc  p8v_rhead
            bra  _more
_done       rts
        }}
    }

    sub avail() -> bool {
        poll()
        return rhead != rtail
    }

    ; next byte; caller checks avail() first
    sub get() -> ubyte {
        ubyte b = @(ring + rtail)
        rtail++
        return b
    }

    ; Receive cnt (>= 1) bytes into dst: first whatever poll() already put in the ring, then straight
    ; from the UART with IRQs off, ~40 cycles a byte (a byte takes 87 us at 115200), so the 16-byte
    ; FIFO can't overrun wherever this code runs (the old Prog8 getb loop could not keep up on real
    ; X16s when running from banked RAM). Gives up after ~0.8 s without a byte; returns how many
    ; bytes are still missing.
    asmsub rx_direct(uword dst @R0, uword cnt @R1) clobbers(X) -> uword @AY {
        %asm {{
            php
            sei
_byte       ldx  p8v_rtail
            cpx  p8v_rhead
            beq  _uart
            lda  $0700,x
            inc  p8v_rtail
            bra  _got
_uart       ldx  #0
            stz  cx16.r2L
            lda  #8
            sta  cx16.r2H
_poll       lda  $9fe5
            lsr  a
            bcs  _rbr
            dex
            bne  _poll
            dec  cx16.r2L
            bne  _poll
            dec  cx16.r2H
            bne  _poll
            bra  _done
_rbr        jsr  p8c_RBRSTUB
_got        sta  (cx16.r0)
            inc  cx16.r0
            bne  +
            inc  cx16.r0+1
+           lda  cx16.r1
            bne  +
            dec  cx16.r1+1
+           dec  cx16.r1
            lda  cx16.r1
            ora  cx16.r1+1
            bne  _byte
_done       plp
            lda  cx16.r1
            ldy  cx16.r1+1
            rts
        }}
    }

    ; wait up to 'jiffies' for a byte. returns true + byte in getb_val
    ubyte getb_val
    sub getb(uword jiffies) -> bool {
        if avail() {
            getb_val = get()
            return true
        }
        uword t0 = cbm.RDTIM16()
        repeat {
            if avail() {
                getb_val = get()
                return true
            }
            if cbm.RDTIM16() - t0 >= jiffies
                return false
        }
    }

    sub put(ubyte b) {
        while LSR & $20 == 0 {
            poll()
        }
        RBR = b
    }

    ; send n bytes, refilling the 16-byte TX FIFO whenever it is empty
    sub send(uword p, uword n) {
        ubyte burst
        while n != 0 {
            if LSR & $20 != 0 {
                burst = 16
                while burst != 0 and n != 0 {
                    RBR = @(p)
                    p++
                    n--
                    burst--
                }
            }
            poll()
        }
    }

    sub puts(str s) {
        ubyte i = 0
        while s[i] != 0 {
            put(s[i])
            i++
        }
    }

    ; discard input until the line has been quiet for 'quiet' jiffies (max ~3 s)
    sub drain(ubyte quiet) {
        uword start = cbm.RDTIM16()
        uword t0 = start
        repeat {
            if avail() {
                rtail = rhead
                t0 = cbm.RDTIM16()
            }
            if cbm.RDTIM16() - t0 >= quiet
                return
            if cbm.RDTIM16() - start >= 180
                return
        }
    }

    ; wait for the ISO/ASCII text 'what' in the modem output; false on timeout or if 'bad' shows up first
    sub expect(str what, str bad, uword jiffies) -> bool {
        ubyte m = 0
        ubyte mb = 0
        ubyte c
        uword t0 = cbm.RDTIM16()
        while cbm.RDTIM16() - t0 < jiffies {
            if avail() {
                c = get()
                if c == what[m] {
                    m++
                    if what[m] == 0
                        return true
                } else {
                    m = 0
                    if c == what[0]
                        m = 1
                }
                if bad != 0 {
                    if c == bad[mb] {
                        mb++
                        if bad[mb] == 0
                            return false
                    } else {
                        mb = 0
                        if c == bad[0]
                            mb = 1
                    }
                }
            }
        }
        return false
    }

    ; Zimodem guarded escape + hang up
    sub hangup_modem() {
        sys.wait(66)
        puts(iso:"+++")
        sys.wait(66)
        puts(iso:"ATH\r")
        void expect(iso:"OK", 0, 120)
    }
}
