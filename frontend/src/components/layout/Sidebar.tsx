import { Link } from "@tanstack/react-router";
import { Search, Settings, UserRound, type LucideIcon } from "lucide-react";
import { Logo } from "../brand/Logo";

interface NavItem {
  label: string;
  icon: LucideIcon;
  to: string;
}

/**
 * 导航。**只列真正能用的模块** —— 没实现的不占位置，
 * 免得点不动的入口混在里面让人以为坏了。
 */
const NAV: NavItem[] = [
  { label: "选号", icon: Search, to: "/" },
  { label: "账户", icon: UserRound, to: "/accounts" },
  { label: "设置", icon: Settings, to: "/settings" },
];

export function Sidebar() {
  return (
    <aside className="flex w-[232px] shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center gap-2.5 px-5 py-6">
        <Logo compact />
        <span className="text-sm font-bold text-text-primary">蒲公英达人选号</span>
      </div>

      <nav className="flex-1 space-y-1 px-3">
        {NAV.map((item) => {
          const Icon = item.icon;
          return (
            <Link
              key={item.label}
              to={item.to}
              className="flex h-11 items-center gap-3 rounded-control px-3.5 text-sm"
              activeProps={{ className: "bg-primary-soft text-primary font-medium" }}
              inactiveProps={{
                className: "text-text-secondary hover:bg-surface-hover hover:text-text-primary",
              }}
            >
              <Icon className="h-[18px] w-[18px]" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="p-4">
        <div className="rounded-card border border-border bg-surface-secondary p-4">
          <p className="text-xs font-medium text-text-primary">本地单机版</p>
          <p className="mt-1 text-[11px] leading-relaxed text-text-secondary">
            账号与凭据仅保存在本机，不上传服务器。
          </p>
        </div>
      </div>
    </aside>
  );
}
