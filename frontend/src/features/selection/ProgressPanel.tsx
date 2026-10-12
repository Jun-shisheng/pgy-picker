import { useEffect, useState } from "react";
import { AlertCircle, Check, Loader2 } from "lucide-react";
import type { SelectionProgress } from "../../lib/api";

/** 步骤条按这个顺序画；与后端 `progress.py` 的 stage 取值对应。 */
const STEPS = [
  { key: "login", label: "登录平台" },
  { key: "search", label: "检索达人" },
  { key: "export", label: "生成 Excel" },
] as const;

const STEP_ORDER = STEPS.map((step) => step.key) as readonly string[];

function stepState(stage: string, key: string): "done" | "active" | "todo" {
  const current = STEP_ORDER.indexOf(stage);
  const target = STEP_ORDER.indexOf(key);
  if (stage === "done") return "done";
  if (stage === "error") return "todo";
  if (target < current) return "done";
  if (target === current) return "active";
  return "todo";
}

function elapsedText(startedAtMs: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - startedAtMs) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

interface ProgressPanelProps {
  progress: SelectionProgress | undefined;
  /** 本次运行的客户端起点（用于自己算耗时，不依赖后端时钟）。 */
  startedAtMs: number;
}

/**
 * 选号进度。
 *
 * 为什么值得单独做一块：采集**不驱动界面**（在页面上下文里直接调平台接口），
 * 所以那个可见的浏览器窗口在翻页期间**一动不动**。没有这块，用户看到的
 * 就是一个转了半天的按钮加一个毫无反应的浏览器 —— 分不清是在干活还是卡死了。
 */
export function ProgressPanel({ progress, startedAtMs }: ProgressPanelProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  if (!progress || progress.stage === "idle") return null;

  const failed = progress.stage === "error";
  const finished = progress.stage === "done";

  return (
    <div className={[
      "space-y-3 rounded-control border px-4 py-3 text-xs",
      failed ? "border-danger/40 bg-danger/5"
        : finished ? "border-success/40 bg-success/5"
          : "border-border bg-surface-secondary",
    ].join(" ")}>
      {/* 步骤条 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {STEPS.map((step, index) => {
          const state = stepState(progress.stage, step.key);
          return (
            <span key={step.key} className="flex items-center gap-2">
              {index > 0 && <span className="text-text-tertiary">→</span>}
              <span className={[
                "flex items-center gap-1.5",
                state === "done" ? "text-success"
                  : state === "active" ? "font-medium text-primary"
                    : "text-text-tertiary",
              ].join(" ")}>
                {state === "done" ? <Check className="h-3.5 w-3.5" />
                  : state === "active" ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <span className="inline-block h-3.5 w-3.5 rounded-full border border-current" />}
                {step.label}
              </span>
            </span>
          );
        })}
        <span className="text-text-tertiary">
          {failed ? "已中断" : finished ? "已完成" : `已进行 ${elapsedText(startedAtMs, now)}`}
        </span>
      </div>

      {/* 检索阶段的计数：页码 + 已取条数 + 各量级进度 */}
      {progress.stage === "search" && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-text-secondary">
          <span>第 <b>{progress.page}</b> 页</span>
          <span>已取 <b>{progress.collected}</b> 条</span>
          {progress.total > 0 && <span>平台共 {progress.total} 条</span>}
          {Object.entries(progress.per_tier_counts).map(([tier, count]) => (
            <span key={tier} className="text-text-tertiary">{tier} {count}</span>
          ))}
        </div>
      )}

      {progress.detail && !failed && (
        <p className="text-text-secondary">{progress.detail}</p>
      )}

      {failed && (
        <p className="flex items-start gap-2 text-danger">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <b>已中断{progress.failed_label ? `（停在「${progress.failed_label}」）` : ""}：</b>
            {progress.error || progress.detail}
          </span>
        </p>
      )}

      {progress.active && (
        <p className="text-[11px] text-text-tertiary">
          提示：采集是在页面里直接调平台接口，那个浏览器窗口**不会自己翻页或滚动**，属正常。
        </p>
      )}
    </div>
  );
}
