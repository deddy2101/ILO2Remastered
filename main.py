#!/usr/bin/env python3
"""HP iLO2 remote console client (video + keyboard/mouse + power control).

Credentials are read from the environment so they never end up hardcoded in
source (see .env.example):
    ILO_HOST      required, e.g. 192.168.1.100
    ILO_USER      default Administrator
    ILO_PASSWORD  required (no default)

They can be overridden with --host/--user/--password on the command line.
"""
import argparse
import os
import sys
from pathlib import Path

from ilo2.dotenv import load_dotenv
from ilo2.gui import IloApp


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ILO_HOST"))
    parser.add_argument("--user", default=os.environ.get("ILO_USER", "Administrator"))
    parser.add_argument("--password", default=os.environ.get("ILO_PASSWORD"))
    args = parser.parse_args()

    if not args.host:
        print("Missing host: set ILO_HOST in the environment, or pass --host.", file=sys.stderr)
        sys.exit(1)
    if not args.password:
        print("Missing password: set ILO_PASSWORD in the environment, or pass --password.",
              file=sys.stderr)
        sys.exit(1)

    app = IloApp(args.host, args.user, args.password)
    app.start()


if __name__ == "__main__":
    main()
