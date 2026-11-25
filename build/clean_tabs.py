"""清掉专属配置浏览器里残留的自动化页签（只留 1 个），并打印前后数量。

用途：修掉"跑一次多一个页签"的历史遗留。日常由 `session._sweep_extra_pages()`
自动完成，这个脚本是给"已经堆了很多、想立刻收干净"用的。

用法：
    python build\\clean_tabs.py            # 只报告，不动
    python build\\clean_tabs.py --apply    # 真的关掉多余的
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PGY_DATA_DIR", str(ROOT))

from backend.services import pgy_system_browser as sb  # noqa: E402

PROFILE_DIR = ROOT / "profiles" / "pugongying"
OUR_HOST = "pgy.xiaohongshu.com"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理残留自动化页签")
    parser.add_argument("--apply", action="store_true", help="真的关闭多余的页签")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    endpoint = sb.ensure_edge_endpoint(
        PROFILE_DIR, start_url="about:blank", profile_kind="dedicated",
        allow_restart=True, timeout=45.0)
    print("端点:", endpoint.summary)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint.cdp_url, timeout=30_000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        pages = list(context.pages)
        print(f"当前页签总数：{len(pages)}")
        for index, page in enumerate(pages):
            try:
                url = (page.url or "")[:90]
            except Exception:
                url = "<读不到>"
            mark = "★" if OUR_HOST in url else " "
            print(f"  {mark} [{index}] {url}")

        ours = [p for p in pages if OUR_HOST in (p.url or "")]
        print(f"\n其中本工具的（{OUR_HOST}）：{len(ours)} 个")

        if not args.apply:
            print("（只报告。加 --apply 才会真的关掉多余的）")
        else:
            closed = 0
            for stale in ours[1:]:
                try:
                    stale.close()
                    closed += 1
                except Exception:
                    pass
            print(f"已关闭 {closed} 个，保留 1 个。")
            print(f"清理后页签总数：{len(context.pages)}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
