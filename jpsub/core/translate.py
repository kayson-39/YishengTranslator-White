# -*- coding: utf-8 -*-
"""翻译服务：OpenAI 兼容大模型（流式）、DeepL、以及免密钥便民通道。"""
from __future__ import annotations

import json
import time
from typing import Callable, Iterable, Optional

import httpx

from .http_util import make_client

OnDelta = Optional[Callable[[str], None]]

class TranslationCancelled(Exception):
    """An obsolete preview was cancelled; never trigger provider fallback."""


class TranslationDeadlineExceeded(TimeoutError):
    pass


class TranslationStream:
    """Cooperative cancellation and an overall deadline, independent of SSE activity."""
    def __init__(self, callback, cancelled, seconds=45.0, read_timeout=15.0):
        self.callback = callback
        self.cancelled = cancelled
        self.deadline = time.monotonic() + seconds
        self.read_timeout = read_timeout

    def checkpoint(self):
        if self.cancelled():
            raise TranslationCancelled()
        if time.monotonic() >= self.deadline:
            raise TranslationDeadlineExceeded("翻译请求超过等待上限")

    def __call__(self, piece):
        self.checkpoint()
        self.callback(piece)


def _checkpoint(callback):
    if hasattr(callback, "checkpoint"):
        callback.checkpoint()


def _stream_timeout(callback, default):
    read = min(default, float(getattr(callback, "read_timeout", default)))
    return httpx.Timeout(default, connect=min(8.0, default), read=read)


SYSTEM_PROMPT = (
    "你是专业的日语→简体中文同声传译字幕员，服务对象是正在观看日语直播的中国观众。\n"
    "翻译要求：\n"
    "1. 只输出简体中文译文本身，不要任何解释、注音、原文、引号或说话人标记。\n"
    "2. 口语化、简洁、贴合直播语气；保留情绪与语气词，不要过度书面化。\n"
    "3. 人名、游戏名、角色名、专有名词使用常见中文译名；没有通用译名时保留日文原文。\n"
    "4. 遇到日式网络用语按中文直播圈习惯翻译；数字、时间、单位保持原意。\n"
    "5. 句尾语气（如「～だよ」「～だね」）用自然的中文语气表达，不要机械直译。\n"
    "6. 直播口语里的「～ない？」「～じゃない？」多为反问或撒娇，用「是不是…？」「…吧？」等自然语气；\n"
    "   只有明确否定陈述时才译成否定句。\n"
    "7. 「やばい」「エグい」等词按语境选褒义（太顶了/绝了/不得了）或贬义（糟糕/可怕），不要一律直译。"
)


def build_messages(text: str, context: list[str], glossary: Iterable[tuple[str, str]]) -> list[dict]:
    sys = SYSTEM_PROMPT
    terms = [f"{a}→{b}" for a, b in glossary if a and b]
    if terms:
        sys += "\n术语表（必须遵守）：\n" + "\n".join(terms)
    user_parts = []
    ctx = [c for c in context if c]
    if ctx:
        user_parts.append("【上文，仅供理解指代与语气，不要翻译】\n" + "\n".join(ctx[-4:]))
    user_parts.append("【本次需要翻译的日语】\n" + text)
    user_parts.append("直接输出中文译文：")
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _friendly_http_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        detail = ""
        try:
            body = e.response.json()
            detail = str(body.get("error", {}).get("message") or body.get("message") or "")[:120]
        except Exception:  # noqa: BLE001
            detail = (e.response.text or "")[:120]
        hints = {
            401: "API Key 无效或未填写",
            403: "没有权限或地区受限",
            404: "接口地址或模型名不对",
            429: "请求太频繁或额度用尽",
            500: "服务端出错", 502: "网关错误", 503: "服务暂时不可用",
        }
        return f"{hints.get(code, f'HTTP {code}')}" + (f"（{detail}）" if detail else "")
    if isinstance(e, httpx.TimeoutException):
        return "请求超时（网络不通或被墙）"
    if isinstance(e, httpx.ConnectError):
        url = ""
        try:
            url = str(e.request.url)
        except Exception:  # noqa: BLE001
            pass
        if "127.0.0.1" in url or "localhost" in url:
            return "本地服务未启动（请先启动 Ollama 或本地模型服务）"
        return "无法连接服务器（检查网络/代理）"
    return str(e)[:160]
