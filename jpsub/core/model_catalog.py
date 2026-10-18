# -*- coding: utf-8 -*-
"""本地模型清单与下载（安装脚本 scripts/download_models.py 和设置面板共用一份）。

默认走 hf-mirror.com 国内镜像；可用环境变量 HF_ENDPOINT 覆盖。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = ROOT / "models"

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

ProgressCb = Optional[Callable[[str, int, int], None]]   # (文件名, 已下载, 总大小)

# name -> 目标信息。desc 会显示在安装脚本和设置面板里。
MODEL_TARGETS: dict[str, dict] = {
    "parakeet-ja": dict(
        repo="csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8",
        dest="parakeet-ja-0.6b-int8",
        patterns=["*.onnx", "tokens.txt", "test_wavs/*"],
        required=["model.int8.onnx", "tokens.txt"],
        size="约 628MB",
        desc="NVIDIA Parakeet 日语版 0.6B（整句校对，速度快）",
    ),
}


def target_dir(name: str) -> Path:
    return MODELS_DIR / MODEL_TARGETS[name]["dest"]


def is_complete(name: str) -> bool:
    info = MODEL_TARGETS[name]
    d = target_dir(name)
    return d.exists() and all((d / r).exists() for r in info["required"])


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(name: str, on_progress: ProgressCb = None) -> Path:
    """下载某个模型；已完整存在时直接返回。"""
    if name not in MODEL_TARGETS:
        raise KeyError(f"未知模型：{name}")
    dest = target_dir(name)
    if is_complete(name):
        return dest

    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    class _ProgressTqdm(tqdm):  # 把单文件进度透出来
        def update(self, n=1):  # noqa: D102
            r = super().update(n)
            if on_progress is not None:
                try:
                    on_progress(str(self.desc or ""), int(self.n or 0), int(self.total or 0))
                except Exception:  # noqa: BLE001
                    pass
            return r

    info = MODEL_TARGETS[name]
    dest.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    snapshot_download(
        repo_id=info["repo"],
        local_dir=str(dest),
        allow_patterns=info["patterns"],
        max_workers=4,
        tqdm_class=_ProgressTqdm,
    )
    if not is_complete(name):
        raise RuntimeError("下载完成但缺少关键文件，请重试")
    (dest / ".complete").write_text(f"{name}\n", encoding="utf-8")
    if on_progress is not None:
        on_progress("完成", int(dir_size(dest)), int(dir_size(dest)))
    _ = t0
    return dest
