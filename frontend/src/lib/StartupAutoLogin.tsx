import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { Loader2, X } from "lucide-react";
import { api } from "./api";

/**
 * 启动时用**已保存的账号**自动登录一次（用户 2026-10-12 要求）。
 *
 * 规则与姊妹项目 `project2-offical` 的 `StartupAutoLogin` 一致：
 *
 * 1. **已经登录** → 什么都不做，用户爱去哪去哪；
 * 2. **有凭据但未登录** → 自动登录一次（会打开那个自动化浏览器窗口）；
 * 3. **没有凭据** → 把人送到「账户」页，由他自己决定什么时候登录。
 *
 * 两处与姊妹项目**刻意不同**：
 * - 它自动登录前要先弹「会关闭你所有 Edge 窗口」的确认，因为它接管的是用户日常浏览器；
 *   本项目用的是**专属配置目录**（`profiles/pugongying`），不碰用户日常浏览器，
 *   所以没有这一步。
 * - 它把「主动退出过就不再自动登录」记在 localStorage；本项目还没有退出入口，
 *   等有了再加，免得现在写一段没有触发点的逻辑。
 */
export function StartupAutoLogin() {
  const navigate = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();

  // 每次客户端启动只自动提交一次登录；会话状态随后会变，不能把状态变化当成新的登录。
  const attempted = useRef(false);
  // 没有账号时只把人送进登录页一次，否则他一点别的模块就会被弹回来。
  const landedOnLogin = useRef(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const status = useQuery({
    queryKey: ["selection-status"],
    queryFn: api.selectionStatus,
    retry: false,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (status.isLoading || status.isError || attempted.current) return;
    const data = status.data;
    if (!data) return;
    if (data.logged_in) return;

    const goToAccountsOnce = () => {
      if (landedOnLogin.current || location.pathname === "/accounts") return;
      landedOnLogin.current = true;
      void navigate({ to: "/accounts" });
    };

    if (!data.has_credentials) {
      goToAccountsOnce();
      return;
    }

    attempted.current = true;
    setBusy(true);
    setMessage("检测到已保存的账号，正在自动登录…（需要人工验证时，请到那个浏览器窗口里完成）");
    // 一次性动作，不设"已取消"闸门：StrictMode 的挂载/卸载演练会在请求进行中
    // 跑一遍清理函数，据此丢弃结果的话，开发环境就永远不会自动登录。
    void (async () => {
      try {
        await api.loginNow();
        setMessage("已自动登录。");
      } catch (error) {
        setMessage(`自动登录未完成：${(error as Error).message}`);
        goToAccountsOnce();
      } finally {
        setBusy(false);
        void qc.invalidateQueries({ queryKey: ["selection-status"] });
        void qc.invalidateQueries({ queryKey: ["account"] });
      }
    })();
  }, [location.pathname, navigate, qc, status.data, status.isError, status.isLoading]);

  if (!message) return null;

  return (
    <div
      role="status"
      className="mb-3 flex items-start gap-2 rounded-control border border-border bg-surface-secondary px-4 py-2.5 text-xs text-text-secondary"
    >
      {busy && <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-primary" />}
      <span className="min-w-0 flex-1">{message}</span>
      {!busy && (
        <button
          onClick={() => setMessage("")}
          className="shrink-0 text-text-tertiary hover:text-text-primary"
          aria-label="关闭"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
