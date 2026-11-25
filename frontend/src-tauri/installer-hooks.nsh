; Pgy data is user-scoped and is not removed by the default NSIS uninstaller.
; This product's install/upgrade flow is explicitly clean-install: stop the
; bundled backend first, then remove the complete local data directory.
;
; 注意：taskkill **不带 /T**——受管浏览器是本服务的子进程，/T 会把浏览器窗口
; 一起带走，违反「关客户端不动浏览器」的边界。
;
; 当前账户只使用 `pgy://pugongying/account/default`，安装/卸载时精确清理。
;
; 注意：同机可能还跑着别的本地应用，这里**只**动本产品自己的东西
; （pgy-backend / pgy:// 凭据 / Pgy 数据目录），绝不碰其它应用的进程、凭据或数据。
;
; 注意：本文件**没有 BOM**，NSIS 会按 ANSI(GBK) 读它 —— 所以下面
; **只能出现 ASCII 路径**。写中文路径会被解错，指向另一个文件夹且毫无提示。
; 要改中文名，请先把本文件转成 UTF-8 with BOM 并真格跑一次升级安装验证。

; ──────────────────────────────────────────────────────────────────────────
; 用户文件的保留：交付物 + 报价档位表
;
; `%LOCALAPPDATA%\Pgy` **既是数据目录也是安装目录**（NSIS 是 currentUser 模式），
; 而下面的清理会 `RMDir /r` 整个目录 —— 用户自己放进来的这两样不能跟着一起没：
;
;   · `exports\`      —— 已交付的 Excel（用户 2026-10-12 定：Excel 放在安装目录下）；
;   · `pricing.json`  —— 报价档位表（商业敏感、不入库，只能由使用者自己放；
;                        查找位置见 `backend/providers/pugongying/pricing.py`）。
;   · `selection-cursor.json` —— 「换一批」翻页游标（丢了就会每次从第 1 页开始，
;                        两次「开始检索」可能拿到一模一样的名单）；
;   · `notes-cache.json`   —— 近10篇笔记点赞中位数缓存（丢了会重新打开蒲公英详情页）。
;      ⚠️ **只保 `pricing.json` 这个 ASCII 路径**：本文件按 ANSI(GBK) 读取、
;      **路径只允许 ASCII**（见文件顶部）。所以装机后请把档位表放成
;      `%LOCALAPPDATA%\Pgy\pricing.json`，升级才不会丢。
;
; 用 `move` 而不是 robocopy：整棵目录树的一次搬迁，同卷时是元数据级操作，
; 不会出现"搬了一半"的中间态（robocopy /MOVE 中途失败会留下两边各一半）。
; `exports` 的路径必须与 `backend/services/settings_service.py` 的 EXPORT_DIR_NAME 一致。
;
; ⚠️ 还原之后**不要**去 rmdir 暂存目录：万一哪一步 move 失败，那里就是用户唯一的一份
; 数据（下一次安装的开头会清掉它，但那时至少已经过了一轮）。
; ──────────────────────────────────────────────────────────────────────────
!macro PGY_STASH_USER_FILES
  nsExec::ExecToLog 'cmd /c rmdir /s /q "%TEMP%\pgy-keep-stash"'
  nsExec::ExecToLog 'cmd /c mkdir "%TEMP%\pgy-keep-stash"'
  nsExec::ExecToLog 'cmd /c if exist "$LOCALAPPDATA\Pgy\exports" move "$LOCALAPPDATA\Pgy\exports" "%TEMP%\pgy-keep-stash\exports"'
  nsExec::ExecToLog 'cmd /c if exist "$LOCALAPPDATA\Pgy\pricing.json" move "$LOCALAPPDATA\Pgy\pricing.json" "%TEMP%\pgy-keep-stash\pricing.json"'
  nsExec::ExecToLog 'cmd /c if exist "$LOCALAPPDATA\Pgy\selection-cursor.json" move "$LOCALAPPDATA\Pgy\selection-cursor.json" "%TEMP%\pgy-keep-stash\selection-cursor.json"'
  nsExec::ExecToLog 'cmd /c if exist "$LOCALAPPDATA\Pgy\notes-cache.json" move "$LOCALAPPDATA\Pgy\notes-cache.json" "%TEMP%\pgy-keep-stash\notes-cache.json"'
!macroend

!macro PGY_RESTORE_USER_FILES
  nsExec::ExecToLog 'cmd /c if not exist "$LOCALAPPDATA\Pgy" mkdir "$LOCALAPPDATA\Pgy"'
  nsExec::ExecToLog 'cmd /c if exist "%TEMP%\pgy-keep-stash\exports" move "%TEMP%\pgy-keep-stash\exports" "$LOCALAPPDATA\Pgy\exports"'
  nsExec::ExecToLog 'cmd /c if exist "%TEMP%\pgy-keep-stash\pricing.json" move "%TEMP%\pgy-keep-stash\pricing.json" "$LOCALAPPDATA\Pgy\pricing.json"'
  nsExec::ExecToLog 'cmd /c if exist "%TEMP%\pgy-keep-stash\selection-cursor.json" move "%TEMP%\pgy-keep-stash\selection-cursor.json" "$LOCALAPPDATA\Pgy\selection-cursor.json"'
  nsExec::ExecToLog 'cmd /c if exist "%TEMP%\pgy-keep-stash\notes-cache.json" move "%TEMP%\pgy-keep-stash\notes-cache.json" "$LOCALAPPDATA\Pgy\notes-cache.json"'
!macroend

!macro PGY_CLEAR_BROWSER_PROFILE
  RMDir /r "$LOCALAPPDATA\Pgy\profiles"
  IfFileExists "$LOCALAPPDATA\Pgy\profiles" 0 +3
    MessageBox MB_OK|MB_ICONSTOP "Close the Pgy browser and retry setup. Its profile is still in use."
    Abort
!macroend

!macro NSIS_HOOK_PREINSTALL
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend-x86_64-pc-windows-msvc.exe'
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend.exe'
  !insertmacro PGY_CLEAR_BROWSER_PROFILE
  !insertmacro PGY_STASH_USER_FILES
  nsExec::ExecToLog 'cmdkey.exe /delete:pgy://pugongying/account/default'
  RMDir /r "$LOCALAPPDATA\Pgy"
  RMDir /r "$APPDATA\Pgy"
  !insertmacro PGY_RESTORE_USER_FILES
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend-x86_64-pc-windows-msvc.exe'
  nsExec::ExecToLog 'taskkill /F /IM pgy-backend.exe'
  !insertmacro PGY_CLEAR_BROWSER_PROFILE
  !insertmacro PGY_STASH_USER_FILES
  nsExec::ExecToLog 'cmdkey.exe /delete:pgy://pugongying/account/default'
  RMDir /r "$LOCALAPPDATA\Pgy"
  RMDir /r "$APPDATA\Pgy"
  !insertmacro PGY_RESTORE_USER_FILES
!macroend
