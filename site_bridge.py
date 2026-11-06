#!/usr/bin/env python3
"""One-shot site-side receiver. No daemon, arbitrary remote shell or model execution."""
from seo_agents.sitebridge import main
if __name__ == '__main__':
    raise SystemExit(main())
