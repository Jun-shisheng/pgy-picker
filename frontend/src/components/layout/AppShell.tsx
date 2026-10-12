import { Outlet } from "@tanstack/react-router";
import { StartupAutoLogin } from "../../lib/StartupAutoLogin";
import { BackendStatusBanner } from "./BackendStatusBanner";
import { Sidebar } from "./Sidebar";

/**
 * 最小外壳：侧边栏 + 状态横幅 + 内容区。
 *
 * 刻意不带 Topbar 与全局操作进度条——那两个在参考项目里与投放业务强耦合
 * （账户登录态、投放进度轮询），本项目等有了会话与任务模型再按需加回。
 * 选号自己的进度画在选号页里（`features/selection/ProgressPanel`）。
 */
export function AppShell() {
  return (
    <div className="flex h-screen w-screen bg-background p-6">
      <div className="flex h-full w-full overflow-hidden rounded-shell border border-border bg-surface shadow-card">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          {/* 后端不可用时在内容区顶部给出可操作的提示，保证任何页面都能看到原因。 */}
          <div className="flex flex-col px-6 pt-4 empty:hidden">
            <BackendStatusBanner />
            <StartupAutoLogin />
          </div>
          <main className="flex-1 overflow-y-auto bg-background p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  );
}
