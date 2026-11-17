; x16cal.p8 - X16Link calendar channel client (PROTOCOL.md 3.5), compiled into the app (not a bank).
; Needs only the transport library X16LINK.BIN (RAM bank 60); load it with lkload.load_link().
;
;   lkcal.sync(events, counter) -> status   push all 128 slots, pull the changed ones into `events`
;   lkcal.new_counter[4]                    counter to store in CALSYNC.DAT after CALENDAR.DAT is saved
;   lkcal.created/updated/deleted/received/nconf/unplaced/full    result of the last sync
;   lkcal.conf_*[i]                         the open conflicts (max MAXCONF) after a sync
;   lkcal.queue_ack(slot) / ack_all()       acknowledged on the next sync (sent before CAL_BEGIN)
;
; `events` = 4096-byte slab of 128 x 32-byte records in main RAM. On failure the slab may be
; partly updated: the caller must reload CALENDAR.DAT (it saved before syncing) and must NOT
; store the counter. Status 0 ok, else the X16Link status code (lkapi / lk_link.p8).

%import x16link_api

lkcal {
    const ubyte CH_CAL = 2
    const ubyte OP_BEGIN = $30
    const ubyte OP_PUT = $31
    const ubyte OP_SYNC = $32
    const ubyte OP_GET = $33
    const ubyte OP_CONFLICTS = $34
    const ubyte OP_ACK = $35
    const ubyte S_PROTO = 38
    const ubyte PER_PUT = 15            ; 15 x 33 = 495 bytes per CAL_PUT frame
    const uword T_REPLY = 600
    const uword T_SYNC = 3600           ; the hub talks to ownCloud during CAL_SYNC: allow 60 s
    const ubyte MAXCONF = 12
    const ubyte CTXT = 28               ; 27 chars + 0
    const uword XB = $0400

    ubyte[4] new_counter
    ubyte created
    ubyte updated
    ubyte deleted
    ubyte received
    ubyte nconf                         ; open conflicts reported by the hub
    ubyte unplaced
    bool full
    ubyte nconf_got                     ; how many are in the conf_* tables
    ubyte[MAXCONF] conf_slot            ; 255 = event not on the X16
    ubyte[MAXCONF] conf_kind            ; 0 both edited, 1 X16 deleted/ownCloud edited, 2 X16 edited/ownCloud deleted
    ubyte[MAXCONF] conf_field           ; 0 event, 1 date, 2 time, 3 text
    ubyte[MAXCONF] conf_kept            ; 0 X16 version kept, 1 ownCloud version kept
    uword conf_text = memory("lkcal_ctext", MAXCONF * CTXT, 0)   ; ownCloud value, PETSCII
    ubyte[16] ackq
    ubyte nack
    bool ack_everything
    ubyte[64] errmsg

    uword rlen
    uword rpos

    sub key_file() -> uword {
        return iso:"X16LINK.KEY"
    }

    sub queue_ack(ubyte slot) {
        ubyte i
        if nack != 0 {
            for i in 0 to nack - 1 {
                if ackq[i] == slot
                    return
            }
        }
        if nack < len(ackq) {
            ackq[nack] = slot
            nack++
        } else
            ack_everything = true
    }

    sub ack_all() {
        ack_everything = true
    }

    sub req(ubyte op, uword plen, uword timeout) -> ubyte {
        ubyte st = lkapi.request(mkword(op, CH_CAL), plen, timeout)
        rlen = lkapi.reply_len()
        rpos = 0
        return st
    }

    sub g8() -> ubyte {
        if rpos >= rlen
            return 0
        ubyte b = @(XB + rpos)
        rpos++
        return b
    }

    sub fail(ubyte st) -> ubyte {
        lkapi.link_error(&errmsg, 64)
        lkapi.hangup()
        lkapi.forget()
        return st
    }

    sub proto() -> ubyte {
        lkapi.hangup()
        lkapi.forget()
        void strings_copy(iso:"bad reply from hub", &errmsg)
        return S_PROTO
    }

    sub strings_copy(uword src, uword dst) -> ubyte {
        ubyte i = 0
        repeat {
            @(dst + i) = @(src + i)
            if @(src + i) == 0
                return i
            i++
        }
    }

    sub sync(uword events, uword counter) -> ubyte {
        ubyte st
        ubyte i
        ubyte k
        ubyte slot
        uword p
        errmsg[0] = 0
        created = 0
        updated = 0
        deleted = 0
        received = 0
        nconf = 0
        nconf_got = 0
        unplaced = 0
        full = false

        st = lkapi.load_key(key_file())
        if st != 0 {
            lkapi.link_error(&errmsg, 64)
            return st
        }
        st = lkapi.connect()
        if st != 0
            return fail(st)

        ; acknowledgements the user gave since the last sync
        if ack_everything or nack != 0 {
            if ack_everything {
                @(XB) = 0
                st = req(OP_ACK, 1, T_SYNC)
            } else {
                @(XB) = nack
                for i in 0 to nack - 1 {
                    @(XB + 1 + i) = ackq[i]
                }
                st = req(OP_ACK, 1 + nack, T_SYNC)
            }
            if st != 0
                return fail(st)
            nack = 0
            ack_everything = false
        }

        ; CAL_BEGIN counter, 128
        for i in 0 to 3 {
            @(XB + i) = @(counter + i)
        }
        @(XB + 4) = 128
        st = req(OP_BEGIN, 5, T_REPLY)
        if st != 0
            return fail(st)

        ; CAL_PUT 15 slots per frame
        slot = 0
        while slot < 128 {
            p = XB
            k = 0
            while k < PER_PUT and slot < 128 {
                @(p) = slot
                sys.memcopy(events + ((slot as uword) << 5), p + 1, 32)
                p += 33
                k++
                slot++
            }
            st = req(OP_PUT, p - XB, T_REPLY)
            if st != 0
                return fail(st)
        }

        ; CAL_SYNC
        st = req(OP_SYNC, 0, T_SYNC)
        if st != 0
            return fail(st)
        if rlen != 12
            return proto()
        for i in 0 to 3 {
            new_counter[i] = g8()
        }
        full = (g8() & 1) != 0
        ubyte m = g8()
        created = g8()
        updated = g8()
        deleted = g8()
        nconf = g8()
        unplaced = g8()
        if m > 128 or (full and m != 128)
            return proto()

        ; CAL_GET until all m records are in
        ubyte idx = 0
        while idx < m {
            @(XB) = idx
            @(XB + 1) = 255
            st = req(OP_GET, 2, T_REPLY)
            if st != 0
                return fail(st)
            if rlen < 2 or g8() != idx
                return proto()
            k = g8()
            if k == 0 or k > m - idx or rlen != 2 + (k as uword) * 33
                return proto()
            while k != 0 {
                slot = g8()
                if slot >= 128
                    return proto()
                sys.memcopy(XB + rpos, events + ((slot as uword) << 5), 32)
                rpos += 32
                k--
                idx++
            }
        }
        received = m

        ; CAL_CONFLICTS (details for the conflict view)
        while nconf_got < nconf and nconf_got < MAXCONF {
            @(XB) = nconf_got
            st = req(OP_CONFLICTS, 1, T_REPLY)
            if st != 0
                return fail(st)
            void g8()
            if g8() != nconf_got
                return proto()
            k = g8()
            if k == 0 or rlen != 3 + (k as uword) * 31
                return proto()
            while k != 0 and nconf_got < MAXCONF {
                conf_slot[nconf_got] = g8()
                conf_kind[nconf_got] = g8()
                conf_field[nconf_got] = g8()
                conf_kept[nconf_got] = g8()
                p = conf_text + (nconf_got as uword) * CTXT
                sys.memcopy(XB + rpos, p, 27)
                @(p + 27) = 0
                rpos += 27
                nconf_got++
                k--
            }
        }
        lkapi.hangup()
        lkapi.forget()
        return 0
    }

    sub get_error(uword dst, ubyte maxn) {
        ubyte i = 0
        while i < maxn - 1 and errmsg[i] != 0 {
            @(dst + i) = errmsg[i]
            i++
        }
        @(dst + i) = 0
    }
}
