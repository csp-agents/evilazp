#!/usr/bin/env python3
"""Compatibility wrapper for the integrated evilazp agent builder."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from azure_pipeline_cli.agent_builder import main as builder_main

    return builder_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
