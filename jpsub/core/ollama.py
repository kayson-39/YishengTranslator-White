# -*- coding: utf-8 -*-
"""Ollama 本地大模型的检测、模型管理、下载与测速。"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from .http_util import make_client

DEFAULT_BASE = "http://127.0.0.1:11434"

# 推荐模型：(名称, 说明, 大约体积GB, 速度标签)
RECOMMENDED: list[tuple[str, str, float, str]] = [
    ("translategemma:4b", "翻译专用 4B（Google，日语中文质量最好）", 3.3, "推荐"),
    ("qwen2.5:3b-instruct", "通用 3B（速度快，质量不错）", 1.9, "快"),
    ("qwen2.5:1.5b-instruct", "通用 1.5B（最快，质量一般）", 1.0, "极速"),
]

ProgressCb = Optional[Callable[[str, int, int], None]]


def normalize_base(base_url: str) -> str:
    """把用户填的（可能带 /v1 的）地址变成 Ollama 原生 API 根地址。"""
    base = (base_url or DEFAULT_BASE).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or DEFAULT_BASE


def find_binary() -> str | None:
    hit = shutil.which("ollama")
    if hit:
        return hit
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "ollama.exe",
        Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
    ]
    for p in cands:
        if p and p.exists():
            return str(p)
    return None


def ping(base_url: str = "", timeout: float = 2.0) -> bool:
    base = normalize_base(base_url)
    try:
        with make_client(base, timeout=timeout) as c:
            r = c.get(f"{base}/api/tags")
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def list_models(base_url: str = "", timeout: float = 5.0) -> list[dict]:
    """返回 [{name, size, size_gb, param_hint}], 按体积升序。"""
    base = normalize_base(base_url)
    with make_client(base, timeout=timeout) as c:
        r = c.get(f"{base}/api/tags")
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        size = int(m.get("size") or 0)
        name = str(m.get("name") or m.get("model") or "")
        out.append({
            "name": name,
            "size": size,
            "size_gb": round(size / (1024 ** 3), 1) if size else 0.0,
            "family": str((m.get("details") or {}).get("family") or ""),
        })
    out.sort(key=lambda x: x["size"])
    return out


def speed_tag(size_gb: float) -> str:
    """按体积给一个粗略速度标签。"""
    if size_gb <= 4.5:
        return "快 ⚡"
    if size_gb <= 8:
        return "中等"
    if size_gb <= 13:
        return "较慢 ⚠"
    return "很慢 ⚠⚠"


def pull_model(model: str, base_url: str = "", on_progress: ProgressCb = None,
               timeout: float = 3600.0) -> None:
    """下载（拉取）模型，流式汇报进度。"""
    base = normalize_base(base_url)
    with make_client(base, timeout=timeout) as client:
        with client.stream("POST", f"{base}/api/pull", json={"model": model, "stream": True}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                status = str(obj.get("status") or "")
                done = int(obj.get("completed") or 0)
                total = int(obj.get("total") or 0)
                if on_progress:
                    on_progress(status, done, total)
                if obj.get("error"):
                    raise RuntimeError(str(obj["error"]))


def chat_once(model: str, text: str, base_url: str = "", timeout: float = 120.0) -> dict:
    """跑一次翻译，返回 {text, seconds, eval_count, eval_duration, tps}。"""
    base = normalize_base(base_url)
    system = ("你是专业的日语→简体中文翻译。只输出简体中文译文，不要解释。")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "keep_alive": "60m",
        "options": {"temperature": 0.2, "num_predict": 400},
    }
    if "qwen3" in model.lower():
        payload["think"] = False
    t0 = time.perf_counter()
    with make_client(base, timeout=timeout) as c:
        r = c.post(f"{base}/api/chat", json=payload)
    r.raise_for_status()
    obj = r.json()
    dt = time.perf_counter() - t0
    eval_count = int(obj.get("eval_count") or 0)
    eval_ns = int(obj.get("eval_duration") or 0)
    tps = (eval_count / (eval_ns / 1e9)) if eval_ns else 0.0
    return {
        "text": str((obj.get("message") or {}).get("content") or "").strip(),
        "seconds": dt,
        "eval_count": eval_count,
        "tps": round(tps, 1),
    }
