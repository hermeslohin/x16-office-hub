; UART loopback / 8-bit transparency test for the TexElec TL16C2550 card (port 1 @ $9FE0)
; talking to Zimodem (real ESP32 or the emulator's libximodem).
;  1. program UART: 115200 8N1 (divisor 8 @ 14.7456MHz), FIFO on, DTR+RTS+AFE (auto RTS/CTS)
;  2. ATZ, ATI, AT&V-free: dial ATD"127.0.0.1:6502" -> wait CONNECT
;  3. send PAYLOAD (531 bytes: 0..255 x2, "+++", $FF $FF, CR LF CR NUL, ESC [ A, XON XOFF, IAC sequence)
;     while reading back what the echo server returns, then compare.
; Results go to the host console via the emulator debug port (emudbg) -> visible on x16emu stdout.
%import textio
%import emudbg
%zeropage basicsafe

uart {
    &ubyte RBR = $9fe0      ; THR/RBR, DLL when DLAB=1
    &ubyte IER = $9fe1      ; DLM when DLAB=1
    &ubyte FCR = $9fe2      ; IIR on read
    &ubyte LCR = $9fe3
    &ubyte MCR = $9fe4
    &ubyte LSR = $9fe5
    &ubyte MSR = $9fe6

    sub init(ubyte divisor) {
        LCR = $80           ; DLAB
        RBR = divisor
        IER = 0
        LCR = $03           ; 8N1, DLAB off
        IER = 0             ; polled, no IRQs
        FCR = $87           ; FIFO enable + reset RX/TX, RX trigger 8
        MCR = $23           ; DTR | RTS | AFE (hardware RTS/CTS)
    }

    sub put(ubyte b) {
        while LSR & $20 == 0 { }    ; wait THR empty (AFE holds it while CTS low)
        RBR = b
    }

    sub ready() -> bool {
        return LSR & 1 != 0
    }

    sub drain() {
        while ready() {
            cx16.r0L = RBR
        }
    }

    sub puts(str s) {
        ubyte i = 0
        while s[i] != 0 {
            put(s[i])
            i++
        }
    }
}

main {
    const uword PAYLOAD_LEN = 531
    uword payload = memory("payload", 600, 0)
    uword rx = memory("rx", 2048, 0)
    uword rxcount

    sub start() {
        txt.print("uart loopback test\n")
        log(iso:"UARTLOOP start\n")
        build_payload()
        uart.init(8)
        sys.wait(30)
        uart.drain()

        uart.puts(iso:"ATZ\r")
        if not expect(iso:"OK", 180) { fail(iso:"no OK after ATZ") return }
        uart.puts(iso:"ATI\r")
        if not expect(iso:"OK", 180) { fail(iso:"no OK after ATI") return }
        uart.puts(iso:"ATE0\r")
        if not expect(iso:"OK", 180) { fail(iso:"no OK after ATE0") return }
        uart.puts(iso:"ATD\"127.0.0.1:6502\"\r")
        if not expect(iso:"CONNECT", 600) { fail(iso:"no CONNECT") return }
        sys.wait(30)
        uart.drain()             ; rest of the CONNECT line
        log(iso:"\nUARTLOOP connected, sending payload\n")

        ; send payload while collecting the echo
        rxcount = 0
        uword i
        ; 16C550 driver style: when THR/FIFO is empty (THRE), refill up to 16 bytes at once
        uword t_start = cbm.RDTIM16()
        i = 0
        while i < PAYLOAD_LEN {
            if uart.LSR & $20 != 0 {
                ubyte burst = 16
                while burst != 0 and i < PAYLOAD_LEN {
                    uart.RBR = @(payload+i)
                    i++
                    burst--
                }
            }
            collect()
        }
        ; let the echo finish (3s idle-ish timeout)
        uword t0 = cbm.RDTIM16()
        while rxcount < PAYLOAD_LEN and cbm.RDTIM16()-t0 < 300 {
            collect()
        }
        sys.wait(30)
        collect()
        uword t_used = cbm.RDTIM16()-t_start

        ; compare
        uword bad = 0
        uword n = rxcount
        if n > PAYLOAD_LEN
            n = PAYLOAD_LEN
        for i in 0 to n-1 {
            if @(rx+i) != @(payload+i) {
                if bad < 16 {
                    log(iso:"MISMATCH at ")
                    loghex16(i)
                    log(iso:" sent ")
                    loghex8(@(payload+i))
                    log(iso:" got ")
                    loghex8(@(rx+i))
                    log(iso:"\n")
                }
                bad++
            }
        }
        log(iso:"UARTLOOP sent=")
        loghex16(PAYLOAD_LEN)
        log(iso:" received=")
        loghex16(rxcount)
        log(iso:" mismatches=")
        loghex16(bad)
        log(iso:" jiffies=")
        loghex16(t_used)
        log(iso:"\nRX DUMP:\n")
        for i in 0 to rxcount-1 {
            loghex8(@(rx+i))
            if (i & 31) == 31
                log(iso:"\n")
            else
                emudbg.console_chrout(' ')
        }
        log(iso:"\n")
        if rxcount == PAYLOAD_LEN and bad == 0 {
            log(iso:"RESULT PASS 8-bit clean\n")
            txt.print("pass\n")
        } else {
            log(iso:"RESULT FAIL\n")
            txt.print("fail\n")
        }
        ; hang up: guard time, +++, guard, ATH
        sys.wait(70)
        uart.puts(iso:"+++")
        sys.wait(70)
        uart.puts(iso:"ATH\r")
        void expect(iso:"OK", 120)
        log(iso:"\nUARTLOOP done\n")
    }

    sub collect() {
        while uart.ready() {
            ubyte b = uart.RBR
            if rxcount < 2048 {
                @(rx+rxcount) = b
                rxcount++
            }
        }
    }

    sub build_payload() {
        uword p = payload
        ubyte rep
        for rep in 0 to 1 {
            ubyte v = 0
            repeat 256 {
                @(p) = v
                p++
                v++
            }
        }
        ; raw hex on purpose: Prog8 char literals like 'A' are PETSCII ($C1), not ASCII
        ubyte[] extra = [ $2b, $2b, $2b,  $ff, $ff,  $0d, $0a, $0d, $00,  $1b, $5b, $41,  $11, $13,  $ff, $ff, $ff, $fb, $01 ]
        ubyte e
        for e in extra {
            @(p) = e
            p++
        }
    }

    ; wait up to 'jiffies' for the iso string 'what' to appear in the modem output; mirror to host log.
    sub expect(str what, uword jiffies) -> bool {
        ubyte m = 0
        uword t0 = cbm.RDTIM16()
        while cbm.RDTIM16()-t0 < jiffies {
            if uart.ready() {
                ubyte c = uart.RBR
                if c == 13
                    emudbg.console_nl()
                else if c >= 32 and c < 127
                    emudbg.console_chrout(c)
                if c == what[m] {
                    m++
                    if what[m] == 0
                        return true
                } else {
                    m = 0
                    if c == what[0]
                        m = 1
                }
            }
        }
        return false
    }

    sub fail(str msg) {
        log(iso:"\nRESULT FAIL ")
        log(msg)
        log(iso:"\n")
        txt.print("fail\n")
    }

    sub log(str s) {
        emudbg.console_write(s)
    }

    sub loghex8(ubyte b) {
        str hexd = iso:"0123456789abcdef"
        emudbg.console_chrout(hexd[b >> 4])
        emudbg.console_chrout(hexd[b & 15])
    }

    sub loghex16(uword w) {
        loghex8(msb(w))
        loghex8(lsb(w))
    }
}
