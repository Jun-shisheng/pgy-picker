"""`python -m pgy_mcp` 的入口：与 mcp_main.py 走同一条 main()。"""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
