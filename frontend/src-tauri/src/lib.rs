use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 本地服务端口。必须与 backend_sidecar.py 的 DEFAULT_PORT 和前端 API_BASE 保持一致。
///
/// **刻意与姊妹项目 Ling 的 18080 错开**：两个产品可能装在同一台机器上（同一个投放团队），
/// 端口撞了会互相抢后端。
const BACKEND_PORT: u16 = 18081;
/// 等待本地服务就绪的上限。onefile 打包的 sidecar 首次启动要解压上百 MB，
/// 叠加杀毒软件扫描时可能接近一分钟，因此这里给足余量。
const BACKEND_READY_TIMEOUT: Duration = Duration::from_secs(90);
/// 端口被上一次残留进程占用时，等它自行退出的时间（sidecar 自带父进程守护）。
const PORT_RELEASE_TIMEOUT: Duration = Duration::from_secs(15);
/// 前端读取启动失败原因时返回的最大字符数，避免把整份日志塞进界面。
const LOG_READ_LIMIT_BYTES: u64 = 64 * 1024;

#[derive(Default)]
struct BackendProcess(Mutex<Option<CommandChild>>);

// ---------- 日志：桌面壳层（进程启动、端口、退出码）单独落盘 ----------
// sidecar 自己会写 pgy.log；这里记录的是「壳看到的事实」，两者按时间对照即可定位
// 「客户端根本没起来」和「服务起来了但操作失败」这两类完全不同的故障。

fn pgy_data_dir() -> PathBuf {
    // 先认 PGY_DATA_DIR，与 Python 侧（mio_core.ROOT）同一套规则。
    // 不这样做的话开发态会分家：run-dev.bat 把 PGY_DATA_DIR 指到项目根，
    // 后端日志写项目里，而本壳的 desktop.log 写 %LOCALAPPDATA%，
    // 于是「打开日志目录」打开的是个空目录 —— 恰好是开发时最需要它的场合。
    if let Ok(dir) = std::env::var("PGY_DATA_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    let base = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var("APPDATA").ok().filter(|value| !value.is_empty()));
    match base {
        Some(dir) => Path::new(&dir).join("Pgy"),
        None => PathBuf::from("."),
    }
}

fn pgy_log_dir() -> PathBuf {
    pgy_data_dir().join("pgy_logs")
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or(0)
}

/// 追加一行桌面日志。任何失败都忽略：日志写不进去也不能影响客户端启动。
fn shell_log(level: &str, message: &str) {
    let dir = pgy_log_dir();
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("desktop.log"))
    {
        let _ = writeln!(file, "{} {} {}", now_ms(), level, message);
    }
}

/// 把「本地服务起不来」的原因写进 pgy-startup-error.log——界面「查看启动失败原因」
/// 读的就是这个文件（Python 侧端口占用/导入失败也写它）。只写 desktop.log 时，
/// 用户点那个按钮永远得到「没有启动错误记录」，等于没有出路。
fn write_startup_error(message: &str) {
    let dir = pgy_log_dir();
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("pgy-startup-error.log"))
    {
        let _ = writeln!(file, "{} {}", now_ms(), message);
    }
}

