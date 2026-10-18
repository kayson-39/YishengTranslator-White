# -*- coding: utf-8 -*-
"""唯一的日语语音识别引擎：Parakeet 0.6B INT8。"""
from __future__ import annotations

import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..paths import MODELS
from .audio import normalize_gain


MODEL_DIR = MODELS / "parakeet-ja-0.6b-int8"


@dataclass
class AsrResult:
    text: str
    seconds: float
    audio_seconds: float

    @property
    def rtf(self) -> float:
        return self.seconds / self.audio_seconds if self.audio_seconds > 0 else 0.0


class _Parakeet:
    def __init__(self, num_threads: int):
        self.num_threads = num_threads
        self._recognizer = None

    def load(self) -> None:
        if self._recognizer is not None:
            return
        import sherpa_onnx

        model = MODEL_DIR / "model.int8.onnx"
        tokens = MODEL_DIR / "tokens.txt"
        if not (model.is_file() and tokens.is_file()):
            raise FileNotFoundError("日语识别模型不完整，请重新安装软件")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(model), tokens=str(tokens), num_threads=self.num_threads,
        )

    def transcribe(self, samples: np.ndarray) -> AsrResult:
        self.load()
        started = time.perf_counter()
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, normalize_gain(samples))
        self._recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        seconds = time.perf_counter() - started
        return AsrResult(text=text, seconds=seconds, audio_seconds=len(samples) / 16000)


class FinalASR:
    """保留原统一入口，内部固定使用 Parakeet，旧配置不会再切换模型。"""

    def __init__(self, engine: str = "parakeet", num_threads: int = 6, cloud=None):  # noqa: ARG002
        self.engine = "parakeet"
        self.num_threads = num_threads
        self._impl = None
        self._lock = threading.RLock()

    def load(self) -> None:
        with self._lock:
            if self._impl is None:
                impl = _Parakeet(self.num_threads)
                impl.load()
                self._impl = impl

    def transcribe(self, samples: np.ndarray, context: str = "") -> AsrResult:  # noqa: ARG002
        with self._lock:
            self.load()
            return self._impl.transcribe(samples)

    @staticmethod
    def available(engine: str = "parakeet") -> bool:
        return engine == "parakeet" and all(
            (MODEL_DIR / filename).is_file()
            for filename in ("model.int8.onnx", "tokens.txt")
        )


def decode_wav(path: str | Path) -> np.ndarray:
    """读取 16-bit PCM WAV，转换为 16kHz 单声道 float32。"""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    if width != 2:
        raise ValueError("仅支持 16bit PCM wav")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        import soxr

        data = soxr.resample(data, rate, 16000)
    return data.astype(np.float32)
