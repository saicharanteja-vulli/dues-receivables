#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ "$(uname)" = Darwin ]; then
  mkdir -p .venv/swift-cache
  swiftc -module-cache-path .venv/swift-cache scripts/ocr.swift -o .venv/bin/dues-ocr
else
  command -v pdftoppm >/dev/null
  command -v tesseract >/dev/null
fi
printf 'Local OCR is ready.\n'
