#!/usr/bin/env python3
"""One-shot site-side receiver. No daemon, arbitrary remote shell or model execution."""
import sys
from seo_agents.sitebridge import main

for stream in (sys.stdin,sys.stdout,sys.stderr):
    if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='strict')
if __name__ == '__main__':
    raise SystemExit(main())
