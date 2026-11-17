# X16 Office Suite — toolchain (Linux dev machine, aarch64)

Everything lives under `~/x16-office/`, user-level, no root used. Built and verified 2026-11-17.

```
tools/     prog8c wrapper + jar, Temurin JDK 21, 64tass, local SDL2, x16emu bundle, R49 ROM
src/       x16-emulator-uart, libximodem, SDL2-2.32.10, 64tass sources
test/      hello.p8, uartloop.p8, echo_server.py, verify_recv.py, run_loopback.sh
build/     compiled .prg/.asm
run/       loopback run logs + server captures, ximodem-data/ (virtual modem config)
Makefile   make prgs | hello | loopback | loopback3 | versions | emu | sdl2 | 64tass | rom
```

## Pinned versions

| Component | Version / commit | Source |
|---|---|---|
| Prog8 compiler | **v12.3.5** (`tools/prog8c-12.3.5-all.jar`) | github.com/irmen/prog8 releases |
| JDK (for prog8c) | Temurin **21.0.13** LTS, aarch64 (`tools/jdk`) | api.adoptium.net. System java is 1.8, too old. |
| 64tass | **1.60.3243** built from source (`tools/64tass`) | sourceforge tass64, `64tass-1.60.3243-src.zip` |
| SDL2 | **2.32.10** built from source → `tools/sdl2` (dummy/offscreen video, dummy audio only) | libsdl-org/SDL release |
| x16emu (UART fork) | morphinejh/x16-emulator-uart **`4564f17002481493b58f873d368e79f68b2d2c3c`** (2026-11-14, "Update for libximodem") | github |
| libximodem | morphinejh/libximodem **`bff2aa83fb5c870520f486a879cd96af8b24db02`** (2026-09-09). This matches the fork's `extern/libximodem` submodule pin. | github |
| Zimodem (in libximodem) | bozimmerman/Zimodem **`a24225cc1b6c219e6c0764a4e6b1a388356a1dd5`** + 11 patches (`patches/SERIES`) | vendored |
| ROM | X16Community x16-rom **R49** `rom.bin`, sha256 `b81654cc…931b819a` | x16-rom releases `Release.R49.ROM.Image.zip` |

The fork is based on official emulator R49 (its RELEASES.md top entry is R49 "Pyrite"), so R49 is the matching ROM.
The x16emu build has TLS (OpenSSL 3.0.13) and no SSH, because libssh2 dev files are missing. Zimodem's SSH client is not needed for this project.

## Build commands (all user-level)

```bash
# Prog8 + JDK
curl -sSLO https://github.com/irmen/prog8/releases/download/v12.3.5/prog8c-12.3.5-all.jar        # -> tools/
curl -sSL -o jdk.tgz "https://api.adoptium.net/v3/binary/latest/21/ga/linux/aarch64/jdk/hotspot/normal/eclipse"
tar xzf jdk.tgz -C tools/jdk --strip-components=1
# tools/prog8c = wrapper: puts tools/ (64tass) on PATH, runs tools/jdk/bin/java -jar prog8c-12.3.5-all.jar
make 64tass        # make in src/64tass-1.60.3243-src, copy binary to tools/
make sdl2          # cmake SDL2 -> tools/sdl2
make emu           # checks out pinned commits; cmake x16-emulator-uart with -DCMAKE_PREFIX_PATH=tools/sdl2
                   # (CMake auto-uses the sibling ../libximodem checkout, needs python3), copies bundle to tools/x16emu/
make rom           # R49 rom.bin -> tools/x16emu/rom.bin
make versions
```

The fork's `quickstart-linux.md` is the generic upstream user guide. It has no build steps. The real build is the CMake one above. `USE_XIMODEM` is ON by default when `../libximodem` or the submodule is present.

## Running

`tools/x16emu.sh` sets `LD_LIBRARY_PATH` to the bundle and passes `-rom tools/x16emu/rom.bin`.

```bash
# compile
tools/prog8c -target cx16 -out build test/hello.p8
# headless (no display needed)
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy tools/x16emu.sh -sound none -echo -prg build/hello.prg -run
# with the virtual Zimodem on UART1 ($9FE0); config/phonebook kept in run/ximodem-data
SDL_VIDEODRIVER=dummy tools/x16emu.sh -sound none -uart1 ximodem:$HOME/x16-office/run/ximodem-data -prg X.prg -run
# extra modem tracing: XIMODEM_VERBOSE=1
```

Headless output: `-echo` mirrors KERNAL CHROUT to stdout. For machine-readable logs, Prog8 `emudbg.console_write()` writes to the emulator debug port ($9FBx), which also goes to stdout. `make hello` shows `HELLO FROM PROG8 ON X16!` ✔.

