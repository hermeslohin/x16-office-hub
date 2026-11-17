# X16 Office Suite - toolchain + tests. See TOOLCHAIN.md.
ROOT    := $(abspath .)
TOOLS   := $(ROOT)/tools
SRC     := $(ROOT)/src
BUILD   := $(ROOT)/build
PROG8C  := $(TOOLS)/prog8c
EMU     := $(TOOLS)/x16emu.sh
HEADLESS:= SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy

# pinned versions
PROG8_VER     := 12.3.5
TASS_VER      := 1.60.3243
SDL2_VER      := 2.32.10
EMU_COMMIT    := 4564f17002481493b58f873d368e79f68b2d2c3c
XIMODEM_COMMIT:= bff2aa83fb5c870520f486a879cd96af8b24db02
ROM_TAG       := r49

.PHONY: all prgs hello loopback loopback3 emu sdl2 64tass rom versions clean

all: prgs

prgs: $(BUILD)/hello.prg $(BUILD)/uartloop.prg

$(BUILD)/%.prg: test/%.p8
	@mkdir -p $(BUILD)
	$(PROG8C) -target cx16 -out $(BUILD) $<

# headless hello world: KERNAL output echoed to stdout, killed after 8 s
hello: $(BUILD)/hello.prg
	-$(HEADLESS) timeout 8 $(EMU) -sound none -warp -echo -prg $< -run > $(BUILD)/hello.out 2>&1
	@grep -a "HELLO FROM PROG8" $(BUILD)/hello.out && echo "hello: OK"

# UART -> libximodem (Zimodem) -> TCP 127.0.0.1:6502 echo server -> back; byte-exact check both ways
loopback: $(BUILD)/uartloop.prg
	test/run_loopback.sh

loopback3: $(BUILD)/uartloop.prg
	for n in 1 2 3; do test/run_loopback.sh > run/loop$$n.txt 2>&1 && echo "run $$n PASS" || echo "run $$n FAIL"; grep -a -E "SERVER SIDE|RESULT" run/loop$$n.txt; done

# ---- rebuild the toolchain from source (no root needed) ----
sdl2:
	cd $(SRC)/SDL2-$(SDL2_VER) && cmake -S . -B build -DCMAKE_INSTALL_PREFIX=$(TOOLS)/sdl2 \
	  -DCMAKE_BUILD_TYPE=Release -DSDL_STATIC=OFF -DSDL_TEST=OFF && cmake --build build -j16 && cmake --install build

64tass:
	$(MAKE) -C $(SRC)/64tass-$(TASS_VER)-src && cp $(SRC)/64tass-$(TASS_VER)-src/64tass $(TOOLS)/

emu:
	cd $(SRC)/libximodem && git checkout -q $(XIMODEM_COMMIT)
	cd $(SRC)/x16-emulator-uart && git checkout -q $(EMU_COMMIT) && \
	  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=$(TOOLS)/sdl2 && cmake --build build -j16
	mkdir -p $(TOOLS)/x16emu
	cp $(SRC)/x16-emulator-uart/build/x16emu $(SRC)/x16-emulator-uart/build/libximodem.so \
	   $(SRC)/x16-emulator-uart/build/makecart $(TOOLS)/x16emu/
	cp -P $(TOOLS)/sdl2/lib/libSDL2*.so* $(TOOLS)/x16emu/

rom:
	mkdir -p $(TOOLS)/rom-$(ROM_TAG) && cd $(TOOLS)/rom-$(ROM_TAG) && \
	  curl -sSL -o rom.zip https://github.com/X16Community/x16-rom/releases/download/$(ROM_TAG)/Release.R49.ROM.Image.zip && \
	  unzip -o -q rom.zip && rm rom.zip
	cp $(TOOLS)/rom-$(ROM_TAG)/rom.bin $(TOOLS)/x16emu/rom.bin

versions:
	@$(TOOLS)/jdk/bin/java -version 2>&1 | head -1
	@$(PROG8C) -version 2>&1 | head -2
	@$(TOOLS)/64tass --version
	@cd $(SRC)/x16-emulator-uart && echo "x16-emulator-uart $$(git rev-parse HEAD)"
	@cd $(SRC)/libximodem && echo "libximodem $$(git rev-parse HEAD) (zimodem $$(cat external/COMMIT))"
	@sha256sum $(TOOLS)/x16emu/rom.bin

clean:
	rm -f $(BUILD)/*.prg $(BUILD)/*.asm $(BUILD)/*.vice-mon-list $(BUILD)/hello.out
