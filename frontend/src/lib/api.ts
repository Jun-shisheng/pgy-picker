/**
 * 本地服务（sidecar）的 HTTP 契约。
 *
 * 只监听回环地址，端口 18081 必须与 `backend_sidecar.py` 的 `DEFAULT_PORT`、
 * Rust 壳的 `BACKEND_PORT` 三方保持一致（改一处就要改三处）。
 */

export const API_BASE = "http://127.0.0.1:18081";

export interface HealthResponse {
  status: string;
  service: string;
  browser: string;
  product: string;
}

export interface SessionResponse {
  account_id: string;
  account: Record<string, unknown>;
  connection_status: string;
  executor_status: string;
  control_mode: string;
  operation: Record<string, unknown>;
}

// ---- 选号 ----

/** 固定的量级档位（名称/区间与后端 `fields.TIERS` 同源）。 */
export interface SelectionTier {
  /** 传给后端的量级名，如 `头部KOL(20w-30w)`。 */
  key: string;
  label: string;
  bracket: string;
  low: number;
  high: number;
}

export interface SelectionStatus {
  has_credentials: boolean;
  login_status: number;
  logged_in: boolean;
  role: string;
  /** 上次真问到登录态是多久以前（秒）；null = 还没问到过。 */
  login_checked_age_s: number | null;
  /** 报价档位配没配。**没配 = 预算把关关着**，交付表会混进超预算的达人。 */
  pricing_configured: boolean;
  pricing_multiplier: number | null;
  /** 六个固定量级。**每次检索选哪些、各取几条在选号页当场设**。 */
  tiers?: SelectionTier[];
}

/** 地域树：中国 → 省/直辖市 → 市 → 区。`value` 是中文名称（平台的 `location` 要名称）。 */
export interface AreaNode {
  value: string;
  label: string;
  code?: string;
  level?: number;
  children?: AreaNode[];
}

export interface AreasResponse {
  areas: AreaNode[];
  source: "live" | "cache";
}

/** 交付表的一行。键就是客户最新模板那 10 列（2026-11-10 起，11-16 加「小红书号」）。 */
export type BloggerRow = Record<string, string | number | null>;

export interface SelectionRunRequest {
  /** **每个量级**取多少条（不是总数）。旧参数；给了 `tier_counts` 时作默认值。 */
  per_tier?: number;
  /** 量级名 → 条数。**没列出的量级不检索**（界面只发勾选的那些）。 */
  tier_counts?: Record<string, number>;
  /** 勾选的量级名；不传 = 六个量级全要。 */
  enabled_tiers?: string[];
  /** 勾选地域的完整路径（如 `中国 安徽 合肥 蜀山区`，多选是「或」）；留空=不限。 */
  regions?: string[];
  interval?: number;
  /**
   * `fresh` = 从头取（同一组条件给同一批人，平台排序是写死的）；
   * `next` = 换一批，接着该条件上次翻到的位置往下取。
   */
  mode?: "fresh" | "next";
  /**
   * 随手输入的一串**检索词**（逗号分隔，界面已拆好）。
   *
   * **由后端自己判断**哪些是行业、哪些是笔记关键词 —— 判断过程对界面是隐性的，
   * 只记在运行日志与统计里（用户 2026-10-12：这些逻辑不要暴露出来）。
   */
  terms?: string[];
}