# ---------------------------------------------------------------------------
class OllamaNative:
    """本地 Ollama（原生 API）：支持 qwen3 关闭思考、模型常驻、流式输出。"""

    kind = "ollama"

    def __init__(self, base_url: str = "", model: str = "", timeout: float = 180.0):
        from .ollama import normalize_base

        self.base = normalize_base(base_url)
        self.model = model or ""
        self.timeout = timeout
        if not self.model:
            raise RuntimeError("未指定本地模型（设置 → 翻译服务 → 本地·Ollama → 选择模型）")

    def _messages(self, text: str, context: list[str], glossary) -> list[dict]:
        terms = [f"{a}={b}" for a, b in glossary if a and b]
        if "translategemma" in self.model.lower():
            prompt = (
                "You are a professional Japanese (ja) to Simplified Chinese (zh-Hans) translator. "
                "Accurately convey the meaning and nuances of the original Japanese text while adhering to "
                "Chinese grammar and cultural conventions, in a natural subtitle style. "
                "Produce only the Chinese translation, no additional commentary, no explanations, no source text."
            )
            prompt += (
                " Note: in Japanese live-streaming speech, \"〜ない？/〜じゃない？\" is usually a rhetorical "
                "question (translate as \"是不是…？\"), and \"やばい\" can be positive slang "
                "(\"太顶了/绝了\") depending on context."
            )
            if terms:
                prompt += "\nGlossary (must follow): " + "; ".join(terms)
            return [{"role": "user", "content": prompt + "\n\n" + text}]
        return build_messages(text, context, glossary)

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:
        payload = {
            "model": self.model,
            "messages": self._messages(text, context, glossary),
            "stream": True,
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_predict": 800},
        }
        if "qwen3" in self.model.lower():
            payload["think"] = False  # qwen3 默认带思考链，拖慢数倍
        parts: list[str] = []
        import json as _json

        with make_client(self.base, timeout=_stream_timeout(on_delta, self.timeout)) as client:
            with client.stream("POST", f"{self.base}/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    _checkpoint(on_delta)
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"]))
                    piece = str((obj.get("message") or {}).get("content") or "")
                    if piece:
                        parts.append(piece)
                        if on_delta:
                            on_delta(piece)
                    if obj.get("done"):
                        break
        out = "".join(parts).strip().strip('"').strip()
        if not out:
            raise RuntimeError("本地模型没有返回内容")
        return out


class LocalMTProvider:
    """内置极速本地翻译（Opus-MT / Argos，无需任何外部软件）。"""

    kind = "localmt"

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:  # noqa: ARG002
        from .local_mt import shared

        return shared("ja", "zh").translate(text, on_delta)


class LlamaCppProvider:
    """内置 llama.cpp 引擎（llama-server）：本地、免安装、加载 GGUF 模型。"""

    kind = "llamacpp"

    def __init__(self, service: dict):
        self.service = service or {}

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:
        from .llama_engine import ensure_running, resolve_model

        model = resolve_model(str(self.service.get("model") or ""))
        if model is None:
            raise RuntimeError("尚未选择 GGUF 模型（设置 → 翻译服务 → 本地·内置引擎）")
        variant = str(self.service.get("variant") or "cpu")
        from . import llama_engine as _le

        requested = variant
        candidates = [variant]
        if variant != "cpu":
            candidates += [v for v in ("vulkan", "cuda", "cpu") if v != variant and _le.installed(v)]
        failures = []
        for candidate in candidates:
            if not _le.installed(candidate):
                continue
            _checkpoint(on_delta)
            try:
                base = ensure_running(model, candidate)
                self.actual_variant = candidate
                self.engine_reason = "；".join(failures) if candidate != requested else ""
                break
            except Exception as exc:
                failures.append(f"{candidate.upper()}：{exc}")
        else:
            raise RuntimeError("；".join(failures) or "没有可用的本地翻译引擎")
        if "translategemma" in model.name.lower():
            return TranslateGemma(base).translate(text, context, glossary, on_delta)
        if "hy-mt2" in model.name.lower():
            return HyMT2(base).translate(text, context, glossary, on_delta)
        return OpenAICompat(base, "", model.stem).translate(text, context, glossary, on_delta)


# ---------------------------------------------------------------------------
class TranslateGemma:
    """通过 GGUF 内置模板调用 TranslateGemma 的官方翻译协议。"""
    def __init__(self, base: str):
        self.base = base.removesuffix('/v1')

    def messages(self, text, context, glossary):
        return [{'role': 'user', 'content': 'ja[sprt]zh-Hans[sprt]' + text}]

    def sampling(self):
        return {'temperature': 0.0}

    def translate(self, text, context, glossary, on_delta=None):
        # 模型 GGUF 自带的 chat_template 只在收到这一格式时才会生成训练时使用的
        # “Japanese -> Chinese”专用提示。旧代码手工拼模板，还额外重复了 BOS，
        # llama.cpp 会明确报警并降低短句翻译的稳定性。
        _ = context, glossary
        payload = {
            'messages': self.messages(text, context, glossary),
            'max_tokens': 512,
            'stream': True,
            **self.sampling(),
        }
        parts = []
        with make_client(self.base, timeout=_stream_timeout(on_delta, 90)) as client:
            with client.stream('POST', self.base + '/v1/chat/completions', json=payload) as response:
                if response.status_code >= 400:
                    response.read()
                    response.raise_for_status()
                for line in response.iter_lines():
                    _checkpoint(on_delta)
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    obj = json.loads(data)
                    if obj.get('error'):
                        raise RuntimeError(str(obj['error']))
                    choices = obj.get('choices') or []
                    piece = str((choices[0].get('delta') or {}).get('content') or '') if choices else ''
                    if piece:
                        parts.append(piece)
                        if on_delta:
                            on_delta(piece)
        out = ''.join(parts).strip()
        if not out:
            raise RuntimeError('本地翻译模型未返回译文')
        return out


class HyMT2(TranslateGemma):
    """腾讯翻译专用模型：用内置聊天模板，支持术语、背景和口语风格。"""

    def messages(self, text, context, glossary):
        sections = []
        terms = [f'{a} 翻译成 {b}' for a, b in glossary if a and b]
        if terms:
            sections.append('参考下面的翻译：\n' + '\n'.join(terms[:40]))
        background = [str(c).strip() for c in context if c]
        if background:
            sections.append('【背景信息，仅供理解，不要翻译】\n' + '\n'.join(background[-2:])[-240:])
        sections.append(
            '将下面日语翻译成自然、简洁的现代汉语口语字幕。只翻译已经说出的内容，'
            '原句没说完时保留未完成的语气，不猜测后半句；保留时态、否定、疑问和人称，'
            '不要把叙述改成劝告。原文省略主语时，不擅自补出“我”或“你”。'
            '不使用文言文或古风措辞，不扩写，不解释。'
            '背景信息只用于理解指代，绝不混入本次译文。只输出译文：\n\n'
            + text
        )
        return [{'role': 'user', 'content': '\n\n'.join(sections)}]


class OpenAICompat:
    kind = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout
        if not self.base_url or not self.model:
            raise RuntimeError("服务未配置：请填写 Base URL 与模型名")

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": build_messages(text, context, glossary),
            "temperature": 0.2,
            "max_tokens": 800,
            "stream": True,
        }
        parts: list[str] = []
        with make_client(self.base_url, timeout=_stream_timeout(on_delta, self.timeout)) as client:
            with client.stream("POST", f"{self.base_url}/chat/completions",
                               headers=headers, json=payload) as r:
                if r.status_code >= 400:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    _checkpoint(on_delta)
                    if not line:
                        continue
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                            delta = obj["choices"][0].get("delta", {})
                            piece = delta.get("content") or ""
                            if piece:
                                parts.append(piece)
                                if on_delta:
                                    on_delta(piece)
                        except (TranslationCancelled, TranslationDeadlineExceeded):
                            raise
                        except Exception:  # noqa: BLE001
                            continue
        out = "".join(parts).strip().strip('"').strip()
        if not out:
            raise RuntimeError("模型没有返回内容")
        return out


