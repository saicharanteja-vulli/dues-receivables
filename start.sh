#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077
if [ -f .env ]; then set -a; . ./.env; set +a; fi
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -c 'import fastapi,uvicorn,pypdf,multipart,cryptography' 2>/dev/null || .venv/bin/pip install -r requirements.txt
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${PORT:-8120}"
