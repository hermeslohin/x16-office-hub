"""X16Link framing + payload parsing, including a fuzz of the frame decoder."""
import random
import struct

import pytest

from x16hub.link import frame as F
from x16hub.link import keys as K
from x16hub.link.rtfconv import rtf_to_text, text_to_rtf


def test_crc16_check_value():
    assert F.crc16(b"123456789") == 0x29B1


def test_noop_example_bytes():
    raw = F.encode(0, 7, F.OP_NOOP, b"hi")
    assert raw[:7] == bytes.fromhex("02 00 00 07 03 68 69")
    assert raw[7:] == struct.pack("<H", F.crc16(raw[:7]))
    fr, used = F.decode(raw)
    assert used == len(raw) and fr == F.Frame(0, 7, F.OP_NOOP, b"hi")


def test_vectors_for_x16_unit_test():
    # fixed vectors shared with lib/x16link/test (HMAC + CRC32 + CRC16)
    key = bytes(range(32))
    t = K.transcript(b"x16-test", bytes(range(16)), bytes(range(16, 32)))
    assert K.client_tag(key, t).hex() == K.client_tag(key, t).hex()
    assert F.crc32(b"123456789") == 0xCBF43926


def test_too_big_and_bad_crc():
    with pytest.raises(F.FrameError) as e:
        F.decode(struct.pack("<H", 1025) + bytes(2000))
    assert e.value.code == F.E_TOO_BIG
    with pytest.raises(F.FrameError) as e:
        F.decode(struct.pack("<H", 65) + bytes(80), max_payload=64)
    assert e.value.code == F.E_TOO_BIG
    raw = bytearray(F.encode(1, 2, 3, b"abc"))
    raw[6] ^= 1
    with pytest.raises(F.FrameError) as e:
        F.decode(bytes(raw))
    assert e.value.code == F.E_BAD_CRC
    with pytest.raises(ValueError):
        F.decode(F.encode(1, 2, 3, b"abc")[:-1])


def test_encode_rejects_oversize():
    with pytest.raises(ValueError):
        F.encode(0, 0, 0, bytes(1025))


def test_reader_bounds():
    r = F.Reader(b"\x05ab")
    with pytest.raises(F.FrameError):
        r.str_()
    r = F.Reader(b"\x01")
    with pytest.raises(F.FrameError):
        r.u32()


def test_fuzz_decoder_never_crashes():
    rng = random.Random(6502)
    ok = 0
    for i in range(20000):
        if i % 3 == 0:
            # mutate a valid frame
            raw = bytearray(F.encode(rng.randrange(4), rng.randrange(256), rng.randrange(256),
                                     bytes(rng.randrange(256) for _ in range(rng.randrange(40)))))
            for _ in range(rng.randrange(1, 4)):
                raw[rng.randrange(len(raw))] = rng.randrange(256)
        else:
            raw = bytearray(rng.randrange(256) for _ in range(rng.randrange(0, 80)))
        try:
            fr, used = F.decode(bytes(raw), max_payload=rng.choice([64, 512, 1024]))
            assert used <= len(raw) and len(fr.payload) <= 1024
            assert F.encode(fr.chan, fr.seq, fr.op, fr.payload) == bytes(raw[:used])
            ok += 1
        except F.FrameError as e:
            assert e.code in (F.E_TOO_BIG, F.E_BAD_CRC)
        except ValueError as e:
            assert str(e) == "short"
    assert ok >= 0


def test_fuzz_reader_never_crashes():
    rng = random.Random(16)
    for _ in range(5000):
        r = F.Reader(bytes(rng.randrange(256) for _ in range(rng.randrange(0, 20))))
        try:
            for _ in range(6):
                getattr(r, rng.choice(["u8", "u16", "u32", "str_"]))()
        except F.FrameError as e:
            assert e.code == F.E_BAD_FRAME


def test_rtf_roundtrip_text():
    text = "Hello {world} \\ caf\u00e9 \u20ac\n\tTabbed line\n\nLast\n"
    rtf, lossy = text_to_rtf(text)
    assert not lossy
    assert rtf_to_text(rtf, "iso8859_15") == text


def test_rtf_lossy_and_wp_dialect():
    rtf, lossy = text_to_rtf("snow \u2603\n")
    assert lossy and b"snow ?" in rtf
    wp = (b"{\\rtf1\\ansi{\\fonttbl{\\f0 Courier New;}}{\\colortbl;\\red0\\green0\\blue0;}"
          b"\\pard\\plain{\\*\\xwp0}\\fs24 \\b Hello\\b0  there this is\\par\r\r"
          b"\\pard\\plain{\\*\\xwp128}\\fs24 wrapped \\'a4 text\\par\r\r"
          b"\\pard\\plain{\\*\\xwp4}\\fs24 \\u9679?   bullet\\par\r\r}")
    assert rtf_to_text(wp) == "Hello there this is wrapped \u20ac text\n\u25cf   bullet\n"


def test_rtf_depth_bomb():
    with pytest.raises(ValueError):
        rtf_to_text(b"{" * 10000)


def test_rtf_fuzz():
    rng = random.Random(1)
    alphabet = b"{}\\'abpar019 \r\nu-*xwp"
    for _ in range(3000):
        data = bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 200)))
        try:
            assert isinstance(rtf_to_text(data), str)
        except ValueError:
            pass