class DeepL:
    kind = "deepl"

    def __init__(self, api_key: str, base_url: str = "", timeout: float = 15.0):
        self.api_key = api_key or ""
        if not self.api_key:
            raise RuntimeError("DeepL 需要填写 API Key")
        self.base_url = (base_url or ("https://api-free.deepl.com" if self.api_key.endswith(":fx")
                                      else "https://api.deepl.com")).rstrip("/")
        self.timeout = timeout

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:  # noqa: ARG002
        data = {"auth_key": self.api_key, "text": text, "source_lang": "JA", "target_lang": "ZH"}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/v2/translate", data=data)
            r.raise_for_status()
            return r.json()["translations"][0]["text"].strip()


class GoogleFree:
    kind = "googlefree"
    _URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:  # noqa: ARG002
        params = {"client": "gtx", "sl": "ja", "tl": "zh-CN", "dt": "t", "q": text}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._URL, params=params)
            r.raise_for_status()
            data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


class MicrosoftFree:
    kind = "msfree"
    _AUTH = "https://edge.microsoft.com/translate/auth"
    _API = "https://api-edge.cognitive.microsofttranslator.com/translate"
    _token = ""
    _token_at = 0.0

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def _get_token(self, client: httpx.Client) -> str:
        now = time.time()
        if MicrosoftFree._token and now - MicrosoftFree._token_at < 480:
            return MicrosoftFree._token
        r = client.get(MicrosoftFree._AUTH)
        r.raise_for_status()
        MicrosoftFree._token = r.text.strip()
        MicrosoftFree._token_at = now
        return MicrosoftFree._token

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:  # noqa: ARG002
        with httpx.Client(timeout=self.timeout) as client:
            token = self._get_token(client)
            params = {"api-version": "3.0", "from": "ja", "to": "zh-Hans"}
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            r = client.post(self._API, params=params, headers=headers, json=[{"Text": text}])
            r.raise_for_status()
            return r.json()[0]["translations"][0]["text"].strip()


