#!/usr/bin/env python3
"""Put a secret from stdin into .env without showing it on screen.

    pbpaste | python3 scripts/env_set.py LLM_API_KEY     (copy the key first, then run this)
"""
import re
import sys
from pathlib import Path

name = sys.argv[1]
value = sys.stdin.read().strip()
if not value or "\n" in value or " " in value:
    sys.exit("The clipboard does not hold a single key. Copy the key again, then rerun.")
env = Path(__file__).resolve().parent.parent / ".env"
text = env.read_text() if env.exists() else ""
line = f"{name}={value}"
text, n = re.subn(rf"^{re.escape(name)}=.*$", line, text, flags=re.M)
if not n:
    text = text.rstrip("\n") + "\n" + line + "\n"
env.write_text(text)
env.chmod(0o600)
print(f"{name} saved in .env ({len(value)} characters, starts with {value[:3]}...)")
