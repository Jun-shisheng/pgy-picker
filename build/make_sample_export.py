"""用**真实抓到的响应**跑一遍映射 + 导出，产出一份样例交付 Excel。

这是对「能不能归类输出到那张表」的直接验证：输入是探针从平台抓的原始 JSON，
输出是严格对齐客户表头的 xlsx。

用法：
    python build\\make_sample_export.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PGY_DATA_DIR", str(ROOT))

from backend.providers.pugongying.fields import COLUMNS  # noqa: E402
from backend.providers.pugongying.mapper import build_gate, map_page, sort_rows  # noqa: E402
from backend.services import settings_service  # noqa: E402
from backend.services.export_service import export_excel  # noqa: E402


def latest_response() -> Path:
    dirs = sorted((ROOT / "docs" / "evidence").glob("bloggers-*"), reverse=True)
    for d in dirs:
        candidate = d / "blogger-search.response.json"
        if candidate.exists():
            return candidate
    raise SystemExit("找不到探针响应，请先跑 build\\probe_blogger_data.py")


def main() -> int:
    source = latest_response()
    payload = json.loads(source.read_text(encoding="utf-8"))
    kols = (payload.get("data") or {}).get("kols") or []
    if not kols:
        raise SystemExit(f"{source} 里没有 kols 数据")
    print(f"输入：{source}")
    print(f"平台返回：total={payload['data'].get('total')}，本页 {len(kols)} 条")

    # 两道把关（粉丝档位 + 预算）走**与真实检索同一个实现** —— 曾经这里只做了
    # 粉丝档位那道，生成出来的样例里混进了「两个报价都超」的达人（用户一眼看出）。
    keep, drops = build_gate()
    kept = [item for item in kols if keep(item)]
    rows = sort_rows(map_page(kept))   # 与真实检索同一套排序
    print(f"把关：保留 {len(rows)} 条（剔除 档位外 {drops['out_of_range']} / "
          f"超预算 {drops['over_budget']}）")
    if drops["samples"]:
        print("  超预算样例：")
        for sample in drops["samples"]:
            print(f"    {sample}")
    # **输出到设置里的目录**（默认 `exports/`），与真实检索的产物同一处、同一命名规则，
    # 不写死在某个固定目录 —— 这样"设置里改了输出位置"对所有产物都生效。
    out_dir = settings_service.export_dir()
    # ⚠️ 文件名**必须带「离线回放」前缀**：本脚本读的是 `docs/evidence/` 里的**旧抓包**，
    # 不是实时数据。曾经因为它产出的文件长得和真实检索一模一样，被误当成了实时结果
    # 去和平台对照，白查半天（用户 2026-10-12：「每次跑的肯定得是实时最新的数据」）。
    # 真实交付一律走 `try_selection.py` / 客户端接口 —— 那条路径每次都是现搜的。
    out = out_dir / f"【离线回放·非实时】达人提报-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    # 元信息那张表已删（2026-10-12 用户要求交付物只有达人信息），所以来源响应路径
    # 改在这里打出来 —— 本脚本是排障用的，得让人看得见它读的是哪份抓包。
    print(f"（离线回放：读的是 {source.relative_to(ROOT)}）")
    export_excel(rows, out)

    print(f"\n输出：{out}")
    print(f"\n== 前 5 行预览（列序 = 客户表头）==")
    print("  " + " | ".join(COLUMNS))
    for row in rows[:5]:
        cells = []
        for column in COLUMNS:
            value = row.get(column)
            text = "" if value is None else str(value)
            cells.append(text[:14])
        print("  " + " | ".join(cells))

    # 字段填充率：哪些列大面积空 → 需要回头看口径/字段对不对
    print("\n== 各列填充率（非空占比）==")
    for column in COLUMNS:
        filled = sum(1 for r in rows if r.get(column) not in (None, ""))
        bar = "█" * int(filled / len(rows) * 20)
        print(f"  {column:<12} {filled:>3}/{len(rows)}  {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