class MyMemoryFree:
    kind = "mymemory"
    _URL = "https://api.mymemory.translated.net/get"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def translate(self, text: str, context: list[str], glossary, on_delta: OnDelta = None) -> str:  # noqa: ARG002
        params = {"q": text[:480], "langpair": "ja|zh-CN"}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(self._URL, params=params)
            r.raise_for_status()
            data = r.json()
        out = (data.get("responseData") or {}).get("translatedText") or ""
        if not out or "MYMEMORY WARNING" in out.upper():
            raise RuntimeError("MyMemory 额度限制")
        return out.strip()


_KINDS = {
    "openai": OpenAICompat,
    "deepl": DeepL,
    "googlefree": GoogleFree,
    "msfree": MicrosoftFree,
    "mymemory": MyMemoryFree,
}

FALLBACK_ORDER = ["ms-free", "mymemory-free", "google-free"]

# 本地服务可用性缓存：{base_url: (是否可用, 检查时间)}
_LOCAL_READY_CACHE: dict[str, tuple[bool, float]] = {}
_LOCAL_READY_TTL = 20.0
_LAST_GOOD_FALLBACK: list[str] = []  # 最近成功的免密钥通道，优先尝试
# 翻译服务实例缓存（避免每次翻译都重新构造 provider 对象）
# key: provider_id -> (provider实例, 构造时使用的 service_dict 哈希)
import hashlib as _hashlib
_PROVIDER_CACHE: dict[str, tuple[object, str]] = {}
_PROVIDER_CACHE_LOCK = __import__("threading").Lock()


def _service_sig(svc: dict) -> str:
    """把 service dict 的关键字段变成一个字符串签名，用于缓存失效判断。"""
    key = f"{svc.get('kind')}|{svc.get('base_url')}|{svc.get('api_key')}|{svc.get('model')}|{svc.get('variant')}"
    return key


