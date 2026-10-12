import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { Copy, FolderOpen, Pause, Play, RefreshCw } from "lucide-react";
import { Button } from "../../components/ui/button";
import { api, type LogLine } from "../../lib/api";

const LEVEL_TONE: Record<string, string> = {
  CRITICAL: "text-danger font-semibold",
  ERROR: "text-danger",
  WARNING: "text-warning",
  INFO: "text-text-secondary",
  DEBUG: "text-text-tertiary",
};

const LEVEL_OPTIONS = [
  { value: "", label: "全部" },
  { value: "INFO", label: "信息及以上" },
  { value: "WARNING", label: "警告及以上" },
  { value: "ERROR", label: "仅错误" },
];

function Line({ line }: { line: LogLine }) {
  const tone = LEVEL_TONE[line.level] ?? "text-text-secondary";
  return (
    <div className="border-b border-border/40 py-1 last:border-b-0">
      <div className="flex gap-2">
        <span className="shrink-0 text-text-tertiary">{line.at || "—"}</span>
        <span className={`w-16 shrink-0 ${tone}`}>{line.level}</span>
        <span className="min-w-0 flex-1 break-all text-text-primary">{line.message}</span>
      </div>
      {line.detail.length > 0 && (
        <pre className="mt-1 whitespace-pre-wrap break-all pl-[9.5rem] text-text-tertiary">
          {line.detail.join("\n")}
        </pre>
      )}
    </div>
  );
}

interface LogViewerProps {
  /** 日志区域高度（Tailwind 类）。 */
  heightClass?: string;
}

/**
 * 运行日志面板：后端 `pgy_logs/pgy.log` 的可读视图。
 *
 * 设计要点（照搬参考项目实测下来的取舍）：
 * - 默认开 2.5 秒自动刷新：排障时人正盯着屏幕等复现，需要它自己滚；
 * - 关掉自动刷新才能稳定选中某段内容复制，不被刷新打断；
 * - 等级与关键词过滤**在后端执行**，别把上万行日志拉进浏览器再筛。
 */
export function LogViewer({ heightClass = "h-96" }: LogViewerProps) {
  const [level, setLevel] = useState("");
  const [keyword, setKeyword] = useState("");
  const [draftKeyword, setDraftKeyword] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setKeyword(draftKeyword.trim()), 400);
    return () => clearTimeout(timer);
  }, [draftKeyword]);

  const logs = useQuery({
    queryKey: ["logs", level, keyword],
    queryFn: () => api.logs({ limit: 500, level, q: keyword }),
    refetchInterval: autoRefresh ? 2_500 : false,
    retry: false,
  });

  const payload = logs.data;
  const lines = useMemo(() => payload?.lines ?? [], [payload]);

  const copyAll = async () => {
    const text = lines
      .map((line) => `${line.at} ${line.level} ${line.logger} ${line.message}`
        + (line.detail.length ? `\n${line.detail.join("\n")}` : ""))
      .join("\n");
    if (!text) {
      setMessage("当前没有可复制的日志");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setMessage(`已复制 ${lines.length} 行日志`);
    } catch {
      setMessage("复制失败，请手动选中日志内容复制");
    }
  };

  const openFolder = async () => {
    if (!isTauri()) {
      setMessage(`日志目录：${payload?.dir ?? "（未知）"}（当前不是桌面客户端环境，无法直接打开）`);
      return;
    }
    try {
      setMessage(`已打开日志目录：${await invoke<string>("open_log_dir")}`);
    } catch (error) {
      setMessage(String(error));
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="h-9 rounded-control border border-border bg-surface px-2 text-xs"
          value={level}
          onChange={(event) => setLevel(event.target.value)}
          aria-label="日志等级过滤"
        >
          {LEVEL_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <input
          className="h-9 min-w-[200px] flex-1 rounded-control border border-border bg-surface px-3 text-xs"
          placeholder="搜索关键词（操作名、错误类型…）"
          value={draftKeyword}
          onChange={(event) => setDraftKeyword(event.target.value)}
        />
        <Button variant="secondary" className="h-9 px-3 text-xs"
                onClick={() => void logs.refetch()} disabled={logs.isFetching}>
          <RefreshCw className={`h-3.5 w-3.5 ${logs.isFetching ? "animate-spin" : ""}`} /> 刷新
        </Button>
        <Button variant="secondary" className="h-9 px-3 text-xs"
                onClick={() => setAutoRefresh((value) => !value)}>
          {autoRefresh ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
          {autoRefresh ? "暂停自动刷新" : "开启自动刷新"}
        </Button>
        <Button variant="secondary" className="h-9 px-3 text-xs" onClick={() => void copyAll()}>
          <Copy className="h-3.5 w-3.5" /> 复制
        </Button>
        <Button variant="secondary" className="h-9 px-3 text-xs" onClick={() => void openFolder()}>
          <FolderOpen className="h-3.5 w-3.5" /> 打开日志目录
        </Button>
      </div>

      <div className={`overflow-auto rounded-control border border-border bg-surface-secondary p-3 font-mono text-[11px] leading-relaxed ${heightClass}`}>
        {logs.isError ? (
          <p className="text-danger">读取日志失败：{(logs.error as Error).message}</p>
        ) : lines.length === 0 ? (
          <p className="text-text-tertiary">
            {logs.isLoading ? "正在读取日志…" : "当前过滤条件下没有日志记录。"}
          </p>
        ) : (
          lines.map((line, index) => <Line key={`${line.at}-${index}`} line={line} />)
        )}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-text-tertiary">
        <span className="break-all">
          {payload ? `日志文件：${payload.file}` : "日志文件：读取中…"}
          {payload?.truncated ? "（仅显示最近一段）" : ""}
        </span>
        <span>{message || (lines.length ? `共 ${lines.length} 行` : "")}</span>
      </div>
    </div>
  );
}
