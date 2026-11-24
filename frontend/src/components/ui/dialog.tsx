import { type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

interface DialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  /** md 用于表单类弹窗；lg 用于日志等需要更大可视区域的场景；
   * region 用于地域四列选择器（接近网页版弹层宽度）。 */
  size?: "md" | "lg" | "region";
  bodyClassName?: string;
}

export function Dialog({ open, title, children, onClose, footer, size = "md", bodyClassName }: DialogProps) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      {/* 遮罩层只负责压暗背景，绝不响应点击关闭：表单弹窗（添加账号/短信验证码）
          里一次误触就会丢掉已填内容、登录中途也会被打断。弹窗只通过右上角 X 或
          弹窗内的明确按钮（取消等）关闭。 */}
      <div
        className="absolute inset-0 bg-black/20"
        aria-hidden="true"
      />
      <div
        className={cn(
          "relative w-full rounded-panel border border-border bg-surface shadow-card-hover",
          size === "lg" ? "max-w-5xl" : size === "region" ? "max-w-3xl" : "max-w-md",
        )}
      >
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <h2 className="text-base font-semibold text-text-primary">{title}</h2>
          <button
            onClick={onClose}
            className="text-text-tertiary transition-colors hover:text-text-primary"
            aria-label="关闭"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className={bodyClassName ?? "px-6 py-5"}>{children}</div>
        {footer ? (
          <div className="flex justify-end gap-3 border-t border-border px-6 py-4">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}