def get_cached_provider(provider_id: str, svc: dict):
    """获取或新建翻译 provider 实例（线程安全，同一配置只构造一次）。"""
    sig = _service_sig(svc)
    with _PROVIDER_CACHE_LOCK:
        cached = _PROVIDER_CACHE.get(provider_id)
        if cached is not None and cached[1] == sig:
            return cached[0]
        inst = make_provider(svc)
        _PROVIDER_CACHE[provider_id] = (inst, sig)
        return inst


def _ollama_ready(base_url: str) -> bool:
    """快速检查 Ollama 是否在线（0.8 秒超时 + 结果缓存 20 秒）。"""
    base = base_url or "http://127.0.0.1:11434"
    now = time.time()
    hit = _LOCAL_READY_CACHE.get(base)
    if hit and now - hit[1] < _LOCAL_READY_TTL:
        return hit[0]
    from .ollama import ping
    try:
        ok = ping(base, timeout=0.8)
    except Exception:  # noqa: BLE001
        ok = False
    _LOCAL_READY_CACHE[base] = (ok, now)
    return ok


def _localmt_ready() -> bool:
    from .local_mt import installed
    return installed("ja", "zh") or (installed("ja", "en") and installed("en", "zh"))


def make_provider(service: dict):
    kind = service.get("kind", "openai")
    if kind == "llamacpp":
        return LlamaCppProvider(service)
    if kind == "ollama":
        return OllamaNative(service.get("base_url", ""), service.get("model", ""))
    if kind == "localmt":
        return LocalMTProvider()
    if kind == "openai":
        return OpenAICompat(service.get("base_url", ""), service.get("api_key", ""), service.get("model", ""))
    if kind == "deepl":
        return DeepL(service.get("api_key", ""), service.get("base_url", ""))
    if kind == "googlefree":
        return GoogleFree()
    if kind == "msfree":
        return MicrosoftFree()
    if kind == "mymemory":
        return MyMemoryFree()
    raise RuntimeError(f"未知的翻译服务类型：{kind}")


