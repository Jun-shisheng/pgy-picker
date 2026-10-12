import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle, CheckCircle2, KeyRound, Loader2, LogIn, Pencil, Plus, RefreshCw, Trash2,
} from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { api } from "../../lib/api";
import { AccountDialog } from "./AccountDialog";

/**
 * 账户页：**单账号**的凭据管理与登录入口。
 *
 * 这个页面存在的理由：登录本身由人在**可见的浏览器窗口**里完成
 * （`CLAUDE.md` 第二、三条：不绕过风控、不实现打码），所以界面上必须有一个
 * 说清楚「接下来会发生什么」的入口，而不是让用户对着一个空白的选号页猜。
 */
export function AccountsPage() {
  const qc = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const status = useQuery({ queryKey: ["selection-status"], queryFn: api.selectionStatus });
  const account = useQuery({ queryKey: ["account"], queryFn: api.getAccount });

  const hasCredentials = account.data?.has_credentials ?? false;
  const loggedIn = status.data?.logged_in ?? false;
  const busy = status.isLoading || account.isLoading;

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["selection-status"] });
    void qc.invalidateQueries({ queryKey: ["account"] });
  };

  // 登录是**阻塞式**的（用户 2026-10-12 定）：请求一直挂着，直到窗口里的人
  // 把验证码点完。所以文案必须提前说清楚，否则用户会以为程序卡死了。
  const login = useMutation({
    mutationFn: api.loginNow,
    onSuccess: (data) => {
      setMessage({
        ok: true,
        text: data.already ? "当前已经是登录态。" : "登录成功。",
      });
      refresh();
    },
    onError: (error) => setMessage({ ok: false, text: (error as Error).message }),
  });

  const remove = useMutation({
    mutationFn: api.deleteAccount,
    onSuccess: () => {
      setMessage({ ok: true, text: "已删除本机保存的凭据。" });
      refresh();
    },
    onError: (error) => setMessage({ ok: false, text: (error as Error).message }),
  });

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-text-primary">账户</h1>
        <p className="mt-1 text-sm text-text-secondary">
          蒲公英账号只保存在本机凭据管理器里；登录在那个可见的浏览器窗口里完成，
          需要你手动过验证码。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>当前状态</CardTitle>
          <Button variant="secondary" className="h-8 px-3 text-xs" onClick={refresh} disabled={busy}>
            <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} /> 刷新
          </Button>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <span className={hasCredentials
              ? "flex items-center gap-1.5 text-success"
              : "flex items-center gap-1.5 text-warning"}>
              {hasCredentials ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
              凭据：{hasCredentials ? "已配置" : "未配置"}
            </span>
            <span className={loggedIn
              ? "flex items-center gap-1.5 text-success"
              : "flex items-center gap-1.5 text-text-secondary"}>
              {loggedIn ? <CheckCircle2 className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
              平台登录：{loggedIn ? "已登录" : "未登录"}
            </span>
            {status.data?.role && (
              <span className="text-xs text-text-tertiary">角色：{status.data.role}</span>
            )}
          </div>
          {(status.isError || account.isError) && (
            <p className="text-xs text-danger">
              读不到本地服务状态：{((status.error ?? account.error) as Error)?.message}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{hasCredentials ? "已保存的账号" : "还没有账号"}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {hasCredentials ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="flex items-center gap-2 text-sm text-text-primary">
                <KeyRound className="h-4 w-4 text-text-tertiary" />
                <span className="font-mono">{account.data?.username_masked || "已保存"}</span>
                <span className="text-xs text-text-tertiary">（密码不回显）</span>
              </span>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  className="h-8 px-3 text-xs"
                  onClick={() => { setMessage(null); void login.mutate(); }}
                  disabled={login.isPending}
                >
                  {login.isPending
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <LogIn className="h-3.5 w-3.5" />}
                  {login.isPending ? "登录中…" : "去登录"}
                </Button>
                <Button variant="secondary" className="h-8 px-3 text-xs"
                        onClick={() => { setMessage(null); setDialogOpen(true); }}>
                  <Pencil className="h-3.5 w-3.5" /> 修改
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 px-3 text-xs text-danger hover:bg-danger/10"
                  disabled={remove.isPending}
                  onClick={() => {
                    if (window.confirm("确定删除本机保存的账号密码吗？\n删除后需要重新填写才能再登录。")) {
                      setMessage(null);
                      void remove.mutate();
                    }
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" /> 删除凭据
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-text-secondary">
                还没有配置蒲公英账号。添加之后就能去登录了。
              </p>
              <Button
                className="h-9 px-3 text-xs"
                onClick={() => { setMessage(null); setDialogOpen(true); }}
              >
                <Plus className="h-3.5 w-3.5" /> 添加账户
              </Button>
            </div>
          )}

          {login.isPending && (
            <p className="rounded-control border border-border bg-surface-secondary px-3 py-2 text-xs text-text-secondary">
              正在登录，请到<b>屏幕上那个浏览器窗口</b>里完成操作
              —— 如果出现验证码，需要你手动点一下。完成后这里会自动更新。
            </p>
          )}

          {message && (
            <p className={message.ok
              ? "rounded-control border border-success/40 bg-success/5 px-3 py-2 text-xs text-success"
              : "rounded-control border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger"}>
              {message.text}
            </p>
          )}
        </CardContent>
      </Card>

      <AccountDialog
        open={dialogOpen}
        hasCredentials={hasCredentials}
        usernameMasked={account.data?.username_masked ?? ""}
        onClose={() => setDialogOpen(false)}
        onSaved={() => {
          setMessage({ ok: true, text: "账号已保存到本机凭据管理器。" });
          refresh();
        }}
      />
    </div>
  );
}