fn tail_text(path: &Path, limit: u64) -> Option<String> {
    let metadata = std::fs::metadata(path).ok()?;
    let mut bytes = std::fs::read(path).ok()?;
    if metadata.len() > limit {
        let cut = bytes.len().saturating_sub(limit as usize);
        bytes.drain(..cut);
    }
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

/// 读取桌面壳日志尾部（前端「设置 → 运行日志」展示启动期故障用）。
#[tauri::command]
fn read_desktop_log(limit: Option<u64>) -> Result<String, String> {
    let path = pgy_log_dir().join("desktop.log");
    tail_text(&path, limit.unwrap_or(LOG_READ_LIMIT_BYTES))
        .ok_or_else(|| "尚未生成桌面日志".to_string())
}

/// 读取 sidecar 启动期致命错误（端口占用、导入失败等），让界面能直接显示原因。
#[tauri::command]
fn read_startup_error() -> Result<String, String> {
    let path = pgy_log_dir().join("pgy-startup-error.log");
    tail_text(&path, LOG_READ_LIMIT_BYTES).ok_or_else(|| "没有启动错误记录".to_string())
}

/// 在资源管理器中打开日志目录（用户要发给同事排查时最省事）。
#[tauri::command]
fn open_log_dir() -> Result<String, String> {
    let dir = pgy_log_dir();
    std::fs::create_dir_all(&dir).map_err(|err| format!("无法创建日志目录：{err}"))?;
    #[cfg(windows)]
    {
        std::process::Command::new("explorer")
            .arg(&dir)
            .spawn()
            .map_err(|err| format!("无法打开日志目录：{err}"))?;
    }
    #[cfg(not(windows))]
    {
        let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
        std::process::Command::new(opener)
            .arg(&dir)
            .spawn()
            .map_err(|err| format!("无法打开日志目录：{err}"))?;
    }
    Ok(dir.to_string_lossy().into_owned())
}

/// 在资源管理器中打开一个目录（用于「打开导出目录」按钮）。
///
/// 只接受**已存在的目录**：路径由用户在设置里填，写错时应当报一句人话，
/// 而不是让 explorer 弹一个"找不到"的系统对话框。
#[tauri::command]
fn open_path(path: String) -> Result<String, String> {
    let target = std::path::PathBuf::from(&path);
    if !target.is_dir() {
        return Err(format!("目录不存在或不是目录：{path}"));
    }
    #[cfg(windows)]
    {
        std::process::Command::new("explorer")
            .arg(&target)
            .spawn()
            .map_err(|err| format!("无法打开目录：{err}"))?;
    }
    #[cfg(not(windows))]
    {
        let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
        std::process::Command::new(opener)
            .arg(&target)
            .spawn()
            .map_err(|err| format!("无法打开目录：{err}"))?;
    }
    Ok(target.to_string_lossy().into_owned())
}

fn port_in_use(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&address, Duration::from_millis(300)).is_ok()
}

fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if port_in_use(BACKEND_PORT) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

fn wait_port_free(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !port_in_use(BACKEND_PORT) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    !port_in_use(BACKEND_PORT)
}

/// 结束本地服务本身，**绝不连带结束我们启动的浏览器**。
///
/// 为什么不能用 `taskkill /T`：`/T` 会连子孙进程一起杀，而 RPA 用的浏览器是本地服务
/// 启动的子进程——「关闭客户端不要同时关闭浏览器，浏览器正常登录着就行」，`/T` 会把
/// 浏览器一起关掉（被强杀的浏览器下次启动还会"恢复上次标签页"，把垃圾页也带回来）。
///
/// PyInstaller onefile 是「启动器进程 → 真正的服务进程」两层结构，所以除了启动器本身，
/// 还要按端口找到真正跑 uvicorn 的进程补一枪，否则会留下僵尸进程占着端口，
/// 用户下次打开客户端只会看到「启动超时」。
fn kill_backend(pid: u32) {
    #[cfg(windows)]
    {
        kill_pid(pid);
        for owner in port_owners(BACKEND_PORT) {
            if owner != pid {
                kill_pid(owner);
            }
        }
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
    }
}

#[cfg(windows)]
fn kill_pid(pid: u32) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
}

/// 监听指定端口的进程 PID（用 netstat 取，避免引入额外依赖）。
#[cfg(windows)]
fn port_owners(port: u16) -> Vec<u32> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let output = std::process::Command::new("netstat")
        .args(["-ano", "-p", "tcp"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
    let Ok(output) = output else { return Vec::new() };
    let text = String::from_utf8_lossy(&output.stdout);
    let needle = format!(":{port} ");
    let mut owners = Vec::new();
    for line in text.lines() {
        if !line.contains(&needle) || !line.contains("LISTENING") {
            continue;
        }
        if let Some(last) = line.split_whitespace().last() {
            if let Ok(pid) = last.parse::<u32>() {
                owners.push(pid);
            }
        }
    }
    owners
}

/// 端口被占用时，先问一句「你是不是本程序的后端」：GET /api/health，
/// 响应里带 `service`/`pgy` 这两个只有我们后端才有的字段才认。
///
/// 用 `pgy` 而不是参考项目的 `playwright`：Ling 与本项目可能同机共存，
/// 判据必须能把两者区分开。
fn probe_backend_identity() -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_secs(3)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buffer = Vec::new();
    let _ = stream.take(64 * 1024).read_to_end(&mut buffer);
    let text = String::from_utf8_lossy(&buffer);
    text.contains("service") && text.contains("pgy")
}

