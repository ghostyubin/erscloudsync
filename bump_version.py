#!/usr/bin/env python3
"""Stamp app/version.py with the current build time.

Format: ``DEV_YYYYMMDDHHMM`` (local time of the build).

Every refactor / image rebuild should bump the version so the UI (sidebar
footer, 全局设置, 关于) tells you at a glance which build is running.

Usage:
    python3 bump_version.py                 # stamp with "now"
    python3 bump_version.py DEV_202610261243  # stamp with an explicit value
    python3 bump_version.py --print         # just print the current value
"""
import datetime
import pathlib
import re
import sys

VERSION_FILE = pathlib.Path(__file__).resolve().parent / "app" / "version.py"
PATTERN = re.compile(r'APP_VERSION\s*=\s*"([^"]*)"')
VALID = re.compile(r"^DEV_\d{12}$")


def read_current() -> str:
    match = PATTERN.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not match:
        sys.exit(f"APP_VERSION not found in {VERSION_FILE}")
    return match.group(1)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--no-commit"]

    if "--print" in args:
        print(read_current())
        return 0

    new = args[0] if args else datetime.datetime.now().strftime("DEV_%Y%m%d%H%M")
    if not VALID.match(new):
        sys.exit(f"bad version {new!r}: expected DEV_YYYYMMDDHHMM")

    old = read_current()
    if old == new:
        print(f"version unchanged: {old}")
        return 0

    source = VERSION_FILE.read_text(encoding="utf-8")
    VERSION_FILE.write_text(
        PATTERN.sub(f'APP_VERSION = "{new}"', source, count=1), encoding="utf-8")
    print(f"version: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
