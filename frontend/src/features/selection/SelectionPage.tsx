import { useCallback, useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  AlertCircle, CheckCircle2, Download, FolderOpen, Loader2, MapPin, Play, RefreshCw,
  RotateCcw,
} from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Dialog } from "../../components/ui/dialog";
import {
  api,
  type AreaNode,
  type BloggerRow,
  type SelectionRunResponse,
  type SelectionStatus,
} from "../../lib/api";
import { PricingTiersCard, type TierSetting } from "./PricingTiersCard";
import { ProgressPanel } from "./ProgressPanel";
import { RegionPicker } from "./RegionPicker";

/**
 * 选号页。业务链路见 `docs/05-业务规则与交付物规格.md`：
 * **给条件 → 检索 → 按客户表头导出 Excel**，就这一件事。
 *
 * 平台侧**只读**：本页只做检索与导出，不发起任何投放动作。
 * 输出目录、运行日志这些"一次配好"的东西在「设置」页。
 */
// 与后端 `fields.COLUMNS` 逐字一致（客户最新模板，2026-11-10；11-16 加「小红书号」）。
const COLUMNS = [
  "昵称",
  "小红书号",
  "粉丝量（w）",
  "赞藏（w）",
  "类型",
  "ip",
  "图文报价",
  "视频报价",
  "主页链接（非蒲公英链接）",
  "主页近10篇内容的点赞中位数",
];

const NUMERIC = new Set([
  "粉丝量（w）", "赞藏（w）", "图文报价", "视频报价", "主页近10篇内容的点赞中位数",
]);

/** 行上的内部标记（不进交付表，只用来加底色）。 */
interface RowFlags {
  over_budget?: string[];
  no_quote?: string[];
}

function flagsOf(row: BloggerRow): RowFlags {
  return ((row as Record<string, unknown>).__flags__ as RowFlags | undefined) ?? {};
}

function overBudget(row: BloggerRow): Set<string> {
  return new Set(flagsOf(row).over_budget ?? []);
}

/**
 * 平台**没有这个报价**的形态。交付表里那格写英文 `None`（用户 2026-10-13 明确要的写法），
 * 标注与超预算**完全一致**（同底色同名色号字体）—— 那个 `None` 不是调试残留，别改。
 */
function noQuote(row: BloggerRow): Set<string> {
  return new Set(flagsOf(row).no_quote ?? []);
}