export interface SelectionStats {
  /** 本次翻了几页。 */
  pages: number;
  /** 整轮翻页安全阀（防失控，通常远达不到）。 */
  page_limit?: number;
  /** 是否被页数上限**截断**（= 后面还有没取到的，可点「换一批」继续）。 */
  stopped_by_page_limit?: boolean;
  /** 被上限截断的「档位 × 关键词」组合数。 */
  keywords_cut_by_limit?: number;
  keywords_exhausted?: number;
  /** 本次实际用的关键词。 */
  keywords?: string[];
  /**
   * 哪些量级动用了「不带检索词」的兜底、各补进来几条。
   * ⚠️ **这些人不保证命中检索词** —— 用户 2026-10-13 拍板接受
   * （「每个量级必凑满」优先于「只收命中检索词的人」）。
   */
  keyword_fallback?: Record<string, number>;
  /** 每个「档位 × 关键词」各翻了几页（额度是各自算的，不是总预算均分）。 */
  pages_per_keyword?: Record<string, number>;
  /** 本批各档位从第几页开始取 / 下次各档位从第几页继续（「换一批」的依据）。 */
  start_page?: number;
  start_pages?: Record<string, number>;
  next_page?: number;
  next_pages?: Record<string, number>;
  /** 没凑满的档位 → 实际条数（高量级本来就少，如实报）。 */
  tiers_short?: Record<string, number>;
  /** 平台那边已经没数据了（与"没凑满名额"是两回事）。 */
  reached_end?: boolean;
  total: number;
  scanned: number;
  collected: number;
  /** 旧口径的单一目标；实际每档目标见 `per_tier_targets`。 */
  per_tier_target: number;
  /** 勾选的量级 → 这次各自的目标条数。 */
  per_tier_targets?: Record<string, number>;
  /** 本次的省/市地域；空串 = 不限。 */
  region?: string;
  /** 量级名 → 实际取到几条。 */
  per_tier_counts: Record<string, number>;
  /** 没凑满目标条数的量级。 */
  per_tier_short?: Record<string, number>;
  exhausted: boolean;
  [key: string]: unknown;
}

export interface SelectionRunResponse {
  rows: BloggerRow[];
  stats: SelectionStats;
  export: { path: string; name: string; dir: string };
  throttle: Record<string, number>;
}

/** 选号进度快照。`stage` 与后端 `services/progress.py` 的取值一一对应。 */
export interface SelectionProgress {
  stage: "idle" | "login" | "search" | "enrich" | "export" | "done" | "error";
  label: string;
  detail: string;
  page: number;
  collected: number;
  total: number;
  per_tier_counts: Record<string, number>;
  /** 「补充主页数据」阶段：已补人数 / 总人数 / 当前达人昵称。 */
  enriched?: number;
  enrich_total?: number;
  current?: string;
  started_at: string;
  updated_at: string;
  active: boolean;
  error: string;
  /** 失败时：中断前所处的阶段（用来告诉用户"停在哪一步"）。 */
  failed_stage: string;
  failed_label: string;
}

// ---- 账户 ----

/** 账号概览。**没有 password 字段，也不会加** —— 后端任何接口都不回传密码。 */
export interface AccountInfo {
  has_credentials: boolean;
  username_masked: string;
}

// ---- 设置 ----

export interface AppSettings {
  export_dir: string;
  export_dir_is_default: boolean;
  settings_file: string;
  resolved_export_dir: string;
}

/** 一个报价档位。区间/名称固定，设置页只改 image/video 两个基准价。 */
export interface PricingTier {
  label: string;
  bracket: string;
  min: number;
  max: number;
  image: number;
  video: number;
}

export interface PricingConfig {
  /** 是否已经有一份生效的 pricing.json（false = 预算把关关闭）。 */
  configured: boolean;
  /** 生效配置的来源路径；可能为空。 */
  source: string;
  /** 这个安装包里有没有内置的默认档位（设置页「恢复开发默认」用）。 */
  default_available?: boolean;
  multiplier: number;
  tiers: PricingTier[];
}

// ---- 运行日志 ----

/** 一行日志。`detail` 是续行（如 Python traceback 的堆栈），给同一条异常收在一起。 */
export interface LogLine {
  at: string;
  level: string;
  logger: string;
  message: string;
  detail: string[];
}

export interface LogPayload {
  file: string;
  dir: string;
  exists: boolean;
  size: number;
  truncated: boolean;
  lines: LogLine[];
  matched?: number;
}

/** 统一的请求封装：把后端的错误信封翻成一句可直接显示的中文。
 *
 * ⚠️ **超时文案必须各自给**：原来所有请求共用一句「选号耗时较长…」，
 * 于是读进度、读状态超时都会说成"选号超时" —— 用户按着这句话去调条数，
 * 真因却在别处（实测踩到：检索明明 200 成功返回，界面却报超时）。
 */
