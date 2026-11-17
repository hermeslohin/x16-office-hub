; hostiletest.p8 - X16Link client against a HOSTILE hub (lib/x16link/test/evil_hub.py) and bad key files.
; Regression test for the W7 security review: F6 (NO CARRIER inside a frame -> no retransmit into
; modem command mode), F7 (endless wrong-SEQ frames -> give up), F8 (READ data past the announced size,
; WRITE reply that doesn't advance `have`), F10 (over-long / unsafe key-file values are refused).
; Run by run_hostiletest.sh. Every line: "<CASE> st=<status>".
%import emudbg
%import conv
%import x16link_api
%zeropage basicsafe
main {
    sub log(str s) {
        emudbg.console_write(s)
    }
    sub status(str what, ubyte st) {
        log(what)
        log(iso:" st=")
        log(conv.str_ub(st))
        log(iso:"\n")
    }
    sub keycase(str what, str fname) {
        status(what, lkapi.load_key(fname))
    }
    sub noop(str what, str tag) {
        ubyte st = lkapi.load_key(iso:"X16LINK.KEY")
        if st == 0
            st = lkapi.connect()
        if st == 0 {
            @($0400) = tag[0]
            @($0401) = tag[1]
            st = lkapi.request(mkword($03, 0), 2, 300)
        }
        status(what, st)
        lkapi.hangup()
    }
    sub start() {
        diskio.drivenumber = 8
        if not lkload.load() {
            log(iso:"HOSTILE load libs FAILED\nHOSTILE done\n")
            repeat {}
        }
        keycase(iso:"KEY_LONGHOST", iso:"KEYBAD1.KEY")
        keycase(iso:"KEY_QUOTEHOST", iso:"KEYBAD2.KEY")
        keycase(iso:"KEY_LONGKEY", iso:"KEYBAD3.KEY")
        keycase(iso:"KEY_LONGDEV", iso:"KEYBAD4.KEY")
        keycase(iso:"KEY_GOOD", iso:"X16LINK.KEY")
        noop(iso:"NOCARRIER_MIDFRAME", iso:"S2")
        noop(iso:"JUNK_FRAMES", iso:"S3")
        ubyte st = lkapi.load_key(iso:"X16LINK.KEY")
        if st == 0
            st = lkapi.connect()
        if st == 0
            st = lkapi.download(iso:"x.rtf", iso:"DL-EVIL.TMP", 0)
        status(iso:"READ_PAST_SIZE", st)
        lkapi.hangup()
        st = lkapi.load_key(iso:"X16LINK.KEY")
        if st == 0
            st = lkapi.connect()
        if st == 0
            st = lkapi.upload(iso:"X16LINK.KEY", iso:"y.rtf", 0, 0)
        status(iso:"WRITE_HAVE_STUCK", st)
        lkapi.hangup()
        log(iso:"HOSTILE done\n")
        repeat {}
    }
}
