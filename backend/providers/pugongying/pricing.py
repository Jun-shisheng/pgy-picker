"""报价档位与预算把关。

**规则**（用户 2026-10-12 确认，出自 `资料和说明/说明.txt`）：

    平台报价上限 = 对应粉丝档位的公司基准价 × 倍数（默认 1.3）
    **图文、视频任一报价在上限之内 → 通过**；两个都超 → 不进名单
    某形态没有报价 → 交付表该列**留空**，且不参与判定

**价格是商业敏感数据，绝不写进本文件**（本文件进 git）。真实价格放在
**不入库**的配置文件里，运行时读取：

    1. `<数据目录>/pricing.json`          —— 用户保存的正式位置（设置页可改）
    2. `<项目根>/资料和说明/报价档位.json`   —— 开发期来源（已 gitignore）
    3. `<sidecar 内置>/pricing.default.json` —— 打包时从开发期 JSON 复制进来
       （`--add-data`，含真实价格，不入库）

**第一次运行**（数据目录里没有 `pricing.json`）时，会拿默认档位自动初始化
`pricing.json` —— 用户 2026-11-10 要求「打包打开第一次出来的就是开发那个 json 的数值」。
设置页（`GET/PUT /api/pricing`）可以手动改每个档位的图文/视频基准价和倍数；
**档位区间/名称固定**（选号分档在 `fields.TIERS`），改区间会被拒绝。

读不到任何配置时**不猜、不硬编码**：记录警告并**关闭预算把关**，
让"少筛一层"可见，而不是拿假价格把达人错杀掉。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.core.mio_core import ROOT
from .fields import TIERS

log = logging.getLogger("pgy.pricing")

DEFAULT_MULTIPLIER = 1.3
#: 用户保存的正式位置（设置页的「保存档位」写这里）。**必须保持 index 0**：
#: `tests/test_export_payload.py` 用 `CONFIG_CANDIDATES[0].name` 校验安装脚本
#: 不会在升级时把它删掉。
CONFIG_PATH = ROOT / "pricing.json"
CONFIG_CANDIDATES = (
    CONFIG_PATH,
    # 开发期来源（已 gitignore）
    ROOT / "资料和说明" / "报价档位.json",
    # 打包时由 build-sidecar.ps1 / build-installer.ps1 从开发期 JSON 复制进来
    # （`--add-data`），装机后第一次启动用它初始化 pricing.json。
    Path(__file__).with_name("pricing.default.json"),
    ROOT / "pricing.default.json",
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


def _read_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("报价配置必须是 JSON 对象")
    return payload


def _parse_payload(payload: dict) -> Pricing:
    """解析并校验。**只允许改价格，不允许改档位区间/名称**。

    选号链路的分档（`fields.TIERS`）是写死的；如果报价配置的区间跟它对不上，
    预算把关会拿错档位算上限。所以这里强制六档一致，改区间直接拒绝。
    """
    raw_multiplier = payload.get("multiplier")
    try:
        # ⚠️ 不能写 `or DEFAULT_MULTIPLIER`：0 是非法值，会被静默当成默认 1.3。
        multiplier = (DEFAULT_MULTIPLIER if raw_multiplier is None or raw_multiplier == ""
                      else float(raw_multiplier))
    except (TypeError, ValueError):
        raise ValueError("倍数必须是数字")
    if not (0 < multiplier <= 10):
        raise ValueError("倍数必须在 0–10 之间")

    tiers: list[Tier] = []
    for item in payload.get("tiers") or []:
        if not isinstance(item, dict):
            raise ValueError(f"档位条目必须是对象：{item!r}")
        try:
            tiers.append(Tier(
                label=str(item["label"]), bracket=str(item.get("bracket") or ""),
                low=int(item["min"]), high=int(item["max"]),
                image=int(item.get("image") or 0), video=int(item.get("video") or 0),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"档位条目无效：{item!r}（{exc}）")
    if not tiers:
        raise ValueError("至少需要一个档位")

    expected = {(low, high, label, bracket) for low, high, label, bracket in TIERS}
    got = {(t.low, t.high, t.label, t.bracket) for t in tiers}
    if len(tiers) != len(TIERS) or got != expected:
        raise ValueError("档位区间/名称必须与当前六个档位一致（只能改图文/视频价格、倍数）")
    return Pricing(multiplier=multiplier, tiers=tiers)


def _payload_of(config: Pricing) -> dict:
    return {
        "multiplier": config.multiplier,
        "tiers": [
            {"label": t.label, "bracket": t.bracket, "min": t.low, "max": t.high,
             "image": t.image, "video": t.video}
            for t in config.tiers
        ],
    }


def default_payload() -> dict | None:
    """开发期/打包内置的默认档位（真实价格，不入库）。读不到返回 None。"""
    for candidate in CONFIG_CANDIDATES[1:]:
        if not candidate.is_file():
            continue
        try:
            payload = _read_payload(candidate)
            _parse_payload(payload)          # 校验；坏文件不拿来当默认
            return payload
        except Exception as exc:
            log.warning("默认报价配置 %s 不可用：%s", candidate, exc)
    return None


def default_snapshot() -> dict | None:
    payload = default_payload()
    if payload is None:
        return None
    return _payload_of(_parse_payload(payload))


def blank_snapshot() -> dict:
    """没有默认值时给设置页的空白六档（价格 0），只用于填表，不会被加载把关。"""
    return {
        "multiplier": DEFAULT_MULTIPLIER,
        "tiers": [
            {"label": label, "bracket": bracket, "min": low, "max": high,
             "image": 0, "video": 0}
            for low, high, label, bracket in TIERS
        ],
    }


def snapshot() -> dict:
    """设置页用：当前生效的配置；没有则回落到开发默认；再没有给空白六档。"""
    config = load()
    if config is not None:
        return _payload_of(config)
    return default_snapshot() or blank_snapshot()


def save(payload: dict) -> Pricing:
    """保存到 `<数据目录>/pricing.json`（设置页的「保存档位」）。"""
    parsed = _parse_payload(payload)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_payload_of(parsed), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    global _cache, _loaded_from
    _cache, _loaded_from = parsed, CONFIG_PATH
    log.info("报价档位已保存：%s 个档位，倍数 %s，写入 %s",
             len(parsed.tiers), parsed.multiplier, CONFIG_PATH)
    return parsed


def load(force: bool = False) -> Pricing | None:
    """读取报价配置；读不到返回 None（调用方据此关闭预算把关）。

    **第一次运行**（`pricing.json` 不存在）时，如果开发期/打包内置的默认档位存在，
    自动把它写进 `pricing.json` 并生效 —— 用户 2026-11-10 要求「打包打开第一次
    出来的就是开发那个 json 的数值」。
    """
    global _cache, _loaded_from
    if _cache is not None and not force:
        return _cache

    if CONFIG_PATH.is_file():
        try:
            _cache = _parse_payload(_read_payload(CONFIG_PATH))
            _loaded_from = CONFIG_PATH
            log.info("已加载报价档位：%s 个档位，倍数 %s，来源 %s",
                     len(_cache.tiers), _cache.multiplier, CONFIG_PATH)
            return _cache
        except Exception as exc:
            # **不要**拿默认值覆盖用户文件：先保留现场，让设置页报错并重新保存。
            log.warning("pricing.json 无效（%s），预算把关关闭；请在「设置 → 报价档位」重新保存",
                        exc)
            return None

    payload = default_payload()
    if payload is not None:
        try:
            log.info("首次运行：用开发默认档位初始化 %s", CONFIG_PATH)
            return save(payload)
        except Exception as exc:
            log.warning("默认报价档位初始化失败：%s", exc)

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
