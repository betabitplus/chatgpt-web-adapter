APP := build/WKChatGPTAuthority.app
MACOS := $(APP)/Contents/MacOS
SRC := WKChatGPTAuthority.m
BIN := $(MACOS)/WKChatGPTAuthority

.PHONY: build clean login smoke

build:
	mkdir -p $(MACOS)
	cp Info.plist $(APP)/Contents/Info.plist
	clang -fobjc-arc -framework Cocoa -framework WebKit $(SRC) -o $(BIN)
	codesign --force --deep --sign - $(APP)

login: build
	$(BIN) --login

smoke: build
	$(BIN) --prompt "Reply with exactly: WK_SEQ_OK" --expect-marker WK_SEQ_OK --timeout 35

clean:
	rm -rf build
