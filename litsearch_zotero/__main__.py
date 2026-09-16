"""Show the available Zotero extension commands without accessing Zotero."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only Zotero extension utilities.")
    parser.add_argument("command", nargs="?", choices=("attach-pdfs", "audit-missing-pdfs"))
    parser.parse_args()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
