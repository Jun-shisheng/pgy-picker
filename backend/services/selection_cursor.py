"""「换一批」用的翻页游标。

**它解决什么问题**：一次检索是**从第 1 页开始、每个量级凑够就停**的，而平台侧
排序是写死的（`comprehensiverank` 降序）。于是同一组条件每次跑都给同一批人 ——
`total=5000` 的池子里，你永远只看得到最靠前的那几十个。

**它怎么解决**：记住每个量级上次取到第几页，下次从那里接着往下翻。代价很小，
但拿到的是**真正没看过的新人**（不像本地打乱顺序那种假变化）。

**按筛选条件分别记游标**：换了一级/二级行业就是另一个结果集，页码对不上，
必须从头来 —— 否则会拿着"母婴的第 7 页"去翻"美妆的第 7 页"，取回来一堆莫名其妙的人。

⚠️ 落在 `ROOT` 下，与 `settings.json` 同级，所以**必须在 `.gitignore` 里**。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.core.mio_core import ROOT, _atomic_write

log = logging.getLogger("pgy.selection_cursor")

CURSOR_FILE = ROOT / "selection-cursor.json"


def key_for(first_industry: str, second_industry: str) -> str:
    """筛选条件 → 游标键。空值用 `*` 占位，免得 `a|` 与 `a|*` 撞车。"""
    return f"{first_industry or '*'}|{second_industry or '*'}"


def _read_all() -> dict:
    try:
        raw = CURSOR_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        log.warning("读游标文件失败，按没有游标处理：%s", CURSOR_FILE, exc_info=True)
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("游标文件不是合法 JSON，按没有游标处理：%s", CURSOR_FILE)
        return {}
    pages = payload.get("pages") if isinstance(payload, dict) else None
    return pages if isinstance(pages, dict) else {}


def load(first_industry: str, second_industry: str) -> int:
    """下次该从第几页开始。没有记录就是 1（从头来）。"""
    value = _read_all().get(key_for(first_industry, second_industry))
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, page)


def save(first_industry: str, second_industry: str, next_page: int) -> None:
    """记下"下次从这一页开始"。写不进去只记日志 —— 游标丢了只是下次从头来，不该让检索失败。"""
    pages = _read_all()
    pages[key_for(first_industry, second_industry)] = max(1, int(next_page))
    try:
        _atomic_write(CURSOR_FILE, json.dumps({"version": 1, "pages": pages},
                                              ensure_ascii=False, indent=2))
    except OSError:
        log.warning("写游标失败（不影响本次结果）：%s", CURSOR_FILE, exc_info=True)

# 「重新开始」不需要额外接口：`mode="fresh"` 从第 1 页取，取完就把游标写回下一页，
# 等于顺手重置了。留一个 reset() 只会变成第二套做法（CLAUDE.md 第五条）。
