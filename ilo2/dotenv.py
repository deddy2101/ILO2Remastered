"""Tiny .env loader (no external dependency) for webmain.py."""
import os
from pathlib import Path


def load_dotenv(path: Path):
    """Sets os.environ for any KEY=VALUE line, without overriding variables
    already set in the shell."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
