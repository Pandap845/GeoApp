# Minimal makefile for Sphinx documentation.

SPHINXOPTS    ?=
SPHINXBUILD   ?= python -m sphinx
SOURCEDIR     = .
BUILDDIR      = _build

.PHONY: help clean html linkcheck latexpdf Makefile

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

clean:
	rm -rf "$(BUILDDIR)"

# Catch-all: route anything else (html, latexpdf, linkcheck, ...) to sphinx-build.
%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
