import { useEffect, useState } from "react";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { AlertCircle, FolderOpen, Loader2, Save } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { api } from "../../lib/api";
import { LogViewer } from "./LogViewer";

/**
 * 设置页。
 *
 * 三块：**表格输出位置**（原先挤在选号页上，跟"筛选"混在一起；
 * 它其实是"一次配好长期不变的偏好"）、**运行日志**（出问题时唯一的现场）、
 * **系统状态**（日志在哪、启动失败原因在哪）。
 */
export function SettingsPage() {
  const [exportDir, setExportDir] = useState("");
  const [resolved, setResolved] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [startupError, setStartupError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.getSettings();
        setExportDir(data.export_dir);
        setResolved(data.resolved_export_dir);
      } catch (error) {
        setMessage((error as Error).message);
      }
    })();
  }, []);

  const save = async () => {
    setSaving(true);
    setMessage("");
    try {
      const data = await api.saveSettings(exportDir);
      setExportDir(data.export_dir);
      setResolved(data.resolved_export_dir);
      setMessage(`已保存。表格将输出到：${data.resolved_export_dir}`);
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const openExportDir = async () => {
    setMessage("");
    if (!isTauri()) {
      setMessage(`导出目录：${resolved}（当前不是桌面客户端环境，无法直接打开）`);
      return;
    }
    try {
      await invoke<string>("open_path", { path: resolved });
    } catch (error) {
      setMessage(String(error));
    }
  };

  const [logDirMessage, setLogDirMessage] = useState("");
  const openLogDir = async () => {
    setLogDirMessage("");
    if (!isTauri()) {
      setLogDirMessage("当前不是桌面客户端环境，无法打开日志目录。");
      return;
    }
    try {
      setLogDirMessage(`已打开：${await invoke<string>("open_log_dir")}`);
    } catch (error) {
      setLogDirMessage(String(error));
    }
  };

  const readStartupError = async () => {
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

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-text-primary">设置</h1>
        <p className="mt-1 text-sm text-text-secondary">
          本机偏好与运行日志。设置只存在这台机器上，不会上传。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>表格输出位置</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-text-secondary">
            导出的 Excel 存到哪个目录。默认跟程序放在一起（安装目录下的
            <span className="font-mono"> exports\</span>），重装与卸载都<b>不会</b>删掉这里的表格。
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="h-9 min-w-[320px] flex-1 rounded-control border border-border bg-surface px-3 font-mono text-xs text-text-primary"
              value={exportDir}
              placeholder="例如 D:\达人提报"
              onChange={(event) => setExportDir(event.target.value)}
            />
            <Button variant="secondary" className="h-9 px-3 text-xs"
                    onClick={() => void save()} disabled={saving}>
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              保存
            </Button>
            <Button variant="secondary" className="h-9 px-3 text-xs" onClick={() => void openExportDir()}>
              <FolderOpen className="h-3.5 w-3.5" /> 打开目录
            </Button>
            <Button
              variant="secondary"
              className="h-9 px-3 text-xs"
              onClick={() => { setExportDir(""); setMessage("清空后点保存即恢复默认位置。"); }}
            >
              恢复默认
            </Button>
          </div>
          {resolved && (
            <p className="text-[11px] text-text-tertiary">
              当前生效：<span className="font-mono">{resolved}</span>
            </p>
          )}
          {message && <p className="text-xs text-text-secondary">{message}</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>运行日志</CardTitle>
        </CardHeader>
        <CardContent>
          <LogViewer />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>排查用</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-text-secondary">
            客户端起不来时，原因记在启动错误文件里；后端服务的日志与它同在一个目录。
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" className="h-8 px-3 text-xs" onClick={() => void readStartupError()}>
              <AlertCircle className="h-3.5 w-3.5" /> 查看启动失败原因
            </Button>
            <Button variant="secondary" className="h-8 px-3 text-xs" onClick={() => void openLogDir()}>
              <FolderOpen className="h-3.5 w-3.5" /> 打开日志目录
            </Button>
          </div>
          {logDirMessage && <p className="text-xs text-text-secondary">{logDirMessage}</p>}
          {startupError && (
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-control bg-surface-secondary p-3 font-mono text-[11px] text-text-secondary">
              {startupError}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
