"""Tiny stdlib-only `.env` loader.

Not a new dependency (no python-dotenv) — it's a dozen lines of parsing
for something this simple. Only used for local-dev convenience, so you can
put `DATABASE_URL=...` in `backend/.env` once instead of exporting it in
every shell session; in a real deployment you'd set actual environment
variables instead, and this becomes a no-op (no `.env` file present).

Must run before `import db`, since db.py reads DATABASE_URL at import
time — see the top of main.py.
"""
import os
from pathlib import Path


def load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
