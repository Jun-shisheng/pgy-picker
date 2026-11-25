"""受管浏览器的静态资源本地缓存：平台前端包全机只下载一次。

为什么需要它：合作伙伴平台的登录页/控制台由平台自己的 JS 包渲染
（``fe-static.xhscdn.com/formula-static/.../index.<hash>.js`` 等，单文件可达 5 MB+）。
真机实测该 CDN 到本机只有 ~100 KB/s，冷 Profile 首次打开登录页要 12–75 秒；
且每个账号一个独立 Profile，新建账号 / 退出后重登都要重下一次。这里把带内容哈希的
静态资源缓存到数据目录，所有账号、所有 Profile 共享，之后登录页约 2 秒可交互。

边界条件（安全规则，宁可少缓存不能缓存错）：
- 只拦 ``*.xhscdn.com`` 上「文件名带内容哈希」的 GET 静态资源（``xxx.<8+位hex>.js``）；
  非哈希命名的资源可能是按会话下发的，绝不缓存；
- 只缓存 200 且响应体非空的完整响应；带 Range 的请求直接放行（不缓存、不复用）；
- 只回放 content-type 等元数据，绝不回放 content-encoding/content-length——
  Playwright 返回的是已解压的响应体，回放压缩头会导致二次解码；
- 任何异常都退回正常网络路径，缓存层不得让页面加载失败。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.core.pgy_core import ROOT

# 只有带内容哈希的静态资源才缓存：URL 内容不可变，缓存永远不会过期失效。
_HASHED_ASSET = re.compile(r"\.[0-9a-fA-F]{8,}\.(?:js|css|woff2?|ttf|otf|png|jpe?g|webp|gif|svg)$")
_CACHEABLE_HEADERS = ("content-type", "cache-control", "etag", "last-modified")
_DEFAULT_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB：平台全量前端包远小于这个量级
_PRUNE_STOP_RATIO = 0.8
# 缓存保鲜上限：内容哈希命名的资源不会变，只作为「平台万一复用 URL」的兜底。
_MAX_AGE_SECONDS = 60 * 24 * 3600
# 落盘用的临时文件只在一次写入里活几分之一秒；超过这个年纪的残file一定是被中断
# 留下的（进程被杀 / 替换失败），装缓存时顺手清掉。
_STALE_TEMP_SECONDS = 600


class AssetCache:
    """磁盘缓存 + Playwright 路由拦截；线程内单实例即可（按目录复用）。"""

    def __init__(self, root: Path | None = None, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root) if root is not None else ROOT / "asset-cache"
        self.max_bytes = max_bytes
        self._log = logging.getLogger(__name__)
        self._temp_seq = itertools.count()

    @staticmethod
    def _cacheable(url: str, method: str, headers: dict[str, str]) -> bool:
        if method.upper() != "GET":
            return False
        if any(name.lower() == "range" for name in headers):
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != "fe-static.xhscdn.com" and not host.endswith(".xhscdn.com"):
            return False
        return bool(_HASHED_ASSET.search(parsed.path))

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        data = self.root / key[:2] / f"{key}.bin"
        return data, data.with_suffix(".json")

    def install(self, context: Any) -> None:
        """在浏览器上下文上挂拦截；失败只记日志，不影响登录本身。"""
        try:
            context.route("https://*.xhscdn.com/**", self._handle)
            self._prune_async()
        except Exception as exc:  # pragma: no cover - 仅在异常环境触发
            self._log.warning("静态资源缓存未启用（%s）", type(exc).__name__)

    def _handle(self, route: Any, request: Any) -> None:
        try:
            if not self._cacheable(request.url, request.method, dict(request.headers)):
                route.continue_()
                return
            data_path, meta_path = self._paths(request.url)
            if data_path.exists() and self._is_fresh(data_path):
                headers = self._cached_headers(meta_path)
                if headers is None:
                    # 元数据没了/坏了：回放一个没有 content-type 的响应可能让页面加载失败，
                    # 按本模块「宁可少缓存不能缓存错」的规矩丢掉它、退回网络路径。
                    self._forget(data_path, meta_path)
                else:
                    body = data_path.read_bytes()
                    try:  # 命中即刷新 mtime，配合容量裁剪实现 LRU
                        os.utime(data_path, None)
                    except OSError:
                        pass
                    route.fulfill(status=200, body=body, headers=headers)
                    return
            response = route.fetch()
            body = response.body()
            if response.status == 200 and body:
                self._store(data_path, meta_path, response.headers, body)
            route.fulfill(response=response)
        except Exception as exc:
            self._log.info("静态资源缓存回退网络路径：%s", type(exc).__name__)
            try:
                route.continue_()
            except Exception:
                pass

    def _is_fresh(self, data_path: Path) -> bool:
        """哈希命名的资源理论上永不过期；这里只做一个很宽的上限兜底。"""
        try:
            return (time.time() - data_path.stat().st_mtime) < _MAX_AGE_SECONDS
        except OSError:
            return False

    @staticmethod
    def _cached_headers(meta_path: Path) -> dict[str, str] | None:
        """读回放用的响应头；读不到返回 None（调用方据此退回网络路径）。"""
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        headers = payload.get("headers") if isinstance(payload, dict) else None
        return headers if isinstance(headers, dict) else None

    @staticmethod
    def _forget(data_path: Path, meta_path: Path) -> None:
        for path in (data_path, meta_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _temp_path(self, data_path: Path) -> Path:
        """每次写入用独立临时名。

        真机 2026-09-24 09:58 的 `静态资源写入缓存失败：PermissionError` 就是栽在
        共用临时名上：两个会话（多账户/多上下文）同时对同一个 URL 落盘时，一个还在写
        `.part`、另一个已经拿它去 `os.replace`，Windows 上就是"文件被占用"。
        带进程号 + 线程号 + 序号之后，互相之间不再可能撞上。
        """
        return data_path.with_name(
            f"{data_path.name}.{os.getpid()}-{threading.get_ident()}-{next(self._temp_seq)}.part")

    def _store(self, data_path: Path, meta_path: Path, headers: dict[str, str], body: bytes) -> None:
        tmp = self._temp_path(data_path)
        try:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(body)
            self._replace(tmp, data_path)
        except OSError as exc:
            # 写不进缓存只是少一次加速，绝不影响页面加载；但把位置和系统错误码留住，
            # 否则事后只看到一个光秃秃的 PermissionError，无从判断卡在哪一步。
            self._log.info("静态资源写入缓存失败：%s winerror=%s tmp=%s",
                           type(exc).__name__, getattr(exc, "winerror", None), tmp.name)
            return
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            kept = {name: value for name, value in headers.items() if name.lower() in _CACHEABLE_HEADERS}
            meta_path.write_text(json.dumps({"headers": kept, "url": data_path.stem}, ensure_ascii=False),
                                 encoding="utf-8")
        except OSError as exc:
            # 没有元数据就回放不出 content-type，命中路径会按上面的规矩把它丢掉重下。
            self._log.info("静态资源缓存元数据写入失败：%s", type(exc).__name__)

    def _replace(self, tmp: Path, data_path: Path) -> None:
        """原子替换，带重试。

        杀毒/索引器/另一个会话可能正好短暂持有目标文件，这时 Windows 的
        `os.replace` 抛 PermissionError；等几十毫秒再试一次通常就成了——真机 09:58
        那次直接放弃，磁盘上留下一个 4.5MB 的 `.part` 残file。
        """
        for attempt in range(4):
            try:
                os.replace(tmp, data_path)
                return
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _sweep_temp_files(self) -> None:
        """清掉历史残file：`_store` 现在一定会自己删临时文件，留下的只可能是
        进程被杀那种中断——所以只清超过 10 分钟没动过的，绝不碰正在写的那一个。"""
        cutoff = time.time() - _STALE_TEMP_SECONDS
        for path in self.root.rglob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _prune_async(self) -> None:
        """容量裁剪放在后台线程：拦截路径上不做目录扫描。"""
        import threading

        def prune() -> None:
            try:
                self._sweep_temp_files()
                files = [p for p in self.root.rglob("*.bin")]
                entries = []
                total = 0
                for path in files:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    entries.append((stat.st_mtime, stat.st_size, path))
                    total += stat.st_size
                if total <= self.max_bytes:
                    return
                entries.sort()
                for _, size, path in entries:
                    if total <= self.max_bytes * _PRUNE_STOP_RATIO:
                        break
                    try:
                        path.unlink(missing_ok=True)
                        path.with_suffix(".json").unlink(missing_ok=True)
                        total -= size
                    except OSError:
                        continue
            except Exception as exc:  # pragma: no cover
                self._log.info("静态资源缓存裁剪跳过：%s", type(exc).__name__)

        threading.Thread(target=prune, name="pgy-asset-cache-prune", daemon=True).start()

    def stats(self) -> dict:
        files = list(self.root.rglob("*.bin")) if self.root.exists() else []
        total = 0
        newest = 0.0
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            newest = max(newest, stat.st_mtime)
        return {"files": len(files), "bytes": total,
                "newest_age_seconds": round(time.time() - newest, 1) if newest else None}
