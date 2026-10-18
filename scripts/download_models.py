# -*- coding: utf-8 -*-
"""下载实时日语翻译所需的本地模型。

模型清单在 jpsub/core/model_catalog.py（软件设置面板里的「下载模型」用的也是它）。
默认走 hf-mirror.com 国内镜像，可用环境变量 HF_ENDPOINT 覆盖。
用法：
    python scripts/download_models.py                 # 下载唯一的日语识别模型
    python scripts/download_models.py --list          # 查看可下载项
    python scripts/download_models.py --only parakeet-ja
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODELS_DIR = ROOT / "models"

from jpsub.core import model_catalog as catalog  # noqa: E402

VAD_URLS = [
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://hf-mirror.com/csukuangfj/sherpa-onnx-vad-models/resolve/main/silero_vad.onnx",
    "https://huggingface.co/csukuangfj/sherpa-onnx-vad-models/resolve/main/silero_vad.onnx",
]


def download_one(name: str) -> bool:
    info = catalog.MODEL_TARGETS[name]
    dest = catalog.target_dir(name)
    if catalog.is_complete(name):
        print(f"[跳过] {name} 已存在（{catalog.human(catalog.dir_size(dest))}）")
        return True
    print(f"[下载] {name}  {info['desc']}")
    print(f"       来源: {os.environ.get('HF_ENDPOINT')}/{info['repo']}")
    t0 = time.time()
    last = ""

    def on_progress(desc: str, done: int, total: int) -> None:
        nonlocal last
        if not total:
            return
        msg = f"        {desc or '下载中'}  {done * 100 // total}%  " \
              f"{catalog.human(done)}/{catalog.human(total)}"
        if msg != last:
            last = msg
            print(f"\r{msg}", end="", flush=True)

    try:
        catalog.download(name, on_progress=on_progress)
    except Exception as e:  # noqa: BLE001
        print(f"\n[失败] {name}: {e}")
        return False
    print()
    print(f"[完成] {name}  {catalog.human(catalog.dir_size(dest))}  用时 {time.time() - t0:.0f}s")
    return True


def download_vad() -> bool:
    import httpx

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    out = MODELS_DIR / "silero_vad.onnx"
    if out.exists() and out.stat().st_size > 100_000:
        print(f"[跳过] silero_vad.onnx 已存在（{catalog.human(out.stat().st_size)}）")
        return True
    tmp = out.with_suffix(".part")
    for url in VAD_URLS:
        try:
            print(f"[下载] silero_vad.onnx <- {url}")
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                got = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            print(f"\r        {got * 100 // total:3d}%  "
                                  f"{catalog.human(got)}/{catalog.human(total)}", end="")
            print()
            if tmp.stat().st_size > 100_000:
                tmp.replace(out)
                print("[完成] silero_vad.onnx")
                return True
        except Exception as e:  # noqa: BLE001
            print(f"[失败] {url}: {e}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="只下载指定项")
    ap.add_argument("--list", action="store_true", help="列出可下载项")
    ap.add_argument("--endpoint", default=None, help="覆盖 HF 端点，如 https://hf-mirror.com")
    ap.add_argument("--skip-vad", action="store_true")
    ap.add_argument("--all", action="store_true", help="兼容旧命令；现在只下载唯一识别模型")
    args = ap.parse_args()

    if args.endpoint:
        os.environ["HF_ENDPOINT"] = args.endpoint
    catalog.os.environ["HF_ENDPOINT"] = os.environ["HF_ENDPOINT"]

    if args.list:
        for k, v in catalog.MODEL_TARGETS.items():
            flag = "（可选，默认不下载）" if v.get("optional") else ""
            print(f"{k:16s} {v['desc']}{flag}")
        return 0

    default_names = ["parakeet-ja"]
    names = args.only or default_names
    bad = [n for n in names if n not in catalog.MODEL_TARGETS]
    if bad:
        print(f"未知项: {bad}")
        return 2

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"镜像端点: {os.environ.get('HF_ENDPOINT')}")
    results = {n: download_one(n) for n in names}
    if not args.skip_vad:
        results["silero-vad"] = download_vad()

    print("\n===== 结果 =====")
    for k, v in results.items():
        print(f"{'OK ' if v else 'ERR'}  {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
