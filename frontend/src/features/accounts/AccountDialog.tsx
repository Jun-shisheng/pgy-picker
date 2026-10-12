import { useEffect, useState } from "react";
import { Loader2, Save } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Dialog } from "../../components/ui/dialog";
import { api } from "../../lib/api";

interface AccountDialogProps {
  open: boolean;
  /** 已有凭据时，密码留空表示「不改密码」。 */
  hasCredentials: boolean;
  /** 已保存的账号名（掩码），仅用于提示，不作为输入框的初值 —— 我们拿不到明文。 */
  usernameMasked: string;
  onClose: () => void;
  onSaved: () => void;
}

const inputClass =
  "h-10 w-full rounded-control border border-border bg-surface px-3 text-sm text-text-primary";

/**
 * 录入 / 修改蒲公英账号。
 *
 * ⚠️ 两条纪律写在代码里：
 * 1. 密码**只在提交那一刻**离开这个组件，不预填、不回显、不进任何本地存储；
 * 2. 已有凭据时账号框留空 = 不改 —— 因为我们**读不到**原账号（接口不回传），
 *    所以不做「回填原始值」这种假动作。
 */
export function AccountDialog({
  open, hasCredentials, usernameMasked, onClose, onSaved,
}: AccountDialogProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  // 每次打开都从空白开始：上一个账号的输入不该留到下一次。
  useEffect(() => {
    if (open) {
      setUsername("");
      setPassword("");
      setMessage("");
      setSaving(false);
    }
  }, [open]);

  const submit = async () => {
    if (!username.trim()) {
      setMessage("请填写蒲公英登录账号。");
      return;
    }
    if (!hasCredentials && !password) {
      setMessage("请填写密码。");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      await api.saveAccount(username.trim(), password);
      onSaved();
      onClose();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      title={hasCredentials ? "修改账号" : "添加账号"}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            取消
          </Button>
          <Button onClick={() => void submit()} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            保存
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <label className="block space-y-1.5">
          <span className="text-xs text-text-secondary">蒲公英登录账号</span>
          <input
            className={inputClass}
            value={username}
            autoComplete="off"
            placeholder={hasCredentials ? `当前：${usernameMasked || "已保存"}（留空则不改）` : "例如 name@example.com"}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>

        <label className="block space-y-1.5">
          <span className="text-xs text-text-secondary">密码</span>
          <input
            className={inputClass}
            type="password"
            value={password}
            autoComplete="new-password"
            placeholder={hasCredentials ? "留空则沿用原密码" : "必填"}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        <p className="text-[11px] leading-relaxed text-text-tertiary">
          密码只写入 <b>Windows 凭据管理器</b>（<span className="font-mono">pgy://pugongying/account/default</span>），
          不落数据库、不进日志、任何接口都不会把它读回来。
        </p>

        {message && (
          <p className="rounded-control border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-danger">
            {message}
          </p>
        )}
      </div>
    </Dialog>
  );
}
