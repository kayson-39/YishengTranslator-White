"""Installer step: reuse the app's resumable, hash-checked model downloader."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'runtime' / 'packages'))

from jpsub.core.llama_engine import RECOMMENDED_MODELS, download_model


def main() -> int:
    model = RECOMMENDED_MODELS[0]
    last_report = -1

    def progress(message: str, done: int, total: int) -> None:
        nonlocal last_report
        bucket = done // (32 * 1024 * 1024)
        if bucket != last_report:
            last_report = bucket
            print(f'{message} {done}/{total}', flush=True)

    path = download_model(model, on_progress=progress)
    print(f'模型就绪：{path}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'模型下载失败：{exc}', file=sys.stderr, flush=True)
        raise SystemExit(1)
