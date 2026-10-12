import { useCallback, useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { invoke, isTauri } from "@tauri-apps/api/core";
import {
  AlertCircle, CheckCircle2, Download, FolderOpen, Loader2, Play, RefreshCw,
} from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import {
  api,
  type BloggerRow,
  type IndustryNode,
  type SelectionRunResponse,
  type SelectionStatus,
} from "../../lib/api";
import { ProgressPanel } from "./ProgressPanel";

/**
 * 选号页。业务链路见 `docs/05-业务规则与交付物规格.md`：
 * **给条件 → 检索 → 按客户表头导出 Excel**，就这一件事。
 *
 * 平台侧**只读**：本页只做检索与导出，不发起任何投放动作。
 * 输出目录、运行日志这些"一次配好"的东西在「设置」页。
 */
const COLUMNS = [
  "量级", "序号", "昵称", "粉丝量", "主页链接",
  "IP", "类型", "曝光中位数", "互动中位数", "图文", "视频",
];

const NUMERIC = new Set(["序号", "粉丝量", "曝光中位数", "互动中位数", "图文", "视频"]);

/** 行上的内部标记（不进交付表，只用来加底色）。 */
interface RowFlags {
  over_budget?: string[];
}

function overBudget(row: BloggerRow): Set<string> {
  const flags = (row as Record<string, unknown>).__flags__ as RowFlags | undefined;
  return new Set(flags?.over_budget ?? []);
}

function cellText(value: BloggerRow[string]): string {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * 把输入框里的一串关键词拆成数组。
 *
 * 只按**逗号类分隔符**拆（中英文逗号、顿号、分号、换行），**不拆空格** ——
 * 关键词本身可能带空格（如 `生活 vlog`），按空格拆会把它切成两半。
 */
function parseKeywords(text: string): string[] {
  return text
    .split(/[,，、;；\n]+/)
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
  const [industries, setIndustries] = useState<IndustryNode[]>([]);
  const [industriesSource, setIndustriesSource] = useState<"live" | "cache" | "">("");
  const [loadingIndustries, setLoadingIndustries] = useState(false);
  const [firstIndustry, setFirstIndustry] = useState("");
  const [secondIndustry, setSecondIndustry] = useState("");
  const [perTier, setPerTier] = useState(20);
  const [keywordText, setKeywordText] = useState("");
  const [running, setRunning] = useState(false);
  const [runStartedAt, setRunStartedAt] = useState(0);
  const [result, setResult] = useState<SelectionRunResponse | null>(null);
  const [error, setError] = useState("");
  const [exportDir, setExportDir] = useState("");
  const [industryMessage, setIndustryMessage] = useState("");

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
      return data;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
  }, []);

  // 行业枚举只能在有登录态时拿到（平台接口）。拿不到就用上次的快照；
  // 连快照都没有，就明确告诉用户"去账户页登录"，而不是摆一个空的空下拉。
  const loadIndustries = useCallback(async () => {
    setLoadingIndustries(true);
    setIndustryMessage("");
    try {
      const data = await api.selectionIndustries();
      setIndustries(data.industries ?? []);
      setIndustriesSource(data.source);
    } catch (err) {
      setIndustries([]);
      setIndustriesSource("");
      setIndustryMessage((err as Error).message);
    } finally {
      setLoadingIndustries(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      const data = await refreshStatus();
      if (data?.logged_in) await loadIndustries();
      try {
        setExportDir((await api.getSettings()).resolved_export_dir);
      } catch {
        /* 设置读不到不挡检索，导出时后端还会再报一次 */
      }
    })();
  }, [refreshStatus, loadIndustries]);

  /**
   * `fresh` = 从头取（同一组条件给同一批人，平台排序是写死的）；
   * `next` = 换一批，接着这组条件上次翻到的位置往下取真正没看过的人。
   */
  const run = async (mode: "fresh" | "next" = "fresh") => {
    setError("");
    setRunning(true);
    setResult(null);
    setRunStartedAt(Date.now());
    try {
      setResult(await api.selectionRun({
        first_industry: firstIndustry,
        second_industry: secondIndustry,
        per_tier: perTier,
        mode,
        keywords: parseKeywords(keywordText),
      }));
      // 检索会顺带把登录做完，回来刷新一下状态与行业清单。
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

  const parsedKeywords = parseKeywords(keywordText);
  const children = industries.find((item) => item.value === firstIndustry)?.children ?? [];
  const markedCount = result
    ? result.rows.filter((row) => overBudget(row).size > 0).length
    : 0;

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

          {/* 没配报价档位时，预算把关是关着的 —— 这会直接影响交付物对不对，必须说出来。
              以前这件事只写在 Excel 的「元信息」表里，那张表已按用户要求删掉。 */}
          {status && !status.pricing_configured && (
            <div className="rounded-control border border-warning/40 bg-warning/5 px-3 py-2 text-xs leading-relaxed text-text-secondary">
              <b className="text-warning">报价档位还没配置 —— 预算把关目前是关着的。</b>
              也就是说<b>超出报价上限的达人会照样进名单</b>。请把档位表放到
              <span className="font-mono"> pricing.json</span>
              （或开发期的 <span className="font-mono">资料和说明\报价档位.json</span>）后刷新。
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>检索条件</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1.5 text-xs text-text-secondary">
              一级行业
              <select
                className="h-9 min-w-[180px] rounded-control border border-border bg-surface px-3 text-sm text-text-primary"
                value={firstIndustry}
                onChange={(event) => { setFirstIndustry(event.target.value); setSecondIndustry(""); }}
              >
                <option value="">不限</option>
                {industries.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1.5 text-xs text-text-secondary">
              二级行业
              <select
                className="h-9 min-w-[180px] rounded-control border border-border bg-surface px-3 text-sm text-text-primary disabled:opacity-50"
                value={secondIndustry}
                disabled={children.length === 0}
                onChange={(event) => setSecondIndustry(event.target.value)}
              >
                <option value="">不限</option>
                {children.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>

            <label className="flex min-w-[320px] flex-1 flex-col gap-1.5 text-xs text-text-secondary">
              笔记关键词（命中任意一个即算；逗号分隔，可留空）
              <input
                className="h-9 w-full rounded-control border border-border bg-surface px-3 text-sm text-text-primary"
                value={keywordText}
                placeholder="例如：体验, 亲子, 抽象, 戏精, cos, vlog, 活人感"
                onChange={(event) => setKeywordText(event.target.value)}
              />
            </label>

            <label className="flex flex-col gap-1.5 text-xs text-text-secondary">
              每个量级取多少条
              <input
                type="number" min={1} max={500}
                className="h-9 w-[130px] rounded-control border border-border bg-surface px-3 text-sm text-text-primary"
                value={perTier}
                onChange={(event) => setPerTier(Math.max(1, Math.min(500, Number(event.target.value) || 1)))}
              />
            </label>

            <Button onClick={() => void run("fresh")} disabled={running}>
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {running ? "检索中…" : "开始检索"}
            </Button>
            {/* 「换一批」只在已经取过之后才有意义：它接着上次翻到的位置往下走。 */}
            {result && !running && (
              <Button
                variant="secondary"
                onClick={() => void run("next")}
                disabled={result.stats?.reached_end === true}
                title={result.stats?.reached_end
                  ? "平台这边已经翻到底了，没有更多新人了"
                  : "接着上次翻到的位置往下取没看过的人"}
              >
                <RefreshCw className="h-4 w-4" /> 换一批
              </Button>
            )}
          </div>

          {/* 把拆出来的关键词摆出来：分隔符写错了（比如用空格分隔）能当场看见 */}
          {parsedKeywords.length > 0 && (
            <p className="flex flex-wrap items-center gap-1.5 text-xs text-text-tertiary">
              按 <b>{parsedKeywords.length}</b> 个关键词分别检索、结果合并去重（命中任意一个即算）：
              {parsedKeywords.map((item) => (
                <span key={item}
                      className="rounded-pill bg-surface-secondary px-2 py-0.5 text-text-secondary">
                  {item}
                </span>
              ))}
            </p>
          )}

          {/* 行业清单的状态：空着的时候必须说清"为什么空、下一步做什么" */}
          {loadingIndustries && (
            <p className="flex items-center gap-2 text-xs text-text-secondary">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> 正在加载行业清单…
            </p>
          )}
          {!loadingIndustries && industries.length === 0 && (
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <span className="text-text-tertiary">
                {industryMessage || "还没有拿到行业清单。"}
              </span>
              <Button variant="secondary" className="h-7 px-2 text-xs"
                      onClick={() => void loadIndustries()}>
                重试加载
              </Button>
              {status && !status.logged_in && (
                <Link to="/accounts" className="text-primary underline">
                  去「账户」页登录
                </Link>
              )}
            </div>
          )}
          {!loadingIndustries && industries.length > 0 && industriesSource === "cache" && (
            <p className="text-[11px] text-text-tertiary">
              行业清单是<b>上次存下来的快照</b>（这次没能连上平台）。去「账户」页登录后重试加载可刷新。
            </p>
          )}

          <p className="text-xs leading-relaxed text-text-tertiary">
            一共 <b>6 个量级</b>，每档各取这么多条，量级高的序号在前：
            超头部KOL（40w–50w / 30w–40w）、头部KOL（20w–30w / 10w–20w）、
            腰部KOL（5w–10w）、尾部KOL（1w–5w）。
            <br />
            只收录粉丝 1w–50w、曝光与互动中位数都不为 0、且图文或视频任一在预算内的达人；
            两个报价都超的、以及中位数为 0 的都会被剔除。
            <br />
            会一直翻到<b>每个量级都凑够</b>为止，可能要几分钟（采集有节流，宁可慢不触发风控）。
            超头部本来人就少，可能整池子就凑不满 —— 那种情况会<b>如实交少并标出来</b>，
            不会为了凑数无限翻。
            <br />
            填了关键词就按关键词搜（平台那套是「按笔记关键词找博主」）：<b>命中任意一个即算</b>，
            一个人命中多个也<b>不会因此排在前面</b> —— 先后仍然只看粉丝量和报价。
            关键词越多、每个词分到的翻页数越少。
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
                  <FolderOpen className="h-4 w-4" /> 打开所在目录
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
                每次跑出来都一样是程序坏了（平台排序写死，从头取当然一样）。 */}
            <p className="text-xs text-text-tertiary">
              本批从第 <b>{result.stats?.start_page ?? 1}</b> 页开始，共翻 {result.stats?.pages ?? "?"} 页
              {result.stats?.pages === result.stats?.page_limit && !result.stats?.reached_end
                ? "（已到单次上限）" : ""}
              。
              {result.stats?.reached_end
                ? " 平台这边已经翻到底了，没有更多新人了；「开始检索」会从头再来一遍。"
                : <> 点<b>「换一批」</b>会从第 {result.stats?.next_page ?? "?"} 页继续往下取没看过的人。</>}
            </p>

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
                底色标出的单元格 = 该项报价超出该粉丝档位的上限（按规则仍保留，
                因为图文、视频有一项在预算内）。共 {markedCount} 条。
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
                    return (
                      <tr key={index} className="border-t border-border hover:bg-surface-hover">
                        {COLUMNS.map((column) => (
                          <td
                            key={column}
                            className={[
                              "whitespace-nowrap px-3 py-2",
                              NUMERIC.has(column) ? "text-right tabular-nums" : "",
                              over.has(column) ? "bg-warning/20 font-semibold text-text-primary" : "text-text-primary",
                            ].join(" ")}
                            title={`${cellText(row[column])}${over.has(column) ? "（超出该档位上限）" : ""}`}
                          >
                            {column === "主页链接" && row[column] ? (
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