async function request<T>(path: string, init?: RequestInit, timeoutMs = 30_000,
                          timeoutMessage = "本地服务响应超时。"): Promise<T> {
  const controller = new AbortController();
  // 选号要真的去平台翻页，可能几十秒到几分钟；默认给足余量。
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
    const raw = await response.text();
    if (!response.ok) {
      // 后端所有 4xx/5xx 都是 {"detail","code","retryable","operation_id"}，
      // detail 已经是写给人看的中文，直接透传，不要在这里二次加工。
      let detail = `本地服务返回 HTTP ${response.status}`;
      try {
        const payload = JSON.parse(raw) as { detail?: string };
        if (payload?.detail) detail = payload.detail;
      } catch {
        /* 非 JSON 响应，保留兜底文案 */
      }
      throw new Error(detail);
    }
    return (raw ? JSON.parse(raw) : {}) as T;
  } catch (error) {
    if ((error as Error)?.name === "AbortError") {
      throw new Error(timeoutMessage);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: () => request<HealthResponse>("/api/health"),
  session: () => request<SessionResponse>("/api/session"),

  selectionStatus: () =>
    request<SelectionStatus>("/api/selection/status", undefined, 30_000,
      "读取状态超时 —— 本地服务可能正在忙着检索，等它跑完再看。"),
  /** 选号进度。轮询用，超时给短一点：它只该是个瞬时快照。 */
  selectionProgress: () =>
    request<SelectionProgress>("/api/selection/progress", undefined, 8_000,
      "读取进度超时。"),
  selectionAreas: () =>
    request<AreasResponse>("/api/selection/areas", undefined, 120_000,
      "读取地域清单超时 —— 本地服务可能正在忙着检索，等它跑完再试。"),
  selectionRun: (body: SelectionRunRequest) =>
    request<SelectionRunResponse>(
      "/api/selection/run",
      { method: "POST", body: JSON.stringify(body) },
      10 * 60_000, // 10 分钟：真的会翻很多页
      "选号响应超时。可能是平台很慢或翻页过多 —— 可以稍后重试，"
      + "或把「每个量级取多少条」调小。",
    ),

  /** 导出文件的下载地址（供浏览器直接打开）。 */
  artifactUrl: (name: string) => `${API_BASE}/api/selection/artifact/${encodeURIComponent(name)}`,

  getSettings: () => request<AppSettings>("/api/settings"),
  saveSettings: (exportDir: string) =>
    request<AppSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ export_dir: exportDir }),
    }),
  validateExportDir: (exportDir: string) =>
    request<{ ok: boolean; detail: string }>("/api/settings/validate", {
      method: "POST",
      body: JSON.stringify({ export_dir: exportDir }),
    }),

  // ---- 报价档位 ----

  getPricing: () => request<PricingConfig>("/api/pricing"),
  getDefaultPricing: () =>
    request<{ multiplier: number; tiers: PricingTier[] }>("/api/pricing/default"),
  savePricing: (body: { multiplier: number; tiers: PricingTier[] }) =>
    request<PricingConfig>("/api/pricing", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  reportClientEvent: (body: Record<string, unknown>) =>
    request<{ ok: boolean }>("/api/logs/client", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** 运行日志尾部。过滤在后端做 —— 日志上万行，别整份拉进浏览器再筛。 */
  logs: (params: { limit?: number; level?: string; q?: string } = {}) => {
    const search = new URLSearchParams();
    search.set("limit", String(params.limit ?? 400));
    if (params.level) search.set("level", params.level);
    if (params.q) search.set("q", params.q);
    return request<LogPayload>(`/api/logs/tail?${search.toString()}`);
  },

  // ---- 账户（单账号）----

  getAccount: () => request<AccountInfo>("/api/account"),
  saveAccount: (username: string, password: string) =>
    request<AccountInfo>("/api/account", {
      method: "PUT",
      body: JSON.stringify({ username, password }),
    }),
  deleteAccount: () =>
    request<{ ok: boolean; deleted: boolean }>("/api/account", { method: "DELETE" }),
  /** 触发登录：在屏幕上那个可见的浏览器窗口里跑，要人过验证码，可能很久。 */
  loginNow: () => request<{ connected: boolean; already: boolean }>(
    "/api/account/login", { method: "POST" }, 10 * 60_000,
    "登录等待超时。如果浏览器窗口里还需要你点验证码，请完成后再试一次。"),
};
