PREFIX ?= /usr/local
BINDIR := $(PREFIX)/bin

.PHONY: install uninstall

install:
	install -Dm755 hypr-cava-visualizer $(DESTDIR)$(BINDIR)/hypr-cava-visualizer
	install -Dm755 hypr-cava-visualizer.py $(DESTDIR)$(BINDIR)/hypr-cava-visualizer.py

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/hypr-cava-visualizer
	rm -f $(DESTDIR)$(BINDIR)/hypr-cava-visualizer.py
