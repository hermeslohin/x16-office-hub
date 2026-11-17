; Crypto self-test for lk_crypto (runs as a normal PRG; output to the emulator console).
%import emudbg
%import conv
%import lk_crypto
%zeropage basicsafe

main {
    ubyte[32] out
    ubyte[200] msg

    sub start() {
        ubyte i
        str s9 = iso:"123456789"
        lkc.crc16_init()
        lkc.crc16_buf(s9, 9)
        log(iso:"CRC16 ")
        hex8(lkc.crchi)
        hex8(lkc.crclo)
        nl()
        lkc.crc32_init()
        lkc.crc32_buf(s9, 9)
        lkc.crc32_get(&out)
        log(iso:"CRC32 ")
        hexn(&out, 4)
        nl()
        ; sha256("abc")
        lkc.sha_init()
        lkc.sha_update(iso:"abc", 3)
        lkc.sha_final(&out)
        log(iso:"SHA_ABC ")
        hexn(&out, 32)
        nl()
        ; sha256 of 200 bytes 0..199 (multi block, padding crosses)
        for i in 0 to 199
            msg[i] = i
        lkc.sha_init()
        lkc.sha_update(&msg, 200)
        lkc.sha_final(&out)
        log(iso:"SHA_200 ")
        hexn(&out, 32)
        nl()
        ; sha256 of 55 and 56 bytes (padding edge cases)
        lkc.sha_init()
        lkc.sha_update(&msg, 55)
        lkc.sha_final(&out)
        log(iso:"SHA_55 ")
        hexn(&out, 32)
        nl()
        lkc.sha_init()
        lkc.sha_update(&msg, 56)
        lkc.sha_final(&out)
        log(iso:"SHA_56 ")
        hexn(&out, 32)
        nl()
        ; HMAC(key = 0..31, "X16L1C" + "x16-test" + 0..15 + 16..31 + 01)
        ubyte[32] key
        for i in 0 to 31
            key[i] = i
        uword t0 = cbm.RDTIM16()
        lkc.hmac_begin(&key)
        lkc.sha_update(iso:"X16L1Cx16-test", 14)
        lkc.sha_update(&msg, 32)
        lkc.sha_byte(1)
        lkc.hmac_end(&out)
        uword t1 = cbm.RDTIM16()
        log(iso:"HMAC ")
        hexn(&out, 32)
        nl()
        log(iso:"HMAC_JIFFIES ")
        log(conv.str_uw(t1 - t0))
        nl()
        log(iso:"CRYPTOTEST done\n")
        repeat {}
    }

    sub log(str s) {
        emudbg.console_write(s)
    }
    sub nl() {
        emudbg.console_write(iso:"\n")
    }
    sub hex8(ubyte b) {
        str hexd = iso:"0123456789abcdef"
        emudbg.console_chrout(hexd[b >> 4])
        emudbg.console_chrout(hexd[b & 15])
    }
    sub hexn(uword p, ubyte n) {
        while n != 0 {
            hex8(@(p))
            p++
            n--
        }
    }
}
