#!/usr/bin/env python3
"""iLO2 remote console served over the web: a WebSocket streams JPEG frame
snapshots + logs and accepts keyboard/mouse/power commands, decoupled from
any particular renderer -- open web/index.html in a browser, or point any
other client (future HLS transcoder, diagnostics dashboard) at the same
WebSocket.

Credentials come from the environment (see .env / main.py's loader):
    ILO_HOST, ILO_USER, ILO_PASSWORD
"""
import argparse
import os
import sys
from pathlib import Path

from ilo2.dotenv import load_dotenv
from ilo2.webserver import main as run_webserver


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ILO_HOST"))
    parser.add_argument("--user", default=os.environ.get("ILO_USER", "Administrator"))
    parser.add_argument("--password", default=os.environ.get("ILO_PASSWORD"))
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--ws-port", type=int, default=8765)
    args = parser.parse_args()

    if not args.host:
        print("Missing host: set ILO_HOST in the environment, or pass --host.", file=sys.stderr)
        sys.exit(1)
    if not args.password:
        print("Missing password: set ILO_PASSWORD in the environment, or pass --password.",
              file=sys.stderr)
        sys.exit(1)

    run_webserver(args.host, args.user, args.password, args.http_port, args.ws_port)


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    main()
