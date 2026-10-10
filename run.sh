#!/bin/sh
set -e
cd "$(dirname "$0")"
cp -n .env.example .env 2>/dev/null || true
python3 main.py --status || true
python3 main.py --cycles 10
