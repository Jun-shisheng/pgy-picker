import { api } from "./api";

/**
 * 把界面里的未捕获错误送回后端日志。
 *
 * 为什么需要：打包后前端没有控制台，React 渲染错误、事件回调异常、未处理的 Promise
 * 拒绝全都悄无声息——用户只能描述「点了一下就白屏了」。这里统一上报到同一份运行日志，
 * 与后台的时间线对照即可定位。
 */
let installed = false;
let sentCount = 0;
const lastSentAt = new Map<string, number>();

/** 护栏：一次会话最多上报这么多条，同一条消息在窗口期内只报一次，防止刷爆日志。 */
const MAX_REPORTS = 40;
const SAME_MESSAGE_INTERVAL_MS = 10_000;

export function reportClientError(kind: string, message: string, stack = ""): void {
  const text = (message || "").trim();
  if (!text || sentCount >= MAX_REPORTS) return;
  const now = Date.now();
  const previous = lastSentAt.get(text) ?? 0;
  if (now - previous < SAME_MESSAGE_INTERVAL_MS) return;
  lastSentAt.set(text, now);
  sentCount += 1;
  void api
    .reportClientEvent({
      kind,
      message: text.slice(0, 2000),
      stack: stack.slice(0, 4000),
      url: location.href,
    })
    .catch(() => undefined);
}

export function installErrorReporter(): void {
  if (installed) return;
  installed = true;
  window.addEventListener("error", (event) => {
    const error = event.error as Error | undefined;
    reportClientError(
      "window.error",
      error?.message || String(event.message || ""),
      error?.stack || "",
    );
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason as unknown;
    const error = reason instanceof Error ? reason : undefined;
    reportClientError(
      "unhandledrejection",
      error?.message || String(reason ?? ""),
      error?.stack || "",
    );
  });
}
