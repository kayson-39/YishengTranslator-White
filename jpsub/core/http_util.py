# -*- coding: utf-8 -*-
"""HTTP 客户端工具：本地地址自动绕过系统代理（避免 Clash 等代理拦截 127.0.0.1）。"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_UNSET = object()
_SYS_PROXY: object = _UNSET


def system_proxy() -> str | None:
    """读 Windows 的「系统代理」（Clash / v2ray 之类），没有就返回 None。

    有些机器上局域网直连 GitHub 很慢、开代理反而快很多，所以下载引擎时优先用它。
    """
    global _SYS_PROXY
    if _SYS_PROXY is _UNSET:
        value: str | None = None
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
                if enable:
                    server, _ = winreg.QueryValueEx(k, "ProxyServer")
                    server = str(server or "").strip()
                    if "=" in server:      # 形如 http=127.0.0.1:7897;https=127.0.0.1:7897
                        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
                        server = (parts.get("https") or parts.get("http") or "").strip()
                    if server and not server.startswith("http"):
                        server = "http://" + server
                    value = server or None
        except Exception:  # noqa: BLE001
            value = None
        if not value:
            value = _env_proxy() or _probe_local_proxy()
        _SYS_PROXY = value
    return _SYS_PROXY if isinstance(_SYS_PROXY, str) else None


def _env_proxy() -> str | None:
    import os

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        v = str(os.environ.get(name) or "").strip()
        if v:
            return v if v.startswith("http") else "http://" + v
    return None


def _probe_local_proxy() -> str | None:
    """很多代理软件（Clash / v2rayN 等）只在本地监听端口、不写系统代理，按常见端口试一下。"""
    import socket

    for port in (7897, 7890, 7891, 10809, 10808, 1080, 2080):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://127.0.0.1:{port}"
        except Exception:  # noqa: BLE001
            continue
    return None


def is_local_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in _LOCAL_HOSTS or host.startswith("127.")


def make_client(url: str = "", timeout=30.0, **kwargs) -> httpx.Client:
    """创建 httpx 客户端；本地地址不使用环境代理，远程地址照常使用。"""
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(timeout=timeout, trust_env=not is_local_url(url), **kwargs)
