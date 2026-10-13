"""「换一批」用的翻页游标。

**它解决什么问题**：一次检索是**从第 1 页开始、每个量级凑够就停**的，而平台侧
排序是写死的（`comprehensiverank` 降序）。于是同一组条件每次跑都给同一批人 ——
`total=5000` 的池子里，你永远只看得到最靠前的那几十个。

**它怎么解决**：记住每个量级上次取到第几页，下次从那里接着往下翻。代价很小，
但拿到的是**真正没看过的新人**（不像本地打乱顺序那种假变化）。

**两级键**：外层按「筛选条件 + 检索词」分文件键，内层按**「档位 × 关键词」**分格
（内层键由调用方 `selection_service.combo_key()` 生成，本模块只当它是字符串）。
两者都不能省：换了行业/检索词就是另一个结果集；同一档里不同关键词、以及
兜底那趟（不带检索词）也都是**不同的池子**，页码互相挪用就会取回一堆莫名其妙的人。

⚠️ 落在 `ROOT` 下，与 `settings.json` 同级，所以**必须在 `.gitignore` 里**。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from backend.core.mio_core import ROOT, _atomic_write

log = logging.getLogger("pgy.selection_cursor")

CURSOR_FILE = ROOT / "selection-cursor.json"


def key_for(first_industry: str, second_industry: str, keywords: str = "") -> str:
    """筛选条件 → 游标键。空值用 `*` 占位，免得 `a|` 与 `a|*` 撞车。

    **关键词也要进 key**：不同关键词搜出来的是**不同的结果集**，
    拿"A 关键词的第 7 页"去当"B 关键词的第 7 页"会取回一堆莫名其妙的人。
    """
    return f"{first_industry or '*'}|{second_industry or '*'}|{keywords or '*'}"


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


def load(first_industry: str, second_industry: str, keywords: str = "") -> dict[str, int]:
    """下次每一格该从第几页开始，形如 `{"超头部KOL(40w-50w) × 体验": 3, ...}`；没有就是 `{}`。

    ⚠️ **键是「档位 × 关键词」**（2026-10-13 起）：档位是独立的粉丝区间查询，
    同一档里不同关键词又是不同结果集 —— "A 格的第 7 页"对 B 格毫无意义。

    旧版文件兼容情况：值可能是**一个整数**（最早，所有档位共用一个页码）或
    **档位名做键的字典**（次早）。两种情况读出来都不报错，只是键与新的组合键
    **匹配不上** ⇒ 那几格从第 1 页开始；因为 `next` 已是默认模式、且续到空页会自动
    回落第 1 页，升级不会让人卡住。
    """
    value = _read_all().get(key_for(first_industry, second_industry, keywords))
    if isinstance(value, dict):
        out: dict[str, int] = {}
        for key, raw in value.items():
            try:
                out[str(key)] = max(1, int(raw))
            except (TypeError, ValueError):
                continue
        return out
    try:
        return {"*": max(1, int(value))}      # 旧版：一个页码管所有档位
    except (TypeError, ValueError):
        return {}


def save(first_industry: str, second_industry: str, next_pages: dict,
         keywords: str = "") -> None:
    """记下"每个档位下次从第几页开始"。写不进去只记日志
    —— 游标丢了只是下次从头来，不该让检索失败。"""
    pages = _read_all()
    clean: dict[str, int] = {}
    for key, value in (next_pages or {}).items():
        try:
            clean[str(key)] = max(1, int(value))
        except (TypeError, ValueError):
            continue
    pages[key_for(first_industry, second_industry, keywords)] = clean
    try:
        _atomic_write(CURSOR_FILE, json.dumps({"version": 2, "pages": pages},
                                              ensure_ascii=False, indent=2))
    except OSError:
        log.warning("写游标失败（不影响本次结果）：%s", CURSOR_FILE, exc_info=True)

# 「重新开始」不需要额外接口：`mode="fresh"` 从第 1 页取，取完就把游标写回下一页，
# 等于顺手重置了。留一个 reset() 只会变成第二套做法（CLAUDE.md 第五条）。
