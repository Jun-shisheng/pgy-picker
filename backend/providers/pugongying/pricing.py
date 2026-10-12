"""报价档位与预算把关。

**规则**（用户 2026-10-12 确认，出自 `资料和说明/说明.txt`）：

    平台报价上限 = 对应粉丝档位的公司基准价 × 倍数（默认 1.3）
    **图文、视频任一报价在上限之内 → 通过**；两个都超 → 不进名单
    某形态没有报价 → 交付表该列**留空**，且不参与判定

**价格是商业敏感数据，绝不写进本文件**（本文件进 git）。真实价格放在
**不入库**的配置文件里，运行时读取：

    1. `<数据目录>/pricing.json`          —— 装机后的正式位置（用户可改）
    2. `<项目根>/资料和说明/报价档位.json`   —— 开发期来源（已 gitignore）

仓库里只有 `pricing.example.json`（**不含真实价格**）作为格式样板。
读不到配置时**不猜、不硬编码**：记录警告并**关闭预算把关**，
让"少筛一层"可见，而不是拿假价格把达人错杀掉。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.core.mio_core import ROOT

log = logging.getLogger("pgy.pricing")

DEFAULT_MULTIPLIER = 1.3
CONFIG_CANDIDATES = (
    ROOT / "pricing.json",
    ROOT / "资料和说明" / "报价档位.json",
)


@dataclass(frozen=True)
class Tier:
    label: str
    bracket: str
    low: int
    high: int
    image: int
    video: int

    @property
    def name(self) -> str:
        return f"{self.label}({self.bracket})"


@dataclass
class Pricing:
    multiplier: float
    tiers: list[Tier]

    def tier_for(self, fans: int | None) -> Tier | None:
        if not isinstance(fans, int):
            return None
        for tier in self.tiers:
            if tier.low <= fans < tier.high:
                return tier
        return None

    def cap(self, fans: int | None, kind: str) -> int | None:
        """`kind` 取 `image` 或 `video`，返回该档位的报价上限（基准价 × 倍数）。"""
        tier = self.tier_for(fans)
        if tier is None:
            return None
        base = tier.image if kind == "image" else tier.video
        if not base:
            return None
        return int(round(base * self.multiplier))

    def within_budget(self, fans: int | None, image_price, video_price) -> tuple[bool, str]:
        """达人的报价是否在公司预算内。返回 `(是否通过, 原因)`。

        **判定口径（用户 2026-10-12 明确）：「有一个在规则之下就可以」**
        —— 图文、视频**任一**报价 ≤ 自己的上限，即通过；两个都超才剔除。

        为什么是这样而不是"任一超出就剔"：达人常只报一种形态的价。若要求两个都达标，
        只做图文的达人会因为没有视频报价而被误杀。用户的原话是"有一个在筛选规则之下
        就可以"，这条同时解决了"形态不全"和"预算"两件事。

        价格缺失（None/0）**不参与判定**，交付表里该列**留空**（不写 0、不编造）。
        两个价都没有 → 无从判断，剔除（并在返回里说明原因，不静默丢）。
        """
        tier = self.tier_for(fans)
        if tier is None:
            return False, "粉丝数不在任何报价档位内"

        checked: list[tuple[str, int, int]] = []
        for kind, price, label in (("image", image_price, "图文"), ("video", video_price, "视频")):
            if not price:                      # 该形态不报价 → 不参与判定
                continue
            cap = self.cap(fans, kind)
            if cap is not None:
                checked.append((label, int(price), cap))

        if not checked:
            return False, "图文、视频都没有报价，无从判断是否超预算"
        for label, price, cap in checked:
            if price <= cap:
                return True, ""
        return False, "；".join(f"{label} {price} > 上限 {cap}" for label, price, cap in checked)

    def over_kinds(self, fans: int | None, image_price, video_price) -> list[str]:
        """**哪些形态超了上限**（返回如 `["图文"]`）。

        用户 2026-10-12：「如果一个超一个没超需要进行标记」——那种达人按规则能留下
        （有一个在预算内），但交付表上要让人一眼看出是哪一项超了。
        """
        tier = self.tier_for(fans)
        if tier is None:
            return []
        over: list[str] = []
        for kind, price, label in (("image", image_price, "图文"), ("video", video_price, "视频")):
            if not price:
                continue
            cap = self.cap(fans, kind)
            if cap is not None and int(price) > cap:
                over.append(label)
        return over


_cache: Pricing | None = None
_loaded_from: Path | None = None


def load(force: bool = False) -> Pricing | None:
    """读取报价配置；读不到返回 None（调用方据此关闭预算把关）。"""
    global _cache, _loaded_from
    if _cache is not None and not force:
        return _cache
    for candidate in CONFIG_CANDIDATES:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("报价配置 %s 解析失败：%s", candidate, exc)
            continue
        tiers = []
        for item in payload.get("tiers") or []:
            try:
                tiers.append(Tier(
                    label=str(item["label"]), bracket=str(item.get("bracket") or ""),
                    low=int(item["min"]), high=int(item["max"]),
                    image=int(item.get("image") or 0), video=int(item.get("video") or 0),
                ))
            except Exception as exc:
                log.warning("报价档位条目无效，跳过：%r（%s）", item, exc)
        if not tiers:
            continue
        _cache = Pricing(multiplier=float(payload.get("multiplier") or DEFAULT_MULTIPLIER),
                         tiers=tiers)
        _loaded_from = candidate
        log.info("已加载报价档位：%s 个档位，倍数 %s，来源 %s",
                 len(tiers), _cache.multiplier, candidate)
        return _cache
    log.warning("没有找到报价配置（找过 %s），**预算把关已关闭**：名单不会因报价被剔除。",
                "、".join(str(p) for p in CONFIG_CANDIDATES))
    return None


def source() -> Path | None:
    load()
    return _loaded_from


def within_budget(fans, image_price, video_price) -> tuple[bool, str]:
    """对外入口：没有配置时一律通过（并已在上层告警）。"""
    config = load()
    if config is None:
        return True, ""
    return config.within_budget(fans, image_price, video_price)


def over_kinds(fans, image_price, video_price) -> list[str]:
    """哪些形态超了上限（如 `["图文"]`）。没有配置时返回空。"""
    config = load()
    if config is None:
        return []
    return config.over_kinds(fans, image_price, video_price)
