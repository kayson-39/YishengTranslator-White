# -*- coding: utf-8 -*-
"""内置极速本地翻译：Opus-MT / Argos 模型（CTranslate2 + SentencePiece）。

完全离线、不需要 Ollama、不需要 API Key；模型从 Argos 官方索引下载（约 100MB/语言对），
日语→中文没有直连包时自动用 日语→英语→中文 两段式。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from ..paths import MODELS

ARGOS_ROOT = MODELS / "argos"
INDEX_URLS = [
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://data.argosopentech.com/argospm/index.json",
]
CACHE_FILE = ARGOS_ROOT / "index.json"
CACHE_TTL = 7 * 86400

ProgressCb = Optional[Callable[[str, int, int], None]]  # (状态文本, 已完成字节, 总字节)


def _pkg_dir(fr: str, to: str) -> Path:
    return ARGOS_ROOT / f"{fr}_{to}"


def package_root(fr: str, to: str) -> Path:
    """定位包的真实目录（兼容 .argosmodel 内部各种嵌套命名）。"""
    d = _pkg_dir(fr, to)
    if (d / "sentencepiece.model").exists():
        return d
    if d.exists():
        for sub in sorted(p for p in d.iterdir() if p.is_dir()):
            if (sub / "sentencepiece.model").exists():
                return sub
    return d


def installed(fr: str, to: str) -> bool:
    d = package_root(fr, to)
    return d.exists() and (d / "sentencepiece.model").exists() and (d / "model").is_dir()


def load_spm(path: Path):
    """加载 SentencePiece 分词模型。

    这里必须自己把文件读进内存再解析，不能把路径直接交给 sentencepiece：它在 Windows
    上是按窄字节路径开文件的，软件装在带中文的目录里（例如「日V实时翻译助手」）会报
    NOT_FOUND，整条内置翻译链路就直接废掉（只能悄悄降级到联网翻译）。
    """
    import sentencepiece

    sp = sentencepiece.SentencePieceProcessor()
    ok = False
    from_proto = getattr(sp, "LoadFromSerializedProto", None)
    if from_proto is not None:
        try:
            ok = bool(from_proto(path.read_bytes()))
        except Exception:  # noqa: BLE001
            ok = False
    if not ok:                  # 老版本没有这个接口时退回按路径加载（纯英文路径仍可用）
        sp.load(str(path))
    return sp


def fetch_index(timeout: float = 30.0, use_cache: bool = True) -> list[dict]:
    """获取模型索引（带本地缓存与多镜像重试）。"""
    if use_cache and CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
    last_err: Exception | None = None
    for url in INDEX_URLS:
        for _attempt in range(2):
            try:
                r = httpx.get(url, timeout=timeout, follow_redirects=True)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list) and data:
                    ARGOS_ROOT.mkdir(parents=True, exist_ok=True)
                    CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
                    return data
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.0)
    if CACHE_FILE.exists():  # 兜底：过期缓存也能用
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError(f"无法获取翻译模型索引：{last_err}")


def find_package(index: list[dict], fr: str, to: str) -> dict | None:
    for p in index:
        if p.get("from_code") == fr and p.get("to_code") == to:
            return p
    return None


def download_package(fr: str, to: str, on_progress: ProgressCb = None) -> Path:
    """下载并解压一个语言对模型（支持断点续传与自动重试），返回模型目录。"""
    index = fetch_index()
    pkg = find_package(index, fr, to)
    if pkg is None:
        raise RuntimeError(f"官方索引里没有 {fr}→{to} 的模型包")
    url = (pkg.get("links") or [""])[0]
    if not url:
        raise RuntimeError(f"{fr}→{to} 模型包缺少下载地址")
    total_hint = int(pkg.get("size") or 0)
    ARGOS_ROOT.mkdir(parents=True, exist_ok=True)
    dest = _pkg_dir(fr, to)
    part = ARGOS_ROOT / f"{fr}_{to}.argosmodel.part"
    timeout = httpx.Timeout(connect=20.0, read=45.0, write=20.0, pool=20.0)
    last_err: Exception | None = None
    got_complete = False

    if on_progress:
        on_progress(f"下载 {fr}→{to} 模型…", part.stat().st_size if part.exists() else 0, total_hint)

    for attempt in range(6):
        try:
            already = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={already}-"} if already else {}
            with httpx.stream("GET", url, headers=headers, timeout=timeout,
                              follow_redirects=True) as r:
                if r.status_code == 416 and already:  # 已下完但校验失败 → 重下
                    part.unlink(missing_ok=True)
                    continue
                if already and r.status_code == 200:  # 服务端不支持断点 → 从头下
                    already = 0
                if r.status_code not in (200, 206):
                    r.raise_for_status()
                remaining = int(r.headers.get("content-length") or 0)
                total = (already + remaining) if r.status_code == 206 else (remaining or total_hint)
                done = already
                mode = "ab" if r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        if on_progress and (done % (1 << 20) < (1 << 16)):
                            on_progress(f"下载 {fr}→{to} 模型…", done, total)
            size = part.stat().st_size if part.exists() else 0
            if total and size < total:
                if on_progress:
                    on_progress(f"连接中断，正在断点续传（已完成 {size // 1048576}MB）…", size, total)
                time.sleep(1.0)
                continue
            got_complete = True
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5)
            continue

    if not got_complete:
        raise RuntimeError(f"下载 {fr}→{to} 模型失败：{last_err}")
    if on_progress:
        on_progress(f"解压 {fr}→{to} 模型…", 1, 1)
    try:
        with zipfile.ZipFile(part) as z:
            z.extractall(dest)
    except zipfile.BadZipFile as e:
        part.unlink(missing_ok=True)
        raise RuntimeError("下载的模型文件损坏，请重新点一次下载按钮") from e
    part.unlink(missing_ok=True)
    if not installed(fr, to):
        raise RuntimeError(f"{fr}→{to} 模型解压后不完整")
    return dest


def ensure_pair(fr: str = "ja", to: str = "zh", on_progress: ProgressCb = None) -> list[tuple[str, str]]:
    """确保有可用的翻译链；返回 [(fr,to), ...] 1~2 段。"""
    if not installed(fr, to):
        index = fetch_index()
        if find_package(index, fr, to):
            download_package(fr, to, on_progress)
            return [(fr, to)]
        # 无直连包 → 走英语中转
        for a, b in ((fr, "en"), ("en", to)):
            if not installed(a, b):
                download_package(a, b, on_progress)
        return [(fr, "en"), ("en", to)]
    return [(fr, to)]


_SHARED: dict[tuple[str, str], LocalMT] = {}
_SHARED_LOCK = threading.Lock()


def shared(fr: str = "ja", to: str = "zh") -> LocalMT:
    """全进程共用一份内置翻译模型（多个线程同时翻译也只加载一次）。"""
    key = (fr, to)
    with _SHARED_LOCK:
        inst = _SHARED.get(key)
        if inst is None:
            inst = LocalMT(fr, to)
            _SHARED[key] = inst
        return inst


class LocalMT:
    """调用 CTranslate2 + SentencePiece 跑 Opus-MT 系模型。"""

    def __init__(self, fr: str = "ja", to: str = "zh"):
        self.fr, self.to = fr, to
        self._chain: list[dict] = []
        self._load_lock = threading.Lock()
        self._trans_lock = threading.Lock()

    # ------------------------------------------------------------------
    def available(self) -> bool:
        if installed(self.fr, self.to):
            return True
        return installed(self.fr, "en") and installed("en", self.to)

    def load(self) -> None:
        if self._chain:
            return
        with self._load_lock:      # 多线程同时用只加载一次，否则内存会多出一份模型
            if self._chain:
                return
            import ctranslate2

            pairs: list[tuple[str, str]]
            if installed(self.fr, self.to):
                pairs = [(self.fr, self.to)]
            elif installed(self.fr, "en") and installed("en", self.to):
                pairs = [(self.fr, "en"), ("en", self.to)]
            else:
                raise RuntimeError("内置翻译模型尚未下载（设置 → 翻译服务 → 一键下载内置模型）")
            # 本地大模型/识别已经在抢 CPU，翻译这边留 2~4 个线程就够，避免线程打到超配
            threads = max(2, min(4, (os.cpu_count() or 4) // 4))
            chain = []
            for fr, to in pairs:
                d = package_root(fr, to)
                chain.append({
                    "fr": fr, "to": to,
                    "translator": ctranslate2.Translator(str(d / "model"), device="cpu",
                                                          compute_type="int8", inter_threads=1,
                                                          intra_threads=threads),
                    "spm": load_spm(d / "sentencepiece.model"),
                })
            self._chain = chain

    def translate(self, text: str, on_delta: Optional[Callable[[str], None]] = None) -> str:
        self.load()
        with self._trans_lock:
            out = text.strip()
            for hop in self._chain:
                spm = hop["spm"]
                tokens = spm.encode(out, out_type=str)
                if not tokens:
                    continue
                result = hop["translator"].translate_batch(
                    [tokens], beam_size=2, max_batch_size=8, max_input_length=256,
                )
                pieces = result[0].hypotheses[0]
                out = spm.decode(pieces).replace("\u2581", " ")
                out = out.replace("\u25b1", " ").strip()
            # \u6e05\u7406\uff1a\u53bb\u6389\u4e2d\u6587\u5b57\u7b26\u4e4b\u95f4\u591a\u4f59\u7684\u7a7a\u683c
            out = re.sub(r"\s+", " ", out).strip()
            out = re.sub(r"(?<=[\u4e00-\u9fff\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a\uff08\uff09])\s+(?=[\u4e00-\u9fff\uff0c\u3002\uff01\uff1f\u3001\uff1b\uff1a\uff08\uff09])", "", out)
            out = out.replace(" ,", "\uff0c").replace(" .", "\u3002").replace(" !", "\uff01").replace(" ?", "\uff1f")
        if on_delta:
            on_delta(out)
        return out

    def test(self, text: str = "こんにちは、今日も配信を見に来てくれてありがとう！") -> tuple[str, float]:
        t0 = time.perf_counter()
        out = self.translate(text)
        return out, time.perf_counter() - t0