**Windowed GUI:** the local SDL2 has no X11/Wayland/ALSA/Pulse backends because those dev headers aren't installed. It runs headless only. See "Root steps" below.

## UART loopback test (8-bit transparency)

`make loopback` (or `make loopback3` for 3 runs) runs `test/run_loopback.sh`:

1. `echo_server.py` is an asyncio TCP server on 127.0.0.1:6502. It logs every chunk as hex, echoes it back unchanged, and saves the full stream to `run/loopback-*/conn1.bin`.
2. x16emu runs headless with `-uart1 ximodem:run/ximodem-data` and executes `build/uartloop.prg`, which:
   - programs the TL16C2550 port 1 @ `$9FE0`: LCR=$80, DLL=8 (921600/115200), LCR=$03 (8N1), IER=0 (polled), FCR=$87 (FIFO on + reset, RX trigger 8), MCR=$23 (DTR+RTS+**AFE** auto RTS/CTS)
   - sends `ATZ`, `ATI`, `ATE0`, then `ATD"127.0.0.1:6502"`, and waits for `CONNECT`
   - sends 531 bytes (0..255 twice, then `+++`, `FF FF`, `CR LF CR NUL`, `ESC [ A`, XON `11`, XOFF `13`, `FF FF FF FB 01` telnet IAC/WILL) in 16-byte FIFO bursts on THRE, while reading the echo back
   - compares byte-for-byte, dumps all received bytes to the host log, then hangs up with guard time + `+++` + `ATH`.
3. `verify_recv.py` checks the server-side capture against the expected payload.

### Result: **8-bit clean in both directions ✔** (3 of 3 runs passed)

```
UARTLOOP sent=0213 received=0213 mismatches=0000 jiffies=0023     (531/531 bytes; $23=35 jiffies ≈0.58 s incl. a 0.5 s settle wait)
RESULT PASS 8-bit clean
expected 531 bytes, server received 531 bytes
SERVER SIDE: PASS - X16->network direction is byte-exact (8-bit clean)
```

- **X16 → net:** the server got exactly the 531 bytes sent. `FF` was not doubled, nothing was stripped, and CR/LF/NUL, XON/XOFF and ESC sequences all came through.
- **net → X16:** the echo came back byte-exact. Telnet IAC `FF FF`/`FF FB 01` was not interpreted, and there was no PETSCII translation.
- An in-band `+++` with no guard time passed through as data and did not drop to command mode.
- The hangup `+++` (sent after a ≥1 s guard) **is forwarded to the server** before Zimodem switches to command mode. Servers should expect those 3 trailing bytes, or the protocol should close the connection itself instead of using `+++`.

### Zimodem settings needed for a transparent link
Use the defaults with plain `ATD"host:port"` and **no dial modifiers**. Per the Zimodem source (`connSettings.ino`, `zstream.ino`):
- Never add `T` (telnet IAC handling) or `P` (PETSCII↔ASCII translation) to the ATD modifiers.
- Keep flow control on RTS/CTS (`DEFAULT_FCT FCT_RTSCTS` in the X16 build). Under XON/XOFF flow control (`FCT_NORMAL`), bytes `11`/`13` are eaten by the modem.
- Don't turn on echo (`E`). `ATE0` only affects command mode.
- In stream mode, an ESC (`1B`) starts a short look-ahead buffer for ANSI sequences. It only regroups the bytes and still forwards them unchanged, as the test confirmed.
- Baud: the virtual modem stays muted until the UART divisor matches its rate (115200 → divisor 8).

### Emulator observation (to check against real hardware)
`serialuartTL16C2550.cpp:360` puts CTS in MSR **bit 5** and DSR in bit 4. On a real 16C550/TL16C2550, CTS is bit 4 (`$10`) and DSR is bit 5 (`$20`). With AFE on, this doesn't matter. Code that polls MSR for CTS by hand could act differently in the emulator than on the TexElec card, so prefer AFE.

## Root steps pending for the machine owner (optional; nothing above is blocked)

Only needed for a **windowed** emulator with sound, or SSH in the modem:

```bash
sudo apt-get install -y libsdl2-dev libssh2-1-dev
# then: cd ~/x16-office && rm -rf src/x16-emulator-uart/build && \
#   (cd src/x16-emulator-uart && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j16)
#   and point tools/x16emu.sh at the system SDL2 (drop the bundled libSDL2*.so)
```
These change system packages only (dev headers and libs). Check with `pkg-config --modversion sdl2 libssh2`. Undo with `sudo apt-get remove libsdl2-dev libssh2-1-dev`. An optional system JDK (`openjdk-21-jdk-headless`) isn't needed because `tools/jdk` is used.
