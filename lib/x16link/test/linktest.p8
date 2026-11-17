; linktest.p8 - headless end-to-end test of the X16Link libraries (X16LINK.BIN + X16DOCS.BIN)
; against a running hub. Run by lib/x16link/test/run_linktest.sh (x16emu -uart1 ximodem).
; Lists Documents/, downloads LKTEST.RTF + LKTEST.TXT (as RTF), uploads the RTF back as a new
; name, then retries the same create (must CONFLICT) and overwrites with the etag (must succeed).
%import emudbg
%import conv
%import x16link_api
%zeropage basicsafe

main {
    ubyte[72] row
    ubyte[64] msg
    ubyte[48] tag

    sub log(str s) {
        emudbg.console_write(s)
    }

    sub status(str what, ubyte st) {
        log(what)
        log(iso:" st=")
        log(conv.str_ub(st))
        if st != 0 {
            lkapi.get_error(&msg, 64)
            log(iso:" err=")
            log(&msg)
        }
        log(iso:"\n")
    }

    sub start() {
        diskio.drivenumber = 8
        if not lkload.load() {
            log(iso:"LINKTEST load libs FAILED\nLINKTEST done\n")
            repeat {}
        }
        ubyte st = lkapi.load_key(iso:"X16LINK.KEY")
        status(iso:"KEY", st)
        uword t0 = cbm.RDTIM16()
        st = lkapi.connect()
        status(iso:"CONNECT", st)
        log(iso:"CONNECT_JIFFIES ")
        log(conv.str_uw(cbm.RDTIM16() - t0))
        log(iso:"\n")
        if st == 0 {
            st = lkapi.list_begin()
            status(iso:"LIST", st)
            uword total = lkapi.list_total()
            log(iso:"LIST_TOTAL ")
            log(conv.str_uw(total))
            log(iso:"\n")
            uword i
            for i in 0 to total - 1 {
                if total == 0
                    break
                st = lkapi.list_row(i, &row)
                if st != 0 {
                    status(iso:"ROW", st)
                    break
                }
                log(iso:"ROW|")
                log(&row + 2)
                log(iso:"|\n")
            }
            st = lkapi.download(iso:"LKTEST.RTF", iso:"DL-RTF.TMP", 1)
            status(iso:"DOWNLOAD_RTF", st)
            st = lkapi.download(iso:"LKTEST.TXT", iso:"DL-TXT.TMP", 1)
            status(iso:"DOWNLOAD_TXT", st)
            st = lkapi.download(iso:"Example.odt", iso:"DL-ODT.TMP", 1)
            status(iso:"DOWNLOAD_ODT", st)
            st = lkapi.download(iso:"../secrets/x.rtf", iso:"DL-BAD.TMP", 1)
            status(iso:"DOWNLOAD_TRAVERSAL", st)
            lkapi.set_etag(iso:"")
            st = lkapi.upload(iso:"DL-RTF.TMP", iso:"LKTEST-UP.RTF", 0, 0)
            status(iso:"UPLOAD_NEW", st)
            lkapi.get_etag(&tag, 48)
            log(iso:"ETAG ")
            log(&tag)
            log(iso:"\n")
            st = lkapi.upload(iso:"DL-RTF.TMP", iso:"LKTEST-UP.RTF", 0, 0)
            status(iso:"UPLOAD_AGAIN", st)
            if st == lkapi.S_CONFLICT {
                ubyte kind = lkapi.conflict(&tag, 48)
                log(iso:"CONFLICT_KIND ")
                log(conv.str_ub(kind))
                log(iso:"\n")
                lkapi.set_etag(&tag)
                st = lkapi.upload(iso:"DL-RTF.TMP", iso:"LKTEST-UP.RTF", 1, 0)
                status(iso:"UPLOAD_OVERWRITE", st)
            }
            st = lkapi.upload(iso:"DL-RTF.TMP", iso:"LKTEST-UP.TXT", 0, 1)
            status(iso:"UPLOAD_TXT", st)
            lkapi.hangup()
            log(iso:"HANGUP\n")
        }
        log(iso:"LINKTEST done\n")
        repeat {}
    }
}
