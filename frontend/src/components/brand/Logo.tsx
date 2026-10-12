import { Sprout } from "lucide-react";

/**
 * 品牌标记。
 *
 * M0 用图标占位——正式品牌素材待补（见 `task_plan.md` M5）。
 * 刻意不引用任何外部图片，避免把别家的 logo 带进交付物。
 */
export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span
      className={
        compact
          ? "flex h-8 w-8 shrink-0 items-center justify-center rounded-small bg-primary-soft"
          : "flex h-10 w-10 shrink-0 items-center justify-center rounded-control bg-primary-soft"
      }
    >
      <Sprout className={compact ? "h-[18px] w-[18px] text-primary" : "h-5 w-5 text-primary"} />
    </span>
  );
}
