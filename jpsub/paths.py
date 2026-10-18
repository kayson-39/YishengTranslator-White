# -*- coding: utf-8 -*-
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = Path(os.environ.get("JPSUB_MODEL_DIR", str(ROOT / "models")))
ASSETS = Path(__file__).resolve().parent / "assets"
BACKGROUNDS = ASSETS / "backgrounds"
CACHE = ROOT / "work" / "cache"
HISTORY = ROOT / "history"
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "jpsub.log"


def ensure_dirs() -> None:
    for d in (MODELS, BACKGROUNDS, CACHE, HISTORY):
        d.mkdir(parents=True, exist_ok=True)
