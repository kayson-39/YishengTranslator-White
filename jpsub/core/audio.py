# -*- coding: utf-8 -*-
"""音频采集：系统声音（WASAPI loopback）/ 麦克风，统一输出 16kHz 单声道。"""
from __future__ import annotations

import threading
from typing import Callable, List

import numpy as np

import soundcard as sc
import soxr

TARGET_RATE = 16000


def list_devices() -> dict[str, List[str]]:
    """列出可用的系统声音（loopback）与麦克风设备名。"""
    loopback, mic = [], []
    try:
        for m in sc.all_microphones(include_loopback=True):
            if getattr(m, "isloopback", False):
                loopback.append(m.name)
            else:
                mic.append(m.name)
    except Exception:  # noqa: BLE001
        pass
    return {"loopback": loopback, "mic": mic}


def _pick_device(source: str, name: str):
    """根据来源与设备名找到 soundcard 设备对象。"""
    devices = [d for d in sc.all_microphones(include_loopback=True)
               if bool(d.isloopback) == (source == "loopback")]
    if name:
        for device in devices:
            if name in (device.name, device.id):
                return device
    if source == "loopback":
        spk = sc.default_speaker()
        if spk is None:
            raise RuntimeError("没有可用的默认扬声器，请连接耳机或启用输出设备")
        for device in devices:
            if device.id == spk.id or device.name == spk.name:
                return device
        raise RuntimeError("默认输出设备不支持系统声音采集")
    return sc.default_microphone()


class AudioCapture(threading.Thread):
    """后台采集线程：按块读取音频，重采样为 16k 单声道后回调。"""

    def __init__(
        self,
        source: str,
        device_name: str,
        on_chunk: Callable[[np.ndarray, float], None],
        on_error: Callable[[str], None],
        block_ms: int = 80,
        on_status: Callable[[str], None] | None = None,
    ):
        super().__init__(daemon=True, name="AudioCapture")
        self.source = source
        self.device_name = device_name
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.block_ms = block_ms
        self._stop_event = threading.Event()
        self.on_status = on_status or (lambda text: None)
        self.sample_rate = 48000

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: C901
        try:
            device = _pick_device(self.source, self.device_name)
            if device is None:
                raise RuntimeError("没有可用的麦克风")
            self.on_status(f"正在采集：{device.name}")
        except Exception as e:  # noqa: BLE001
            self.on_error(f"找不到音频设备：{e}")
            return

        # 探测设备原生采样率（loopback 一般 48000 或 44100）
        native = 48000
        try:
            native = int(round(device.samplerate)) if getattr(device, "samplerate", None) else 48000
        except Exception:  # noqa: BLE001
            native = 48000
        if native not in (44100, 48000, 16000, 22050, 32000, 88200, 96000, 11025, 8000):
            native = 48000
        self.sample_rate = native

        block = max(160, int(native * self.block_ms / 1000))
        resampler = (soxr.ResampleStream(native, TARGET_RATE, 1, dtype="float32", quality="HQ")
                     if native != TARGET_RATE else None)
        silent_samples = 0
        silence_warned = False

        try:
            # WASAPI 的强制单声道录音可能产生异常数据。保留原始声道，再在软件里混音。
            recorder = device.recorder(samplerate=native, channels=None, blocksize=block)
            with recorder as rec:
                while not self._stop_event.is_set():
                    data = rec.record(numframes=block)
                    if self._stop_event.is_set():
                        break
                    if data is None or len(data) == 0:
                        continue
                    data = np.asarray(data, dtype=np.float32)
                    mono = data.mean(axis=1) if data.ndim == 2 else data.reshape(-1)
                    mono = np.nan_to_num(mono)
                    if resampler is not None:
                        mono = resampler.resample_chunk(mono)
                    if mono.size == 0:
                        continue
                    rms = float(np.sqrt(np.mean(mono**2) + 1e-12))
                    silent_samples = silent_samples + mono.size if rms < 1e-5 else 0
                    if silent_samples > TARGET_RATE * 8 and not silence_warned:
                        silence_warned = True
                        self.on_status("没有采集到声音：请确认视频正在播放，并选择视频实际使用的输出设备")
                    elif rms >= 1e-5 and silence_warned:
                        silence_warned = False
                        self.on_status(f"正在采集：{device.name}")
                    self.on_chunk(mono, rms)
        except Exception as e:  # noqa: BLE001
            if not self._stop_event.is_set():
                self.on_error(f"音频采集出错：{e}")


def normalize_gain(samples: np.ndarray, target_peak: float = 0.85, max_gain: float = 6.0) -> np.ndarray:
    """轻度自动增益，让小声的直播也能被稳定识别。"""
    if samples.size == 0:
        return samples
    peak = float(np.max(np.abs(samples)))
    if peak < 1e-4:
        return samples
    gain = min(max_gain, target_peak / peak)
    if gain <= 1.02:
        return samples
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)
