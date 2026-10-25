#!/usr/bin/env python3
"""Main module execution entry point for python -m bharat_gate.cli."""

import sys
from bharat_gate.cli.main import run_cli

if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
