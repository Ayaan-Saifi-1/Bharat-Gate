#!/usr/bin/env python3
"""BharatGate root manage.py CLI wrapper."""

import sys
from bharat_gate.cli.main import run_cli

if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