class Translator:
    """带自动降级的翻译调度器：主服务 → 免密钥通道并发竞速（谁快用谁）。"""

    def __init__(self, config):
        self.config = config
        self.last_provider = ""
        self.last_error = ""
        self.last_engine = ""
        self.last_engine_reason = ""
        self.last_reason = ""     # 主服务被跳过/失败的原因（降级时给用户看）

    def _service(self, provider: str | None = None) -> dict:
        pid = provider or self.config.get("translate.provider", "ms-free")
        svc = self.config.service(pid)
        if svc.get("kind") == "llamacpp":
            # 内置引擎要知道用哪个变体（CPU / Vulkan / CUDA）——以前这里漏了，
            # 导致不管设置里选什么都只会用 CPU 引擎
            from .llama_engine import preferred_variant

            svc["variant"] = preferred_variant(self.config)
        return svc

    def _skip_reason(self, svc: dict) -> str | None:
        """判断某个服务现在是否可以直接跳过，返回原因文本。"""
        kind = svc.get("kind", "openai")
        base_url = svc.get("base_url", "") or ""
        local = "localhost" in base_url or "127.0.0.1" in base_url
        if kind == "ollama" and not _ollama_ready(base_url):
            return f"{svc.get('label', '')}：本地服务未运行（安装并启动 Ollama 后即可使用）"
        if kind == "localmt" and not _localmt_ready():
            return f"{svc.get('label', '')}：内置模型尚未下载（在设置面板点『一键下载』）"
        if kind == "llamacpp":
            from .llama_engine import installed_variants, resolve_model

            if not installed_variants():
                return f"{svc.get('label', '')}：内置引擎尚未下载（设置里点『下载引擎』）"
            if resolve_model(svc.get("model", "") or "") is None:
                return f"{svc.get('label', '')}：还没有 GGUF 模型（面板里可一键导入 Ollama 已下载的模型）"
        if kind == "openai" and not base_url:
            return f"{svc.get('label', '')}：缺少 Base URL"
        if kind in ("openai", "deepl") and not svc.get("api_key") and not local:
            return f"{svc.get('label', '')}：缺少 API Key"
        return None

    def _order_fallbacks(self, provider: str) -> list[str]:
        rest = [p for p in FALLBACK_ORDER if p != provider]
        if _LAST_GOOD_FALLBACK:
            top = [p for p in _LAST_GOOD_FALLBACK if p in rest]
            rest = top + [p for p in rest if p not in top]
        return rest

    def _race_keyless(self, text: str, context: list[str], glossary) -> tuple[str, str, list[str]]:
        """几个免密钥通道并发翻译，谁先成功用谁。返回 (通道, 译文, 错误列表)。"""
        import queue as _queue
        import threading as _threading

        provider = self.config.get("translate.provider", "ms-free")
        order = self._order_fallbacks(provider)
        q: _queue.Queue = _queue.Queue()
        errors: list[str] = []

        def work(pid: str) -> None:
            svc = self._service(pid)
            try:
                out = make_provider(svc).translate(text, context, glossary, None)
                q.put((pid, out or "", ""))
            except Exception as e:  # noqa: BLE001
                q.put((pid, "", f"{svc.get('label', pid)}：{_friendly_http_error(e)}"))

        for pid in order:
            _threading.Thread(target=work, args=(pid,), daemon=True).start()
        deadline = time.time() + 12.0
        got = 0
        while got < len(order):
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                pid, out, err = q.get(timeout=remain)
            except Exception:  # noqa: BLE001
                break
            got += 1
            if out:
                if pid not in _LAST_GOOD_FALLBACK:
                    _LAST_GOOD_FALLBACK.insert(0, pid)
                    del _LAST_GOOD_FALLBACK[3:]
                return pid, out, errors
            if err:
                errors.append(err)
        return "", "", errors

    def translate(self, text: str, context: list[str], on_delta: OnDelta = None,
                  provider: str | None = None) -> str:
        """翻译文本；主服务失败或未就绪时自动改用免密钥通道。

        provider 参数可临时指定本次使用的服务（例如草稿译文走极速模型）。
        """
        text = (text or "").strip()
        if not text:
            return ""
        glossary = [tuple(x) for x in (self.config.get("translate.glossary", []) or [])]
        provider = provider or self.config.get("translate.provider", "ms-free")
        errors: list[str] = []

        svc = self._service(provider)
        reason = self._skip_reason(svc)
        if reason:
            errors.append(reason)
        else:
            try:
                instance = get_cached_provider(provider, svc)
                out = instance.translate(text, context, glossary, on_delta)
                self.last_engine = getattr(instance, "actual_variant", "")
                self.last_engine_reason = getattr(instance, "engine_reason", "")
                if not out or not out.strip():
                    raise RuntimeError("翻译服务返回空内容")
                self.last_provider = provider
                self.last_error = ""
                return out
            except (TranslationCancelled, TranslationDeadlineExceeded):
                raise
            except Exception as e:  # noqa: BLE001
                errors.append(f"{svc.get('label', provider)}：{_friendly_http_error(e)}")

        self.last_reason = errors[0] if errors else ""
        if not self.config.get("translate.auto_fallback", False):
            self.last_error = "；".join(errors)
            raise RuntimeError(self.last_error)
        pid, out, errs = self._race_keyless(text, context, glossary)
        errors.extend(errs)
        if out:
            self.last_provider = pid
            self.last_error = ""
            return out
        self.last_error = "；".join(errors) or "没有可用的翻译服务"
        raise RuntimeError(self.last_error)

    def test(self, provider: Optional[str] = None) -> str:
        """测试某个服务是否可用，返回中文结果说明。"""
        svc = self._service(provider)
        try:
            out = make_provider(svc).translate("こんにちは、テストです。", [], [])
            return f"连接成功：{out}"
        except Exception as e:  # noqa: BLE001
            return f"连接失败：{_friendly_http_error(e)}"

