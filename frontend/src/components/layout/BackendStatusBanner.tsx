import { useQuery } from "@tanstack/react-query";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { CircleAlert, FolderOpen, RotateCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/button";
import { api } from "../../lib/api";

/**
 * 本地服务（sidecar）不可用时的常驻提示条。
 *
 * 为什么需要它：打包后的客户端如果后台服务没起来，界面上的每个按钮都会失败，
 * 但用户看不到任何原因，只能描述「点了没反应」。这里把「服务未运行 / 启动失败」
 * 明确说出来，并给出可直接执行的下一步（重试、看启动错误、打开日志目录）。
 *
 * 启动后前 25 秒视为「正在启动」：onefile 首启动要解压上百 MB，属于正常等待，
 * 此时不该弹红色错误把用户吓一跳。
 */
const STARTUP_GRACE_MS = 25_000;

export function BackendStatusBanner() {
  const mountedAt = useRef(Date.now());
  const [now, setNow] = useState(() => Date.now());
  const [startupError, setStartupError] = useState("");
  const [folderMessage, setFolderMessage] = useState("");

  const health = useQuery({
    queryKey: ["backend-health"],
    queryFn: api.health,
    retry: false,
    refetchOnWindowFocus: false,
    // 正常时低频探活（20 秒），异常时快速重试：后端中途崩溃也能在一分钟内被发现。
    refetchInterval: (query) => (query.state.status === "error" ? 3_000 : 20_000),
  });

  const healthy = health.isSuccess;
  useEffect(() => {
    if (healthy) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [healthy]);

  if (healthy) return null;

  const starting = now - mountedAt.current < STARTUP_GRACE_MS;
  const detail = health.isError ? (health.error as Error).message : "";

  const readStartupError = async () => {
    setStartupError("");
    if (!isTauri()) {
      setStartupError("当前不是桌面客户端环境，无法读取启动日志文件。");
      return;
    }
    try {
      setStartupError(await invoke<string>("read_startup_error"));
    } catch (error) {
      setStartupError(String(error));
    }
  };

  const openLogFolder = async () => {
    setFolderMessage("");
    if (!isTauri()) {
      setFolderMessage("当前不是桌面客户端环境，无法打开日志目录。");
      return;
    }
    try {
      const dir = await invoke<string>("open_log_dir");
      setFolderMessage(`已打开日志目录：${dir}`);
    } catch (error) {
      setFolderMessage(String(error));
    }
  };

  return (
    <div
      role="status"
      className={
        starting
          ? "mb-3 shrink-0 rounded-control border border-border bg-surface-secondary px-4 py-3 text-sm text-text-secondary"
          : "mb-3 shrink-0 rounded-control border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger"
      }
    >
      <div className="flex flex-wrap items-center gap-3">
        {starting ? (
          <span>正在启动本地服务，请稍候…（首次启动需要解压程序，可能耗时较久）</span>
        ) : (
          <>
            <CircleAlert className="h-4 w-4 shrink-0" />
            <span>本地服务未运行或启动失败：{detail || "未知原因"}</span>
          </>
        )}
        {!starting && (
          <span className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              className="h-8 px-3 text-xs"
              onClick={() => void health.refetch()}
              disabled={health.isFetching}
            >
              <RotateCw className={`h-3.5 w-3.5 ${health.isFetching ? "animate-spin" : ""}`} /> 重试
            </Button>
            <Button variant="secondary" className="h-8 px-3 text-xs" onClick={() => void readStartupError()}>
              查看启动失败原因
            </Button>
            <Button variant="secondary" className="h-8 px-3 text-xs" onClick={() => void openLogFolder()}>
              <FolderOpen className="h-3.5 w-3.5" /> 打开日志目录
            </Button>
          </span>
        )}
      </div>
      {startupError && (
        <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-control bg-surface p-3 font-mono text-[11px] text-danger">
          {startupError}
        </pre>
      )}
      {folderMessage && <p className="mt-2 text-xs text-text-secondary">{folderMessage}</p>}
    </div>
  );
}
