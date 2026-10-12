"""蒲公英达人搜索：把筛选条件翻译成平台请求，在页面上下文里调用。

请求体形状来自 M1 实测（`docs/evidence/bloggers-*/blogger-search.request.json`）。
**这是 `FilterSpec` → `platform_query` 的翻译层**（`docs/03` §5.1）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("pgy.pugongying.search")

SEARCH_PATH = "/api/solar/cooperator/blogger/v2"
KOL_PAGE = "https://pgy.xiaohongshu.com/solar/pre-trade/note/kol"


@dataclass
class SearchParams:
    """一次检索的条件。字段名尽量贴近业务语言，翻译在 `to_body()` 里。

    ⚠️ **尚未破译的参数（不要凭名字猜）**：
    - **粉丝量筛选**：不知道它编码在哪个字段。`fansNumUp` **不是**粉丝数下限 ——
      实测把它设成 10000 会让 total 直接从 5000 变成 0，而它旁边就有 `fansRiseNum`，
      多半是「粉丝**上升**数」。真正的粉丝量筛选参数待**驱动 UI 后抓包**确认。
      在那之前 `fans_num_up` 保持 0（不限），不要拿它当业务筛选用。
    - **一级/二级行业**：`firstIndustry`/`secondIndustry` 要的是**ID 不是名称**
      （母婴 = `698c373db570b54469f1b874`），枚举见 `/api/pgy/kol/get_industry_tag`。
    """

    first_industry: str = ""            # 一级行业 ID（不是名称！）
    second_industry: str = ""           # 二级行业 ID
    fans_num_up: int = 0                # ⚠️ 疑似「粉丝上升数」，**不是**粉丝量下限，保持 0
    note_type: int = 0                  # 0=不限 1=图文 2=视频（平台约定，待复核）
    gender: str | None = None           # 达人性别
    location: Any = None                # 达人地域
    fans_gender: int = 0                # 粉丝性别
    exclude_low_active: bool = False    # 剔除低活
    exclude_cooperated: bool = False    # 剔除已合作
    exclude_invited: bool = False       # 剔除已邀约
    flag_list: list[dict] = field(default_factory=list)
    sort_column: str = "comprehensiverank"
    sort: str = "desc"
    extras: dict = field(default_factory=dict)   # 平台新增维度透传

    def to_body(self, *, page_num: int = 1, page_size: int = 20) -> dict:
        """翻译成平台请求体。

        **未被本类显式建模的字段一律保留平台的默认值**——宁可少筛，不可错筛：
        传错一个参数可能让结果集完全不同，且很难发现。
        """
        body: dict[str, Any] = {
            "searchType": 1,
            "column": self.sort_column,
            "sort": self.sort,
            "pageNum": page_num,
            "pageSize": page_size,
            "trackId": f"kolMatch_{uuid.uuid4().hex}",
            "marketTarget": None,
            "audienceGroup": [],
            "personalTags": [],
            "gender": self.gender,
            "location": self.location,
            "signed": -1,
            "featureTags": [],
            "fansAge": 0,
            "fansGender": self.fans_gender,
            "fansLocation": None,
            "fansMaritalStatus": -1,
            "fansConsumptionLevel": -1,
            "fansChildAgeInfo": [],
            "fansDevicePrice": [],
            "fansDeviceBrand": [],
            "accumCommonImpMedinNum30d": [],
            "readMidNor30": [],
            "interMidNor30": [],
            "thousandLikePercent30": [],
            "noteType": self.note_type,
            "progressOrderCnt": [],
            "tradeType": "不限",
            "tradeReportBrandIdSet": [],
            "excludedTradeReportBrandId": False,
            "estimateCpuv30d": [],
            "inStar": 0,
            "firstIndustry": self.first_industry,
            "secondIndustry": self.second_industry,
            "newHighQuality": 0,
            "filterIntention": False,
            "flagList": self.flag_list or [
                {"flagType": "HAS_BRAND_COOP_BUYER_AUTH", "flagValue": "0"},
                {"flagType": "IS_HIGH_QUALITY", "flagValue": "0"},
            ],
            "activityCodes": [],
            "excludeLowActive": self.exclude_low_active,
            "fansNumUp": self.fans_num_up,   # ⚠️ 不是粉丝量下限，见类文档
            "excludedTradeReportBrand": self.exclude_cooperated,
            "excludedTradeInviteReportBrand": self.exclude_invited,
            "filterList": [],
            "contentSceneLabel": [],
        }
        body.update(self.extras)
        return body


_EVAL_FETCH = """
async ([path, body]) => {
  const resp = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    credentials: "include",
    body: JSON.stringify(body),
  });
  let payload = null;
  try { payload = await resp.json(); } catch (e) { payload = {_parseError: String(e)}; }
  return {status: resp.status, payload};
}
"""


def search_page(page, params: SearchParams, *, page_num: int = 1,
                page_size: int = 20) -> dict:
    """取一页达人。返回 `{status, payload}`；`payload.data.kols` 是达人列表。"""
    body = params.to_body(page_num=page_num, page_size=page_size)
    log.info("检索 page=%s size=%s 一级行业=%r",
             page_num, page_size, params.first_industry)
    result = page.evaluate(_EVAL_FETCH, [SEARCH_PATH, body])
    status = int(result.get("status") or 0)
    payload = result.get("payload") or {}
    if status != 200:
        raise RuntimeError(f"平台检索失败：HTTP {status}，msg={payload.get('msg')!r}")
    if payload.get("success") is False:
        raise RuntimeError(f"平台检索失败：code={payload.get('code')} msg={payload.get('msg')!r}")
    return payload


def search_all(page, params: SearchParams, *, per_tier: int = 20, key_of=None,
               tier_keys=None, keep=None, keep_all: bool = False, page_size: int = 20,
               max_pages: int = 60, stall_pages: int = 6, throttle=None,
               on_page=None, start_page: int = 1) -> tuple[list[dict], dict]:
    """翻页取数，**每个量级各凑 `per_tier` 条**（用户 2026-10-12 的要求）。

    Args:
        per_tier: 每个量级的目标条数。
        key_of: 由原始达人字典算出它的**量级归属**（如档位行序）。分配名额用。
        tier_keys: 全部量级的标识集合（判断"是否每个量级都凑够了"用）。
        keep: 是否有资格进名单（粉丝档位 + 预算两道关）。
        keep_all: True 时忽略 `per_tier` 与 `key_of`，只按总数取（排查/兼容用）。
        start_page: **从第几页开始翻**。默认 1＝从头取；>1 就是「换一批」，
            接着上次翻到的位置往下走（游标由 `selection_cursor` 维护）。

    **为什么要按量级分配而不是"取总数"**：总数取法下，高量级容易把名额占满，
    低量级就分不到人 —— 而用户要的是每个量级都有名单。

    **为什么需要 `start_page`**：排序是平台写死的（`comprehensiverank` 降序），
    所以总是从第 1 页取的话，同一组条件每次都给同一批人，`total=5000` 的池子里
    永远只看得到最前面那一小截。

    **高量级凑不满怎么办**：超头部本来人就少，可能翻到底也只有几条。
    所以设了「连续 `stall_pages` 页没有任何量级新增就停」，**如实交少**，
    而不是为了凑数无限翻页（那既慢又容易被风控盯上）。

    返回 `(达人列表, 统计)`；统计里带**每个量级实际取到几条**。
    **每一次翻页都过 throttle** —— 节流是功能需求不是优化项（`CLAUDE.md` 第三条）。
    """
    collected: list[dict] = []
    counts: dict = {}
    all_keys = list(tier_keys or [])
    total = 0
    scanned = 0
    first_page = max(1, int(start_page))
    # `max_pages` 是**本次允许翻的页数**，不是绝对页码上限 ——
    # 否则接着上次翻时，页码一早超过上限就一页都取不到了。
    last_page = first_page + max(0, max_pages - 1)
    page_num = first_page
    stall = 0

    def satisfied() -> bool:
        """每个量级都凑够了才算满足。`tier_keys` 没给就永远不满足（只受页数/停滞兜底）。"""
        if keep_all or not all_keys:
            return False
        return all(counts.get(k, 0) >= per_tier for k in all_keys)

    # 本次实际翻过的页数；以及「下次该从第几页开始」——
    # 后者是「换一批」的全部依据，所以每个 break 点都要**显式**给出，不能靠推。
    pages_read = 0
    next_page = first_page
    reached_end = False          # 真的翻到底了（后面没数据了），与"没凑满名额"无关

    while page_num <= last_page:
        payload = search_page(page, params, page_num=page_num, page_size=page_size)
        data = payload.get("data") or {}
        kols = data.get("kols") or []
        total = data.get("total") or total
        if not kols:
            reached_end = True
            break
        pages_read += 1
        scanned += len(kols)

        gained = 0
        for item in kols:
            if keep is not None and not keep(item):
                continue
            if keep_all:
                collected.append(item)
                gained += 1
                continue
            key = key_of(item) if key_of else 0
            if counts.get(key, 0) >= per_tier:
                continue          # 该量级已满，后面同量级的就不要了
            collected.append(item)
            counts[key] = counts.get(key, 0) + 1
            gained += 1

        log.info("第 %s 页：扫描 %s 条，本页新增 %s，累计 %s（平台 total=%s）",
                 page_num, len(kols), gained, len(collected), total)
        if on_page is not None:
            on_page(page_num, len(collected), total, dict(counts))

        # 这一页读完了：默认下次从下一页接着翻，各 break 分支再按情况覆盖。
        next_page = page_num + 1
        if not keep_all and satisfied():
            break
        stall = stall + 1 if gained == 0 else 0
        if stall >= stall_pages:
            log.info("连续 %s 页无新增，停止翻页（高量级可能本就稀少）", stall_pages)
            break
        if len(kols) < page_size:
            reached_end = True    # 平台给的不足一页＝这是最后一页
            break
        page_num += 1
        if throttle is not None:
            throttle()
    else:
        log.info("本次已翻满 %s 页上限，后面还有数据；「换一批」可继续", max_pages)

    stats = {
        "pages": pages_read,
        "page_limit": max_pages,     # 界面据此说明"本批是到上限了、还是平台到底了"
        "start_page": first_page,
        "next_page": next_page,
        "reached_end": reached_end,
        "total": total,
        "scanned": scanned,
        "collected": len(collected),
        "per_tier_target": per_tier,
        # 按量级报数（顺序与报价表一致）—— 用户要的是"每个量级多少人"，
        # 所以统计也得按量级给，而不是只给一个总数。
        "per_tier_counts": {
            key: counts.get(key, 0)
            for key in (all_keys or sorted(counts, key=str))
        },
        "exhausted": not keep_all and not satisfied(),
    }
    return collected, stats
