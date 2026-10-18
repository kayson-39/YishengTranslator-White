# -*- coding: utf-8 -*-
"""配置管理：默认值、JSON 持久化、翻译/识别服务预设。"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from ..paths import CONFIG_PATH

# ---------------------------------------------------------------- 服务预设
# kind: openai=OpenAI 兼容 chat/completions；deepl；googlefree；msfree；mymemory
TRANSLATE_PRESETS: dict[str, dict] = {
    "ms-free": dict(
        label="免费 · 微软翻译（免密钥）", kind="msfree", base_url="", model="",
        key_hint="无需密钥，直连可用", unofficial=True,
    ),
    "google-free": dict(
        label="免费 · 谷歌翻译（免密钥）", kind="googlefree", base_url="", model="",
        key_hint="无需密钥，需能访问谷歌", unofficial=True,
    ),
    "mymemory-free": dict(
        label="免费 · MyMemory（免密钥）", kind="mymemory", base_url="", model="",
        key_hint="无需密钥，有每日额度限制", unofficial=True,
    ),
    "localmt": dict(
        label="本地 · 轻量翻译（低配备用）", kind="localmt", base_url="", model="",
        key_hint="完全离线，日语经英语中转为中文；速度快，口语和反问句精度有限",
    ),
    "llamacpp": dict(
        label="本地 · 自然口语翻译（混元 Hy-MT2）", kind="llamacpp",
        base_url="", model="",
        key_hint="自带引擎，不依赖 Ollama；选好 GGUF 模型即可用，面板里可一键导入 Ollama 已下载的模型",
    ),
    "ollama": dict(
        label="本地 · Ollama（可自定义任意模型）", kind="ollama",
        base_url="http://127.0.0.1:11434", model="translategemma:4b",
        key_hint="需要安装 Ollama；可自由选择/下载本地模型，面板里带测速",
    ),
    "deepseek": dict(
        label="DeepSeek（推荐，便宜且中文好）", kind="openai",
        base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        key_hint="在 platform.deepseek.com 创建 API Key",
    ),
    "siliconflow": dict(
        label="硅基流动 SiliconFlow", kind="openai",
        base_url="https://api.siliconflow.cn/v1", model="deepseek-ai/DeepSeek-V3",
        key_hint="在 siliconflow.cn 创建 API Key（有免费额度）",
    ),
    "dashscope": dict(
        label="阿里云百炼 · 通义千问", kind="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-flash",
        key_hint="在 bailian.console.aliyun.com 创建 API Key",
    ),
    "zhipu": dict(
        label="智谱 GLM", kind="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-4-flash",
        key_hint="在 bigmodel.cn 创建 API Key",
    ),
    "moonshot": dict(
        label="月之暗面 Kimi", kind="openai",
        base_url="https://api.moonshot.cn/v1", model="moonshot-v1-8k",
        key_hint="在 platform.moonshot.cn 创建 API Key",
    ),
    "openai": dict(
        label="OpenAI / 兼容代理", kind="openai",
        base_url="https://api.openai.com/v1", model="gpt-4o-mini",
        key_hint="需要能访问 api.openai.com，或填代理地址",
    ),
    "gemini": dict(
        label="Google Gemini", kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-2.5-flash", key_hint="在 aistudio.google.com 获取 API Key",
    ),
    "deepl": dict(
        label="DeepL", kind="deepl", base_url="", model="",
        key_hint="DeepL 免费版 Key 即可，直连质量高",
    ),
    "custom": dict(
        label="自定义（本地 LM Studio / llama.cpp / 云端均可）", kind="openai",
        base_url="", model="", key_hint="填写任意 OpenAI 兼容的 Base URL / 模型 / Key；本地服务可点“拉取模型列表”",
    ),
}

# 日语识别只保留一个经过取舍的本地模型，避免用户在相似选项中反复试错。
FINAL_ENGINES: dict[str, dict] = {
    "parakeet": dict(label="日语高速识别（Parakeet 0.6B）",
                     path="parakeet-ja-0.6b-int8", kind="sherpa", download="parakeet-ja"),
}


def _defaults() -> dict:
    return {
        "version": 1,
        "model_setup_version": 4,
        "audio": {"source": "loopback", "device": "", "block_ms": 80},
        "vad": {"threshold": 0.4, "min_silence_ms": 350, "min_speech_ms": 200, "max_segment_s": 10},
        "asr": {
            "partial": True,
            "draft_mode": "rolling",     # rolling=滚动识别 / off=关闭实时草稿
            "draft_interval_ms": 500,
            "draft_threads": 4,
            "final_engine": "parakeet",
            "final_threads": 6,
            "max_segment_s": 10,
            "context_prompt": False,
        },
        "translate": {
            "enabled": True,
            "provider": "llamacpp",
            "auto_fallback": False,
            "live_provider": "same",
            "live": True,
            "live_min_interval_ms": 450,
            "live_min_new_chars": 2,
            "context_lines": 3,
            "glossary": [],
            "services": {"llamacpp": {"model": "models/llm/Hy-MT2-7B-Q4_K_M.gguf", "variant": "auto"}},
        },
        "ui": {
            "theme": "light",
            "accent": "#d95482",
            "bg": "none",
            "bg_blur": 16,
            "bg_dim": 45,
            "bg_opacity": 100,
            "font": "Microsoft YaHei UI",
            "jp_size": 13,
            "zh_size": 19,
            "max_lines": 100,
            "display": "both",  # both / zh / jp
            "show_jp_in_history": True,
            "window": {"w": 1220, "h": 720, "x": None, "y": None, "on_top": False},
        },
        "overlay": {
            "enabled": False, "lines": 0, "zh_size": 24, "jp_size": 15,
            "theme": "dark", "opacity": 88, "text_shadow": True, "show_jp": True,
            "click_through": False, "locked": False,
            "x": None, "y": None, "w": 760, "h": 360,
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """线程安全的配置对象（dict 风格访问）。"""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = _defaults()
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        if self.path.exists():
            try:
                user = json.loads(self.path.read_text(encoding="utf-8"))
                if int(user.get('model_setup_version', 0)) < 3:
                    # 旧版本的高门限和备用识别设置会让轻声、短句无法进入识别。
                    user.setdefault('vad', {}).update(threshold=0.4, min_silence_ms=500, max_segment_s=18)
                    asr = user.setdefault('asr', {})
                    asr.update(final_engine='parakeet', max_segment_s=18, context_prompt=False)
                    for name in ('cloud', 'streaming', 'stream_model'):
                        asr.pop(name, None)
                    translation = user.setdefault('translate', {})
                    translation['live_provider'] = 'same'
                    local = translation.setdefault('services', {}).setdefault('llamacpp', {})
                    if not local.get('model') or 'translategemma' in str(local['model']).lower():
                        local['model'] = 'models/llm/Hy-MT2-1.8B-Q4_K_M.gguf'
                    user['model_setup_version'] = 3
                if int(user.get('model_setup_version', 0)) < 4:
                    user.setdefault('asr', {}).update(final_engine='parakeet', draft_interval_ms=500, max_segment_s=10)
                    user.setdefault('vad', {}).update(min_silence_ms=350, max_segment_s=10)
                    user.setdefault('translate', {}).update(live_provider='same', live_min_interval_ms=450, live_min_new_chars=2)
                    local = user.setdefault('translate', {}).setdefault('services', {}).setdefault('llamacpp', {})
                    if not local.get('model') or 'hy-mt2-1.8b' in str(local['model']).lower():
                        local['model'] = 'models/llm/Hy-MT2-7B-Q4_K_M.gguf'
                    user['model_setup_version'] = 4
                with self._lock:
                    self.data = _deep_merge(_defaults(), user)
            except Exception:  # noqa: BLE001 配置损坏时回退默认值
                pass

    def save(self) -> None:
        with self._lock:
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, path: str, value: Any, save: bool = True) -> None:
        with self._lock:
            node = self.data
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        if save:
            self.save()

    def update_many(self, mapping: dict[str, Any], save: bool = True) -> None:
        for k, v in mapping.items():
            self.set(k, v, save=False)
        if save:
            self.save()

    # ------------------------------------------------------------------
    def service(self, provider: str) -> dict:
        """返回某个翻译服务的有效配置（预设 + 用户覆盖）。"""
        preset = TRANSLATE_PRESETS.get(provider, TRANSLATE_PRESETS["custom"])
        user = self.get(f"translate.services.{provider}", {}) or {}
        merged = {
            "provider": provider,
            "kind": preset["kind"],
            "label": preset["label"],
            "base_url": user.get("base_url") or preset.get("base_url", ""),
            "model": user.get("model") or preset.get("model", ""),
            "api_key": user.get("api_key", ""),
        }
        return merged

    def set_service(self, provider: str, **fields: Any) -> None:
        cur = dict(self.get(f"translate.services.{provider}", {}) or {})
        cur.update(fields)
        self.set(f"translate.services.{provider}", cur)
