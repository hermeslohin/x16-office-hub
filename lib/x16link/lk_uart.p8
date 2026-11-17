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

    const ubyte DIV_115200 = 8      ; 921600 / 115200 (14.7456 MHz crystal)

    const uword ring = $0700        ; 256-byte receive ring in golden RAM ($0700-$07FF)
    ubyte rhead
    ubyte rtail

    ; returns false if no UART answers at $9FE0 (scratch register test)
    sub init() -> bool {
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
            lda  $9fe0
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