fn spawn_backend(app: &tauri::AppHandle) -> Result<(), String> {
    if port_in_use(BACKEND_PORT) {
        shell_log(
            "WARN",
            &format!(
                "端口 {BACKEND_PORT} 已被占用，可能是上一次未退出的本地服务；等待其自行退出…"
            ),
        );
        if !wait_port_free(PORT_RELEASE_TIMEOUT) {
            // 复用前必须先确认端口上跑的**就是本程序的后端**：这个端口也可能被
            // 别的程序（或另一个登录用户的本程序）占着，盲目复用会让界面一直对着
            // 陌生服务报错（换机 / RDP 多用户最容易踩）。
            if probe_backend_identity() {
                shell_log(
                    "WARN",
                    &format!("端口 {BACKEND_PORT} 仍被占用，已确认是本程序的本地服务，本次直接复用。"),
                );
                return Ok(());
            }
            return Err(format!(
                "端口 {BACKEND_PORT} 被其他程序占用（探测到的服务不是本程序后端）；\
                 请关闭占用该端口的程序后重开客户端。"
            ));
        }
    }

    let sidecar = app
        .shell()
        .sidecar("pgy-backend")
        .map_err(|err| format!("无法定位内置服务：{err}"))?;
    let (mut events, child) = sidecar
        .spawn()
        .map_err(|err| format!("无法启动内置服务：{err}"))?;
    shell_log("INFO", &format!("已启动本地服务进程 pid={} ", child.pid()));
    *app.state::<BackendProcess>().0.lock().unwrap() = Some(child);

    // sidecar 的 stdout/stderr 在打包后没有任何窗口可看，必须转存到文件：
    // Python 侧的启动失败（缺模块、端口占用）最先就出现在这里。
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line);
                    let text = text.trim_end();
                    if !text.is_empty() {
                        shell_log("SIDECAR", text);
                    }
                }
                CommandEvent::Error(error) => shell_log("ERROR", &format!("本地服务错误：{error}")),
                CommandEvent::Terminated(payload) => {
                    shell_log("ERROR", &format!("本地服务进程已退出：{payload:?}"));
                    return;
                }
                _ => {}
            }
        }
    });

    std::thread::spawn(|| {
        if wait_for_backend(BACKEND_READY_TIMEOUT) {
            shell_log("INFO", "本地服务已就绪（127.0.0.1:18081）");
        } else {
            shell_log(
                "ERROR",
                "本地服务在限定时间内未就绪；请查看同目录的 pgy-startup-error.log 与 pgy.log。",
            );
        }
    });
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // 单实例必须最先注册：启动慢时反复双击，第二个进程只聚焦已有窗口，
        // 不会另起一个客户端去争同一个后端端口、也不会出现「关掉一个另一个断线」。
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            shell_log("INFO", "检测到重复启动：聚焦已有窗口");
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(BackendProcess::default())
        .setup(|app| {
            shell_log("INFO", "客户端启动");
            // 后端未就绪时也必须把窗口显示出来：界面自己有「正在连接本地服务」的提示
            // 与失败原因（含日志路径），比一味隐藏窗口、让用户以为程序没反应安全得多。
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
            }
            if let Err(error) = spawn_backend(app.handle()) {
                shell_log("ERROR", &format!("本地服务启动失败：{error}"));
                write_startup_error(&format!("本地服务启动失败：{error}"));
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            read_desktop_log,
            read_startup_error,
            open_log_dir,
            open_path
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            if let Ok(mut process) = handle.state::<BackendProcess>().0.lock() {
                if let Some(child) = process.take() {
                    kill_backend(child.pid());
                    shell_log(
                        "INFO",
                        "客户端退出，已结束后台服务进程（浏览器保持运行，登录态保留）",
                    );
                }
            }
        }
    });
}
