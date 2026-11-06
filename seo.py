#!/usr/bin/env python3
"""Portable entry point: Python 3.11+, no pip installation required."""
import sys
from seo_agents.cli import main

for stream in (sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')

if __name__ == '__main__':
    raise SystemExit(main())