function cellText(value: BloggerRow[string]): string {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * 把输入框里的一串检索词拆成数组。
 *
 * 分隔符：逗号类（中英文逗号、顿号、分号、换行）、**连续两个以上的空格**、
 * 以及**全角空格**。
 *
 * ⚠️ **单个半角空格不算分隔符**：关键词本身可能带空格（如 `生活 vlog`），
 * 按空格一味地拆会把它切成两半（拿半个词去搜，结果也不是你要的）。
 * 代价是"用一个空格隔开两个词"会被当成**一个**词 —— 所以两个以上空格才算分隔，
 * 界面上也写明了用逗号分隔。
 * （2026-10-13 实测踩到：`亲子达人  抽象` 被当成一个词搜，白丢一个关键词的检索机会。）
 */
function parseTerms(text: string): string[] {
  return text
    .split(/[,，、;；\n]+|[ \t]{2,}|\u3000+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 登录态是多久以前查的。刚查过就不打扰；久了就如实标一句。 */
function staleLoginNote(ageSeconds: number | null | undefined) {
  if (ageSeconds === null || ageSeconds === undefined) return null;   // 还没查到过
  if (ageSeconds < 60) return null;
  const minutes = Math.floor(ageSeconds / 60);
  return (
    <span className="text-[11px] text-text-tertiary">
      （{minutes >= 60 ? `${Math.floor(minutes / 60)} 小时` : `${minutes} 分钟`}前查的）
    </span>
  );
}

export function SelectionPage() {
  const [status, setStatus] = useState<SelectionStatus | null>(null);
  //: 勾选的量级 + 每档条数。默认全选、每档 20 条；每次检索都可能改，不持久化。
  const [tiers, setTiers] = useState<TierSetting[]>([]);
  //: 勾选的地域路径（如 `中国 安徽 合肥 蜀山区`，多选是「或」）。空 = 不限。
  const [regions, setRegions] = useState<string[]>([]);
  //: 地域选择弹层（照网页版：点开四列选择，重置/确定在底部）。
  const [regionOpen, setRegionOpen] = useState(false);
  const [areas, setAreas] = useState<AreaNode[]>([]);
  const [areasSource, setAreasSource] = useState<"live" | "cache" | "">("");
  const [loadingAreas, setLoadingAreas] = useState(false);
  const [areaMessage, setAreaMessage] = useState("");
  const [termText, setTermText] = useState("");
  const [running, setRunning] = useState(false);
  const [runStartedAt, setRunStartedAt] = useState(0);
  const [result, setResult] = useState<SelectionRunResponse | null>(null);
  const [error, setError] = useState("");
  const [exportDir, setExportDir] = useState("");

  // 检索期间每 1 秒拉一次进度：同步接口那几分钟里，这是界面唯一的中间状态来源。
  // 停下来之后再补拉一次，好把「停在哪一步」留在屏幕上。
  const progress = useQuery({
    queryKey: ["selection-progress"],
    queryFn: api.selectionProgress,
    refetchInterval: running ? 1_000 : false,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const refreshStatus = useCallback(async () => {
    try {
      const data = await api.selectionStatus();
      setStatus(data);
      // 档位清单由后端给（名称/区间与 `fields.TIERS` 同源）。
      // **保留用户已经改过的勾选与条数**，只补新出现的档位；默认全选、20 条。
      const incoming = data.tiers ?? [];
      if (incoming.length > 0) {
        setTiers((prev) => incoming.map((tier) => (
          prev.find((item) => item.key === tier.key)
          ?? { key: tier.key, enabled: true, count: 20 }
        )));
      }
      return data;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
  }, []);

  // 地域枚举：能活取就活取，取不到用上次快照。它不挡检索 ——
  // 实在拿不到就让用户先不选地域，不能因为一个下拉把整页卡死。
  const loadAreas = useCallback(async () => {
    setLoadingAreas(true);
    setAreaMessage("");
    try {
      const data = await api.selectionAreas();
      setAreas(data.areas ?? []);
      setAreasSource(data.source);
    } catch (err) {
      setAreas([]);
      setAreasSource("");
      setAreaMessage((err as Error).message);
    } finally {
      setLoadingAreas(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await refreshStatus();
      void loadAreas();
      try {
        setExportDir((await api.getSettings()).resolved_export_dir);
      } catch {
        /* 设置读不到不挡检索，导出时后端还会再报一次 */
      }
    })();
  }, [refreshStatus, loadAreas]);

  const enabledTiers = tiers.filter((tier) => tier.enabled);

  /**
   * `next`（默认）= 接着这组条件**上次翻到的位置**往下取，拿到的是没看过的新人；
   * `fresh` = **显式**从第 1 页起（同一组条件必然给同一批人，平台排序是写死的）。
   *
   * 为什么默认是 `next`（用户 2026-10-13）：平台排序写死 ⇒ 每次都从头取＝每次发同一批，
   * 池子里剩下的永远不可知。要"再看一遍第 1 页"就用「从头开始」。
   */
  const run = async (mode: "fresh" | "next" = "next") => {
    setError("");
    if (enabledTiers.length === 0) {
      setError("至少要勾选一个量级 —— 一个都不选就没有要检索的人了。");
      return;
    }
    setRunning(true);
    setResult(null);
    setRunStartedAt(Date.now());
    try {
      setResult(await api.selectionRun({
        tier_counts: Object.fromEntries(
          enabledTiers.map((tier) => [tier.key, tier.count])),
        enabled_tiers: enabledTiers.map((tier) => tier.key),
        regions,
        mode,
        terms: parseTerms(termText),
      }));
      // 检索会顺带把登录做完，回来刷新一下状态。
      void refreshStatus();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunning(false);
      // 补拉一次：把最终的 done / 中断在哪一步 留在屏幕上。
      void progress.refetch();
    }
  };

  const openExportDir = async () => {
    // 优先用**本次产物实际所在的目录**，而不是设置里配的那个：
    // 设置里的目录不可用时后端会回落到默认目录，两者可能不是同一个。
    const target = result?.export.dir || exportDir;
    if (!target) return;
    if (!isTauri()) {
      setError(`导出目录：${target}（当前不是桌面客户端环境，无法直接打开）`);
      return;
    }
    try {
      await invoke<string>("open_path", { path: target });
    } catch (err) {
      setError(String(err));
    }
  };

  const markedCount = result
    ? result.rows.filter((row) => overBudget(row).size > 0 || noQuote(row).size > 0).length
    : 0;

  /** 哪些量级动用了「不带检索词」的兜底、各补了几条。这些**不保证命中检索词**，必须当面说。 */
  const fallbackTiers = Object.entries(
    (result?.stats?.keyword_fallback as Record<string, number> | undefined) ?? {},
  );
  const hasFallback = fallbackTiers.length > 0;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>蒲公英达人选号</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm leading-relaxed text-text-secondary">
            按条件在蒲公英检索达人，采集信息后按客户提报表格式导出 Excel。
            <span className="text-text-tertiary">本工具对平台只读，不涉及任何投放动作。</span>
          </p>

          <div className="flex flex-wrap items-center gap-3 text-sm">
            {status && (
              <>
                <span className={status.has_credentials
                  ? "flex items-center gap-1.5 text-success"
                  : "flex items-center gap-1.5 text-warning"}>
                  {status.has_credentials ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                  凭据：{status.has_credentials ? "已配置" : "未配置"}
                </span>
                <span className={status.logged_in
                  ? "flex items-center gap-1.5 text-success"
                  : "flex items-center gap-1.5 text-text-secondary"}>
                  {status.logged_in ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                  平台登录：{status.logged_in ? "已登录" : "未登录"}
                  {/* 检索跑着的时候服务不会再往浏览器里挤，所以这个值可能是旧快照 ——
                      如实标出来，免得用户拿一个几分钟前的状态做判断。 */}
                  {staleLoginNote(status.login_checked_age_s)}
                </span>
                <span className={status.pricing_configured
                  ? "flex items-center gap-1.5 text-success"
                  : "flex items-center gap-1.5 text-warning"}>
                  {status.pricing_configured ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                  报价档位：{status.pricing_configured
                    ? `已配置（上限 × ${status.pricing_multiplier ?? "?"}）`
                    : "未配置"}
                </span>
              </>
            )}
            {/* 检索期间禁用：查状态要走浏览器，而浏览器正被检索占着，
                点了只会堵到超时、弹一句看不懂的错（这就是那个 bug 的来源之一）。 */}
            <Button
              variant="secondary"
              className="h-8 px-3 text-xs"
              onClick={() => void refreshStatus()}
              disabled={running}
              title={running ? "检索进行中，完成后会自动刷新状态" : undefined}
            >
              <RefreshCw className="h-3.5 w-3.5" /> 刷新
            </Button>
            {status && !status.has_credentials && (
              <Link to="/accounts" className="text-xs text-primary underline">
                先去「账户」页添加账号
              </Link>
            )}
          </div>

          {/* 用户 2026-11-19：生成的 Excel 去哪、怎么打开，要在选号页直接看得到。
              没跑过时打开设置里的输出目录；跑完后打开**本次产物**所在目录。 */}
          <div className="flex flex-wrap items-center gap-3 rounded-control border border-border bg-surface-secondary px-3 py-2 text-sm text-text-secondary">
            <span className="font-medium text-text-tertiary">Excel 输出到：</span>
            <span className="break-all font-mono text-xs text-text-primary">
              {result?.export.path || exportDir || "（正在读取设置）"}
            </span>
            <Button
              variant="secondary"
              className="h-9 px-4 text-sm"
              onClick={() => void openExportDir()}
              disabled={!result && !exportDir}
            >
              <FolderOpen className="h-4 w-4" /> 打开所在文件夹
            </Button>
          </div>

          {/* 没配报价档位时，预算把关是关着的 —— 这会直接影响交付物对不对，必须说出来。
              以前这件事只写在 Excel 的「元信息」表里，那张表已按用户要求删掉。 */}
          {status && !status.pricing_configured && (
            <div className="rounded-control border border-warning/40 bg-warning/5 px-3 py-2 text-xs leading-relaxed text-text-secondary">
              <b className="text-warning">报价档位还没配置 —— 预算把关目前是关着的。</b>
              也就是说<b>超出报价上限的达人会照样进名单</b>。请到下面
              <b>「报价档位、预算与每档条数」</b>表里填写并保存
              （第一次打开会自动带出开发默认值，点保存即生效）。
            </div>
          )}
        </CardContent>
      </Card>

      {/* 报价档位、预算与每档条数：整体从「设置」搬到这里，一打开就能看到。
          用户 2026-11-19：价格/倍数仍是长期配置（保存到 pricing.json），
          勾选与每档条数只对本次检索生效。 */}
      <PricingTiersCard
        tierSettings={tiers}
        onTierSettingsChange={setTiers}
        disabled={running}
        onSaved={() => void refreshStatus()}
      />

      {/* 地域：照网页版做成「选地域」弹层（四列 + 底部重置/确定），
          不再把四列直接铺在页面上 —— 用户 2026-11-19 嫌框太丑、大小不对。 */}
      <Card>
        <CardHeader>
          <CardTitle>地域（可多选）</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-text-secondary">
              已选 {regions.length} 个{regions.length === 0 ? "（不限）" : ""}
            </span>
            <Button
              className="h-9 px-4 text-sm"
              onClick={() => setRegionOpen(true)}
              disabled={running}
            >
              <MapPin className="h-4 w-4" /> 选择地域
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs leading-relaxed text-text-secondary">
            和网页版一样：<b>国家 → 省 → 市 → 区</b>四列。勾父级会
            <b>级联勾上它下面所有项</b>；取消一个子项，父级变成部分选中。
            每列顶部的<b>「全部」是本列全选/全不选</b>。
            <b>勾中的地域都会检索</b>（多选是「或」），不勾 = 不限。
          </p>

          {regions.length === 0 ? (
            <p className="text-xs text-text-tertiary">未选择地域 = 不限。</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {regions.map((path) => (
                <button
                  key={path}
                  type="button"
                  className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-secondary px-2 py-0.5 text-[11px] text-text-secondary hover:text-text-primary disabled:opacity-50"
                  disabled={running}
                  onClick={() => setRegions((prev) => prev.filter((item) => item !== path))}
                  title={path}
                >
                  {path.split(" ").pop()} <span aria-hidden="true">×</span>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={regionOpen}
        onClose={() => setRegionOpen(false)}
        title="地域"
        size="region"
        bodyClassName="p-0"
        footer={
          <div className="flex w-full items-center justify-between">
            <Button
              variant="secondary"
              onClick={() => setRegions([])}
              disabled={running || regions.length === 0}
            >
              重置
            </Button>
            <Button onClick={() => setRegionOpen(false)}>确定</Button>
          </div>
        }
      >
        {loadingAreas && (
          <p className="flex items-center gap-2 px-4 py-8 text-sm text-text-secondary">
            <Loader2 className="h-4 w-4 animate-spin" /> 正在加载地域清单…
          </p>
        )}
        {!loadingAreas && areas.length === 0 && (
          <div className="flex flex-wrap items-center gap-3 px-4 py-8 text-sm">
            <span className="text-text-tertiary">
              {areaMessage || "还没有拿到地域清单（不影响检索，不勾地域即可）。"}
            </span>
            <Button variant="secondary" className="h-8 px-3 text-xs"
                    onClick={() => void loadAreas()}>
              重试加载
            </Button>
            {status && !status.logged_in && (
              <Link to="/accounts" className="text-primary underline">
                去「账户」页登录
              </Link>
            )}
          </div>
        )}
        {!loadingAreas && areas.length > 0 && (
          <div className="space-y-2">
            <RegionPicker
              areas={areas}
              selected={regions}
              onChange={setRegions}
              disabled={running}
            />
            {areasSource === "cache" && (
              <p className="px-3 pb-2 text-[11px] text-text-tertiary">
                地域清单是<b>上次存下来的快照</b>（这次没能连上平台）。去「账户」页登录后重试加载可刷新。
              </p>
            )}
          </div>
        )}
      </Dialog>

      <Card>
        <CardHeader>
          <CardTitle>检索条件</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex min-w-[320px] flex-1 flex-col gap-1.5 text-xs text-text-secondary">
              检索词（逗号或两个空格分隔，可留空）
              <input
                className="h-9 w-full rounded-control border border-border bg-surface px-3 text-sm text-text-primary"
                value={termText}
                placeholder="例如：体验, 亲子, 抽象, 戏精, cos, vlog, 活人感"
                onChange={(event) => setTermText(event.target.value)}
              />
            </label>

            <Button
              onClick={() => void run("next")}
              disabled={running || enabledTiers.length === 0}
              title={enabledTiers.length === 0 ? "先在上面至少勾选一个量级" : undefined}
            >
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {running ? "检索中…" : "开始检索"}
            </Button>
            {/* 「从头开始」是**显式**的重来 —— 同一组条件给的是同一批人（平台排序写死），
                所以它平时用不到，留着只是"我就想再看一遍第 1 页那批人"。 */}
            <Button
              variant="secondary"
              onClick={() => void run("fresh")}
              disabled={running || enabledTiers.length === 0}
              title={enabledTiers.length === 0
                ? "先在上面至少勾选一个量级"
                : "从第 1 页重新取 —— 同一组条件给的是同一批人（平台排序是写死的）"}
            >
              <RotateCcw className="h-4 w-4" /> 从头开始
            </Button>
          </div>

          <p className="text-xs leading-relaxed text-text-tertiary">
            一共 <b>6 个量级</b>，<b>只检索上面勾选的那些</b>，每档各取它自己的条数，
            量级高的排在前面（交付表本身不输出「量级」列）：
            超头部KOL（40w–50w / 30w–40w）、头部KOL（20w–30w / 10w–20w）、
            腰部KOL（5w–10w）、尾部KOL（1w–5w）。
            <br />
            <b>地域</b>是网页版同款的国家/省/市/区四列：
            <b>勾父级会级联勾上它下面所有项</b>；每列「全部」是本列全选/全不选。
            <b>勾中的地域都会检索</b>（多选是「或」），不勾 = 不限。
            <br />
            只收录粉丝 1w–50w、且图文或视频任一在预算内的达人；两个报价都超的会被剔除。
            交付表按客户最新模板输出 10 列：昵称、小红书号、粉丝量/赞藏（w）、类型、ip、
            图文/视频报价、小红书主页链接，以及<b>近 10 篇内容的点赞中位数</b>。
            「类型」会列出该达人的<b>全部</b>内容类目 + 特征标签（如 AvaFoo = 影视综资讯 时尚 ootd 测评）。
            最后两列会在检索完成后逐个补充：浏览器会一个个打开<b>蒲公英详情页</b>，
            读取「日常笔记 / 笔记案例」里最新 10 篇的点赞数（<b>不做额外等待、直接下一个</b>；
            3 天内算过的人走本地缓存）。全部在蒲公英域内完成，<b>不访问小红书、也不需要扫码</b>。
            遇到安全验证会停下来等你处理，不要手动刷新。
            <br />
            会一直翻到<b>每个勾选的量级都凑够</b>为止，可能要几分钟（采集有节流，宁可慢不触发风控）。
            <br />
            某个量级光靠检索词凑不满时（比如 40w–50w 挂上词以后只剩几十人），会
            <b>丢开检索词、按这一档的粉丝区间再翻一遍</b>把这一档补满 ——
            ⚠️ <b>这样补进来的人不保证命中你的检索词</b>（哪档补了几条记在运行日志里）。
            确实一整档都没更多人时，才<b>如实交少</b>。
            <br />
            检索词用<b>逗号</b>或<b>两个空格</b>分隔（单个空格算词的一部分，
            `生活 vlog` 这种不会被拆开）。词里 <b>哪个是行业、哪个是笔记关键词，程序自己判断</b>
            （你只管把词丢进来）。命中<b>任意一个</b>即算；一个人命中多个也<b>不会因此排在前面</b>
            —— 先后仍然只看粉丝量和报价。
            <br />
            同一组条件<b>再点一次「开始检索」给的是没看过的人</b>（接着上次翻到的位置往下取）——
            平台排序是写死的，每次都从头取的话，池子里剩下的永远轮不到。
            想看第 1 页那批人就点<b>「从头开始」</b>。
          </p>

          <ProgressPanel progress={progress.data} startedAtMs={runStartedAt} />

          {running && (
            <p className="text-xs text-text-secondary">
              正在按节流节奏翻页（默认每页间隔 1.5 秒，宁可慢，不触发风控）。请勿关闭窗口。
            </p>
          )}
          {error && (
            <p className="rounded-control border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</p>
          )}
        </CardContent>
      </Card>

      {result && (
        <Card>
          <CardHeader>
            <CardTitle>
              结果（共 {result.rows.length} 条 · 平台共 {result.stats?.total ?? "?"} 条 · 翻 {result.stats?.pages ?? "?"} 页
              {markedCount > 0 ? ` · ${markedCount} 条有一项报价超预算` : ""}）
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {/* Excel 在检索完成时就已经写到磁盘上了 —— 这里的主动作是"去把它打开"，
                不是"下载"。 */}
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-3">
                <Button className="h-9 px-4" onClick={() => void openExportDir()}>
                  <FolderOpen className="h-4 w-4" /> 打开所在文件夹
                </Button>
                {/* 只在开发态（浏览器里跑 Vite）留下载：那里调不了 Tauri 命令打开目录。
                    打包后的客户端点了没用 —— 没有注册 on_download，下载会被直接丢弃。 */}
                {!isTauri() && (
                  <a
                    className="inline-flex h-9 items-center gap-2 rounded-control border border-border bg-surface px-4 text-sm font-medium text-text-primary hover:bg-surface-hover"
                    href={api.artifactUrl(result.export.name)}
                    download={result.export.name}
                  >
                    <Download className="h-4 w-4" /> 下载 Excel（开发态）
                  </a>
                )}
              </div>
              <p className="break-all text-xs text-text-tertiary">
                已保存到 <span className="font-mono">{result.export.path}</span>
              </p>
            </div>

            {/* 说清"这一批是从哪儿取的、下一批从哪儿开始" —— 否则用户会以为
                每次跑出来都不一样/一样是程序坏了（平台排序写死，从头取当然一样）。 */}
            <p className="text-xs text-text-tertiary">
              本批共翻 {result.stats?.pages ?? "?"} 页。
              {result.stats?.reached_end
                ? " 平台这边已经翻到底了，没有更多新人了 —— 再点「开始检索」会回到第 1 页。"
                : result.stats?.stopped_by_page_limit
                  ? <> 有 <b>{result.stats?.keywords_cut_by_limit ?? "?"}</b> 个「量级 × 检索词」组合翻到了页数上限
                    —— 后面还有，再点<b>「开始检索」</b>接着往下取新人。</>
                  : <> 部分量级已经没有更多结果了。再点<b>「开始检索」</b>会给没看过的下一批人。</>}
            </p>

            {/* 兜底必须**当面说清**：这些人是丢开检索词补进来的，不保证命中你输入的词。
                不说的话，用户会以为名单里每个人都命中了检索词（交付物里也看不出来）。 */}
            {hasFallback && (
              <p className="rounded-control border border-warning/40 bg-warning/5 px-3 py-2 text-xs leading-relaxed text-text-secondary">
                <b className="text-warning">有 {fallbackTiers.length} 个量级是丢掉检索词补满的</b>
                （{fallbackTiers.map(([tier, n]) => `${tier} 补 ${n} 条`).join("；")}）。
                <b>这些人不保证命中你输入的检索词</b> —— 是为了让每个量级都凑够才补的。
              </p>
            )}

            {/* 各量级实际取到几条。
                **不再逐档标"没凑满"**：单次只翻 15 页，翻不满是常态而不是异常，
                全刷成橙色只会变成一堵噪音墙。够不够、要不要继续，交给下面的
                「换一批」提示统一说。 */}
            {result.stats?.per_tier_counts && (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                {Object.entries(result.stats.per_tier_counts).map(([tier, count]) => (
                  <span key={tier}>{tier} <b>{count}</b> 条</span>
                ))}
              </div>
            )}

            {markedCount > 0 && (
              <p className="rounded-control border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-text-secondary">
                底色标出的单元格有两种：<b>超出该粉丝档位上限</b>的报价（按规则仍保留，
                因为图文、视频有一项在预算内），以及<b>平台根本没有这个报价</b>的形态
                —— 后者交付表里写的是 <b>None</b>，标注与超预算完全相同。共 {markedCount} 条。
              </p>
            )}

            <div className="overflow-x-auto rounded-control border border-border">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-surface-secondary text-left text-xs text-text-secondary">
                    {COLUMNS.map((column) => (
                      <th key={column} className="whitespace-nowrap px-3 py-2 font-medium">{column}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, index) => {
                    const over = overBudget(row);
                    const none = noQuote(row);
                    return (
                      <tr key={index} className="border-t border-border hover:bg-surface-hover">
                        {COLUMNS.map((column) => (
                          <td
                            key={column}
                            className={[
                              "whitespace-nowrap px-3 py-2",
                              NUMERIC.has(column) ? "text-right tabular-nums" : "",
                              over.has(column) || none.has(column)
                                ? "bg-warning/20 text-text-primary"
                                : "text-text-primary",
                              over.has(column) ? "font-semibold" : "",
                            ].join(" ")}
                            title={`${cellText(row[column])}${
                              over.has(column)
                                ? "（超出该档位上限）"
                                : none.has(column)
                                  ? "（平台没有这一形态的报价）"
                                  : ""
                            }`}
                          >
                            {column === "主页链接（非蒲公英链接）" && row[column] ? (
                              <a className="text-primary hover:underline" href={String(row[column])} target="_blank" rel="noreferrer">
                                主页
                              </a>
                            ) : (
                              cellText(row[column])
                            )}
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
