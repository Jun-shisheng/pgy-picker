; Pgy data is user-scoped and is not removed by the default NSIS uninstaller.
; This product's install/upgrade flow is explicitly clean-install: stop the
; bundled backend first, then remove the complete local data directory.
;
; 注意：taskkill **不带 /T**——受管浏览器是本服务的子进程，/T 会把浏览器窗口
; 一起带走，违反「关客户端不动浏览器」的边界。
;
; 凭据清理匹配 `pgy://` 前缀：这是本产品在系统凭据库里的命名空间
; （形如 `pgy://pugongying/account/<id>`），换平台也不影响。
;
; 注意：Pgy 与姊妹项目 Ling 可能同机共存，这里**只**动 pgy 自己的东西
; （pgy-backend / pgy:// 凭据 / Pgy 数据目录），绝不碰 Ling 的进程、凭据或数据。
;
; 注意：本文件**没有 BOM**，NSIS 会按 ANSI(GBK) 读它 —— 所以下面
; **只能出现 ASCII 路径**。写中文路径会被解错，指向另一个文件夹且毫无提示。
; 要改中文名，请先把本文件转成 UTF-8 with BOM 并真格跑一次升级安装验证。

; ──────────────────────────────────────────────────────────────────────────
; 交付物保留（用户 2026-10-12 定：Excel 放在安装目录下的 exports/）
;
; `%LOCALAPPDATA%\Pgy` **既是数据目录也是安装目录**（NSIS 是 currentUser 模式），
; 而下面的清理会 `RMDir /r` 整个目录 —— 用户已交付的 Excel 就在 `exports/` 里，
; 不能跟着一起没。所以清理前后把它**挪出去再挪回来**。
;
; 用 `move` 而不是 robocopy：整棵目录树的一次搬迁，同卷时是元数据级操作，
; 不会出现"搬了一半"的中间态（robocopy /MOVE 中途失败会留下两边各一半）。
; 路径必须与 `backend/services/settings_service.py` 的 EXPORT_DIR_NAME 一致。
; ──────────────────────────────────────────────────────────────────────────
!macro PGY_STASH_EXPORTS
  nsExec::ExecToLog 'cmd /c rmdir /s /q "%TEMP%\pgy-exports-stash"'
  nsExec::ExecToLog 'cmd /c if exist "$LOCALAPPDATA\Pgy\exports" move "$LOCALAPPDATA\Pgy\exports" "%TEMP%\pgy-exports-stash"'
!macroend

!macro PGY_RESTORE_EXPORTS
  nsExec::ExecToLog 'cmd /c if not exist "$LOCALAPPDATA\Pgy" mkdir "$LOCALAPPDATA\Pgy"'
  nsExec::ExecToLog 'cmd /c if exist "%TEMP%\pgy-exports-stash" move "%TEMP%\pgy-exports-stash" "$LOCALAPPDATA\Pgy\exports"'
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro PGY_STASH_EXPORTS
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend-x86_64-pc-windows-msvc.exe'
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend.exe'
  nsExec::ExecToLog 'cmd /c "for /f ""tokens=2,* delims=="" %A in (''cmdkey /list ^| findstr /i ""pgy://""'') do cmdkey /delete:%B"'
  RMDir /r "$LOCALAPPDATA\Pgy"
  RMDir /r "$APPDATA\Pgy"
  !insertmacro PGY_RESTORE_EXPORTS
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro PGY_STASH_EXPORTS
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend-x86_64-pc-windows-msvc.exe'
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend.exe'
  nsExec::ExecToLog 'cmd /c "for /f ""tokens=2,* delims=="" %A in (''cmdkey /list ^| findstr /i ""pgy://""'') do cmdkey /delete:%B"'
  RMDir /r "$LOCALAPPDATA\Pgy"
  RMDir /r "$APPDATA\Pgy"
  !insertmacro PGY_RESTORE_EXPORTS
!macroend
