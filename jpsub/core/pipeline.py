# -*- coding: utf-8 -*-
"""主流水线：音频采集 → 静音检测断句 → 流式出字 → 整句高精度校对 → 翻译。"""
from __future__ import annotations

import collections
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6 import QtCore

from ..paths import MODELS
from .asr import FinalASR
from .audio import AudioCapture, normalize_gain
from .segment import Segment
from .translate import Translator, TranslationCancelled, TranslationStream

# 草稿重识别最多占用这么多比例的墙钟时间，剩下的留给整句校对和翻译；
# 一句话越长，重识别一次越慢，草稿就会自动跟着放慢（否则会越跑越卡）。
DRAFT_CPU_BUDGET = 0.5
# 实时草稿译文每跑 1 秒，至少留这么多秒给整句翻译，避免本地模型被草稿占满。
LIVE_DUTY_GAP = 1.2


class Pipeline(QtCore.QThread):
    """在后台线程中运行整条识别翻译流水线。"""

    sig_status = QtCore.Signal(str)
    sig_error = QtCore.Signal(str)
    sig_partial = QtCore.Signal(str)             # 流式日语文本（空串=清空）
    sig_segment = QtCore.Signal(object)          # Segment 最终日语
    sig_live_zh = QtCore.Signal(str, str)        # (sid, 实时译文增量后的全文)
    sig_final_zh = QtCore.Signal(str, str)       # (sid, 最终译文)
    sig_translate_queue = QtCore.Signal(dict)
    sig_translate_failed = QtCore.Signal(str, str)
    sig_translate_info = QtCore.Signal(dict)     # 本次翻译来自哪个服务/耗时
    sig_level = QtCore.Signal(float)             # 0~1 音量
    sig_stats = QtCore.Signal(dict)              # 统计信息

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.cfg = config
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._reset_pending = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._capture: AudioCapture | None = None
        self._final_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="final-asr")
        self._draft_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="draft-asr")
        self._trans_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translate")
        self._live_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translate-live")
        self._pools_closed = False
        self._translate_gate = threading.Lock()
        self._translation_state_lock = threading.Lock()
        self._pending_final = {}
        self._failed_translations = {}
        self._live_task_inflight = False
        self._last_queue_report = 0.0

        # 运行状态
        self._fed = 0                    # 已处理样本数（16k）
        self._pre_roll: collections.deque = collections.deque(maxlen=18)   # ~0.56s 预卷
        self._raw_frames: collections.deque = collections.deque(maxlen=1250)  # 40 秒原始音频
        self._vad_origin = 0
        self._active = False
        self._utterance: list[np.ndarray] = []
        self._utterance_start = 0.0
        self._partial = ""
        self._last_final_jp: list[str] = []
        self._live = {"gen": 0, "sid": "", "busy": False, "last_req": 0.0, "text": "", "latency": 0.0}
        self._fallback_notified = ""
        self._stream = None
        self._draft_engine: FinalASR | None = None
        self._draft_mode = "off"
        self._draft_busy = False
        self._draft_started = 0.0
        self._draft_next_ok = 0.0
        self._final_busy = 0
        self._final: FinalASR | None = None
        self._vad = None
        self._level_counter = 0          # sig_level 降采样计数器
        self._max_segment_samples = 0   # 在 _setup 里初始化，避免热路径每帧读配置
        self._vad_pending = np.zeros(0, dtype=np.float32)
        self._vad_window = 512
        self._queue_warned = False

    # ------------------------------------------------------------- 生命周期
    def _submit(self, pool, fn, *args):
        """往线程池投任务。线程池已经关了就当没这回事——停止瞬间的收尾任务丢掉没关系。"""
        if self._pools_closed:
            return
        try:
            return pool.submit(fn, *args)
        except RuntimeError:      # 池已关闭
            pass

    def _shutdown_pools(self) -> None:
        if self._pools_closed:
            return
        self._pools_closed = True
        for pool in (self._final_pool, self._draft_pool, self._trans_pool, self._live_pool):
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass

    def stop(self, max_wait: float = 1.5) -> None:
        """请求停止。默认不等太久，避免界面在「停止 / 切换设置」时卡住。

        真正的收尾（关线程池、放掉模型）在流水线线程自己退出时做，所以哪怕它还在
        加载模型也不会把任务投进已经关掉的线程池里。
        """
        self._stop.set()
        if self._capture:
            self._capture.stop()
        self.wait(int(max_wait * 1000))

    def pause(self, flag: bool) -> None:
        if flag:
            self._pause.set()
        else:
            self._reset_pending.set()
            self._pause.clear()

    # ------------------------------------------------------------- 主循环
    def run(self) -> None:  # noqa: C901
        try:
            try:
                self._setup()
            except Exception as e:  # noqa: BLE001
                self.sig_error.emit(str(e))
                self.sig_status.emit("启动失败")
                return

            self.sig_status.emit("正在聆听…")
            while not self._stop.is_set():
                try:
                    chunk, rms = self._queue.get(timeout=0.2)
                except queue.Empty:
                    self._report_translation_queue()
                    continue
                if self._stop.is_set():
                    break
                if self._pause.is_set():
                    continue
                if self._reset_pending.is_set():
                    self._reset_pending.clear()
                    self._pre_roll.clear()
                    self._active = False
                    self._utterance = []
                    self._vad_pending = np.zeros(0, dtype=np.float32)
                    self._vad.reset()
                    self._vad_origin = self._fed
                    self._raw_frames.clear()
                    self._live['gen'] += 1
                    if self._stream:
                        self._stream.reset()
                try:
                    self._process_chunk(chunk, rms)
                    self._report_translation_queue()
                except Exception as e:  # noqa: BLE001
                    self.sig_error.emit(f"处理音频出错：{e}")

            # 收尾：如果还有未完成的语音，做最后一次校对
            if self._active and self._utterance:
                self._finalize_utterance()
            self.sig_level.emit(0.0)
        finally:
            if self._capture:
                self._capture.stop()
                self._capture.join(timeout=2.0)
            # finished 代表所有模型工作也已结束。
            self._shutdown_pools()

    # ------------------------------------------------------------- 初始化
    def _setup(self) -> None:
        cfg = self.cfg
        # 0) 先定下整句校对引擎：草稿要不要和它共用同一份模型实例，取决于这里
        engine = "parakeet"
        cloud = {}
        if not FinalASR.available(engine):
            raise FileNotFoundError("日语识别模型不完整，请重新安装软件")
        final_threads = int(cfg.get("asr.final_threads", 6))
        # 1) 实时出字：用唯一模型滚动识别
        want_mode = str(cfg.get("asr.draft_mode", "rolling"))
        if not bool(cfg.get("asr.partial", True)):
            want_mode = "off"
        self._draft_mode = "off"
        if want_mode != "off":
            self.sig_status.emit("正在加载日语识别模型…")
            # 草稿和整句校对共用唯一模型和原生服务进程。
            self._draft_engine = FinalASR("parakeet", num_threads=final_threads)
            self._draft_engine.load()
            self._draft_mode = "rolling"
        # 2) 静音检测
        import sherpa_onnx

        vad_path = MODELS / "silero_vad.onnx"
        if not vad_path.exists():
            raise FileNotFoundError("缺少 silero_vad.onnx，请先运行 scripts/download_models.py")
        vcfg = sherpa_onnx.VadModelConfig()
        vcfg.silero_vad.model = str(vad_path)
        vcfg.silero_vad.window_size = self._vad_window
        vcfg.silero_vad.threshold = float(cfg.get("vad.threshold", 0.5))
        vcfg.silero_vad.min_silence_duration = int(cfg.get("vad.min_silence_ms", 350)) / 1000.0
        vcfg.silero_vad.min_speech_duration = int(cfg.get("vad.min_speech_ms", 200)) / 1000.0
        vcfg.silero_vad.max_speech_duration = float(cfg.get("asr.max_segment_s", 24))
        vcfg.sample_rate = 16000
        vcfg.num_threads = 1
        self._vad = sherpa_onnx.VoiceActivityDetector(vcfg, buffer_size_in_seconds=60)
        # 3) 整句校对引擎（异步预加载）
        if (self._draft_mode == "rolling" and self._draft_engine is not None
                and self._same_engine(self._draft_engine.engine, engine)):
            # 共用上面已经加载好的那一份（草稿 ≠ 校对只是两个用途，模型是同一个）
            self._final = self._draft_engine
        else:
            self._final = FinalASR(engine, num_threads=final_threads, cloud=cloud)
        self._preload_final()
        if self._stop.is_set():
            return
        if cfg.get("translate.enabled", True) and cfg.get("translate.provider", "") == "llamacpp":
            self.sig_status.emit("正在加载本地翻译模型…")
            self._warmup_translate()
            if self._stop.is_set():
                return
        # 4) 音频采集
        self.sig_status.emit("正在打开音频设备…")
        self._capture = AudioCapture(
            source=str(cfg.get("audio.source", "loopback")),
            device_name=str(cfg.get("audio.device", "") or ""),
            on_chunk=self._on_audio,
            on_error=lambda m: self.sig_error.emit(m),
            block_ms=int(cfg.get("audio.block_ms", 80)),
            on_status=lambda m: self.sig_status.emit(m),
        )
        self._capture.start()
        self._fed = 0
        self._max_segment_samples = int(cfg.get("asr.max_segment_s", 24)) * 1000 // 80

    @staticmethod
    def _same_engine(a: str, b: str) -> bool:
        """两个角色能不能共用同一份模型实例：同名、而且是 sherpa 系离线模型才行。"""
        if a != b:
            return False
        from .config import FINAL_ENGINES

        return str(FINAL_ENGINES.get(b, {}).get("kind") or "sherpa") in ("sherpa", "native")

    def _preload_final(self) -> None:
        try:
            self._final.load()
            self.sig_status.emit("模型就绪，正在聆听…")
        except Exception as e:  # noqa: BLE001
            self.sig_error.emit(f"校对模型加载失败：{e}")
            raise

    # ------------------------------------------------------------- 音频处理
    def _on_audio(self, chunk: np.ndarray, rms: float) -> None:
        try:
            self._queue.put_nowait((chunk, rms))
        except queue.Full:
            if not self._queue_warned:
                self._queue_warned = True
                self.sig_error.emit("音频处理跟不上播放速度，请关闭实时草稿或减少后台程序")

    def _process_chunk(self, chunk: np.ndarray, rms: float) -> None:
        level = min(1.0, max(0.0, (20 * np.log10(rms + 1e-6) + 60) / 60)) if rms > 1e-6 else 0.0
        self._level_counter += 1
        if self._level_counter % 4 == 0:
            self.sig_level.emit(level)
        self._vad_pending = np.concatenate((self._vad_pending, chunk))
        while self._vad_pending.size >= self._vad_window:
            frame = self._vad_pending[:self._vad_window]
            self._vad_pending = self._vad_pending[self._vad_window:]
            self._process_frame(frame)

    def _process_frame(self, frame: np.ndarray) -> None:
        now = time.time()
        self._fed += frame.size
        self._raw_frames.append(frame.copy())
        # VAD 以固定窗口运行。整句必须使用 VAD 保存的音频，不能把队列直接 pop 掉。
        self._vad.accept_waveform(normalize_gain(frame, max_gain=6.0))
        speech = bool(self._vad.is_speech_detected())
        if speech and not self._active:
            self._active = True
            self._utterance = list(self._pre_roll)
            self._utterance_start = (self._fed - frame.size - sum(c.size for c in self._pre_roll)) / 16000
            self._live['gen'] += 1
            self._live.update(sid=f"L{self._live['gen']}", busy=False, last_req=0.0, text='')
            self._draft_next_ok = now + int(self.cfg.get('asr.draft_interval_ms', 900)) / 1000
        if self._active:
            self._utterance.append(frame)
        self._pre_roll.append(frame)
        if self._draft_mode == 'streaming' and self._stream is not None:
            text = self._stream.feed(frame)
            if text is not None and text != self._partial:
                self._partial = text
                self.sig_partial.emit(text)
                self._maybe_live_translate(now)
        while not self._vad.empty():
            segment = self._vad.front
            # VAD 接收的逐帧增益音频只用于检测；识别必须使用原始录音。
            # 否则每 32ms 一次的独立放大会改变音色，并放大停顿里的背景噪声。
            history = np.concatenate(self._raw_frames)
            history_start = self._fed - history.size
            segment_start = self._vad_origin + segment.start
            begin = max(history_start, segment_start - 6400)
            end = min(self._fed, segment_start + len(segment.samples) + 2560)
            audio = history[begin-history_start:end-history_start].copy()
            start = begin / 16000.0
            self._vad.pop()
            self._finalize_utterance(audio=audio, start=start)
        if self._active:
            self._maybe_rolling_draft(now)

    def _finalize_utterance(self, keep_active: bool = False,
                            audio: np.ndarray | None = None, start: float | None = None) -> None:
        if audio is None:
            audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, dtype=np.float32)
        live_text = self._partial
        self._utterance = []
        self._active = keep_active
        self._partial = ''
        self._live['gen'] += 1  # 已结束句子的慢草稿不能在最终结果之后重新冒出来
        self.sig_partial.emit('')
        self._draft_next_ok = 0.0
        if self._stream is not None:
            self._stream.reset()
        if audio.size < 4000 or self._stop.is_set():
            return
        t0 = self._utterance_start if start is None else start
        seg = Segment(sid=uuid.uuid4().hex[:8], jp='', t0=t0,
                      t1=t0 + audio.size / 16000.0, live_jp=live_text)
        self._submit(self._final_pool, self._run_final, seg, audio)

    def _run_final(self, seg: Segment, audio: np.ndarray) -> None:
        if self._stop.is_set():
            return
        self._final_busy += 1
        try:
            try:
                context = ("\n".join(self._last_final_jp[-3:])
                           if self.cfg.get("asr.context_prompt", True) else "")
                result = self._final.transcribe(audio, context)
            except Exception as e:  # noqa: BLE001
                self.sig_error.emit(f"整句识别失败：{e}")
                return
            self._emit_final(seg, result)
        finally:
            self._final_busy -= 1

    def _emit_final(self, seg: Segment, result) -> None:
        text = result.text.strip()
        if self._stop.is_set():
            return
        if not text:
            self.sig_partial.emit("")
            return
        seg.jp = text
        seg.asr_seconds = result.seconds
        seg.rtf = result.rtf
        context = self._context_lines()
        self._last_final_jp.append(text)
        if len(self._last_final_jp) > 8:
            self._last_final_jp = self._last_final_jp[-8:]
        self.sig_segment.emit(seg)
        self.sig_stats.emit({"rtf": round(result.rtf, 2), "asr_ms": int(result.seconds * 1000),
                             "audio_s": round(result.audio_seconds, 1)})
        if self.cfg.get("translate.enabled", True):
            sid = seg.sid
            self.enqueue_translation(sid, text, context)

    # ------------------------------------------------------------- 滚动草稿
    def _maybe_rolling_draft(self, now: float) -> None:
        if self._draft_mode != "rolling" or self._draft_engine is None or self._draft_busy:
            return
        if self._final_busy:
            return          # 整句校对才是要用的结果，草稿先让路
        if now < self._draft_next_ok:
            return
        total = sum(len(c) for c in self._utterance)
        if total < int(16000 * 0.7):
            return
        self._draft_busy = True
        self._draft_started = now
        gen = self._live["gen"]
        audio = np.concatenate(self._utterance)
        self._submit(self._draft_pool, self._run_draft, gen, audio)

    def _run_draft(self, gen: int, audio: np.ndarray) -> None:
        text = ""
        try:
            result = self._draft_engine.transcribe(audio)
            text = result.text.strip()
        except Exception:  # noqa: BLE001
            text = ""
        finally:
            self._draft_busy = False
            # 这一趟花了多久，下一趟就按比例多等一会儿：草稿最多吃掉 DRAFT_CPU_BUDGET 的时间。
            # （以前是固定 0.6 秒一次，一句话越长重识别越慢，最后会把 CPU 全占满）
            spent = max(0.0, time.time() - self._draft_started)
            interval = int(self.cfg.get("asr.draft_interval_ms", 600)) / 1000.0
            self._draft_next_ok = self._draft_started + max(interval, spent / DRAFT_CPU_BUDGET)
        if not text or gen != self._live["gen"] or not self._active or self._final_busy:
            return
        if text != self._partial:
            self._partial = text
            self.sig_partial.emit(text)
            self._maybe_live_translate(time.time())

    # ------------------------------------------------------------- 翻译
    def _context_lines(self) -> list[str]:
        n = int(self.cfg.get("translate.context_lines", 3))
        return self._last_final_jp[-n:] if n > 0 else []

    def _warmup_translate(self) -> None:
        """启动时静默预热翻译服务（本地大模型先加载好，第一句就不用等）。"""
        try:
            translator = Translator(self.cfg)
            translator.translate("こんにちは。", [])
            self.sig_translate_info.emit({"provider": translator.last_provider,
                                          "engine": translator.last_engine,
                                          "engine_reason": translator.last_engine_reason, "warmup": True})
        except Exception:  # noqa: BLE001
            pass

    def _report_translation_queue(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_queue_report < 1.0:
            return
        self._last_queue_report = now
        with self._translation_state_lock:
            pending = len(self._pending_final)
            oldest = max(0.0, now - min(self._pending_final.values())) if pending else 0.0
            failed = len(self._failed_translations)
        self.sig_translate_queue.emit({"pending": pending, "oldest_s": round(oldest), "failed": failed})

    def _has_final_pending(self):
        with self._translation_state_lock:
            return bool(self._pending_final)

    def enqueue_translation(self, sid, text, context=None):
        if self._stop.is_set() or not text.strip():
            return
        with self._translation_state_lock:
            if sid in self._pending_final:
                return
            self._pending_final[sid] = time.monotonic()
        self._report_translation_queue(True)
        future = self._submit(self._trans_pool, self._run_translate_final, sid, text, context)
        def finished(_future=None):
            with self._translation_state_lock:
                self._pending_final.pop(sid, None)
            self._report_translation_queue(True)
        if future is None:
            finished()
        else:
            future.add_done_callback(finished)

    def _run_translate_final(self, sid: str, text: str, context: list[str] | None = None) -> None:
        # The server has one inference slot. Finals own it ahead of optional previews.
        while not self._stop.is_set():
            if self._translate_gate.acquire(timeout=0.1):
                break
        else:
            return
        try:
            self._translate_final_locked(sid, text, context)
        finally:
            self._translate_gate.release()

    def _translate_final_locked(self, sid, text, context):
        if self._stop.is_set():
            return
        translator = Translator(self.cfg)
        t0 = time.monotonic()
        error = None
        for attempt in range(2):
            parts = []
            last_emit = [0.0]
            def delta(piece):
                parts.append(piece)
                now = time.monotonic()
                if now - last_emit[0] >= 0.04:
                    self.sig_final_zh.emit(sid, ''.join(parts))
                    last_emit[0] = now
            control = TranslationStream(delta, self._stop.is_set,
                                        seconds=max(30.0, min(90.0, len(text) * 0.6)),
                                        read_timeout=15.0)
            try:
                control.checkpoint()
                out = translator.translate(text, context or [], on_delta=control)
                control.checkpoint()
                break
            except TranslationCancelled:
                return
            except Exception as exc:
                error = exc
                # Retry the same configured service once; never silently change models.
                if attempt or any(code in str(exc) for code in ("401", "403", "API Key", "额度")):
                    with self._translation_state_lock:
                        self._failed_translations[sid] = str(exc)
                    self.sig_translate_failed.emit(sid, str(exc))
                    self.sig_error.emit(f"翻译失败（可点补译）：{exc}")
                    return
                if self._stop.wait(0.2):
                    return
        else:
            return
        if self._stop.is_set():
            return
        with self._translation_state_lock:
            self._failed_translations.pop(sid, None)
        self.sig_final_zh.emit(sid, out)
        from .config import TRANSLATE_PRESETS
        label = TRANSLATE_PRESETS.get(translator.last_provider, {}).get("label", translator.last_provider)
        self.sig_translate_info.emit({"provider": translator.last_provider, "label": label,
                                      "seconds": round(time.monotonic() - t0, 1), "sid": sid,
                                      "engine": getattr(translator, "last_engine", ""),
                                      "engine_reason": getattr(translator, "last_engine_reason", "")})
        chosen = str(self.cfg.get("translate.provider", ""))
        if translator.last_provider and translator.last_provider != chosen and self._fallback_notified != chosen:
            self._fallback_notified = chosen
            self.sig_status.emit(f"翻译服务已使用配置允许的备用通道：{label}")

    def _maybe_live_translate(self, now: float) -> None:
        if not self.cfg.get("translate.live", True) or not self.cfg.get("translate.enabled", True):
            return
        live = self._live
        interval = int(self.cfg.get("translate.live_min_interval_ms", 1100)) / 1000.0
        min_new = int(self.cfg.get("translate.live_min_new_chars", 2))
        text = self._partial.strip()
        if not text or live["busy"]:
            return
        if len(text) < 6:
            return  # 太短的草稿容易翻出乱码，等积累一点再翻
        live_provider = self._live_provider()
        main = str(self.cfg.get("translate.provider", ""))
        if live_provider == main:
            from .config import TRANSLATE_PRESETS

            kind = TRANSLATE_PRESETS.get(main, {}).get("kind")
            model = str(self.cfg.service(main).get('model') or '').lower()
            if kind == 'ollama' or (kind == 'llamacpp' and 'hy-mt2' not in model):
                interval = max(interval, 2.2)  # 较大的本地模型避免草稿堆积
            if TRANSLATE_PRESETS.get(main, {}).get("kind") == "localmt":
                interval = max(interval, 1.8)  # localmt 也是阻塞 CPU 的同步引擎，防止堆积
        if len(text) - len(live["text"]) < min_new:
            return
        # 实测的翻译耗时决定节奏：本地模型一句要 2 秒，那就至少隔 2.4 秒再发下一句，
        # 否则草稿翻译会把本地模型（和 CPU）排满，整句翻译只能排队等 —— 越看越卡。
        gap = max(interval, float(live.get("latency") or 0.0) * LIVE_DUTY_GAP)
        if now - live["last_req"] < gap:
            return
        if live_provider == main and self._has_final_pending():
            return
        with self._translation_state_lock:
            if self._live_task_inflight:
                return
            self._live_task_inflight = True
        live["busy"] = True
        live["last_req"] = now
        live["text"] = text
        gen = live["gen"]
        sid = live["sid"]
        if self._submit(self._live_pool, self._run_translate_live, gen, sid, text) is None:
            with self._translation_state_lock:
                self._live_task_inflight = False
            live["busy"] = False

    def _live_provider(self) -> str:
        """草稿译文用哪个服务：主服务是慢速本地大模型时，自动用内置极速模型打底。"""
        mode = str(self.cfg.get("translate.live_provider", "auto"))
        main = str(self.cfg.get("translate.provider", "ms-free"))
        if mode == "same":
            return main
        if mode == "auto":
            from .config import TRANSLATE_PRESETS

            kind = TRANSLATE_PRESETS.get(main, {}).get("kind")
            model = str(self.cfg.service(main).get('model') or '').lower()
            if kind == 'llamacpp' and 'hy-mt2' in model:
                return main
            # localmt/ollama/llamacpp 都是阻塞 CPU 的同步引擎；
            # 草稿改走免密钥在线通道，避免和整句翻译抢同一个本地模型
            if kind in ("ollama", "llamacpp", "localmt"):
                return "localmt"
            return main
        return mode

    def _run_translate_live(self, gen: int, sid: str, text: str) -> None:
        shared = self._live_provider() == str(self.cfg.get("translate.provider", ""))
        def obsolete():
            return (self._stop.is_set() or self._pause.is_set() or not self._active
                    or gen != self._live["gen"] or (shared and self._has_final_pending()))
        acquired = False
        ran = False
        t0 = time.monotonic()
        out = ""
        translator = Translator(self.cfg)
        try:
            if obsolete():
                return
            if shared:
                acquired = self._translate_gate.acquire(blocking=False)
                if not acquired or obsolete():
                    return
            ran = True
            parts = []
            last_emit = [0.0]
            def delta(piece):
                parts.append(piece)
                now = time.monotonic()
                if now - last_emit[0] >= 0.04:
                    self.sig_live_zh.emit(sid, ''.join(parts))
                    last_emit[0] = now
            control = TranslationStream(delta, obsolete, seconds=10.0, read_timeout=6.0)
            out = translator.translate(text, self._context_lines(), provider=self._live_provider(), on_delta=control)
            control.checkpoint()
            if out and out.count("*") < 4 and "＊＊" not in out:
                self.sig_live_zh.emit(sid, out)
                from .config import TRANSLATE_PRESETS
                label = TRANSLATE_PRESETS.get(translator.last_provider, {}).get("label", translator.last_provider)
                self.sig_translate_info.emit({"provider": translator.last_provider, "label": label,
                                              "seconds": round(time.monotonic() - t0, 1), "live": True,
                                              "engine": getattr(translator, "last_engine", ""),
                                              "engine_reason": getattr(translator, "last_engine_reason", "")})
        except TranslationCancelled:
            pass
        except Exception:
            # Optional preview failures must not stop final translation or occupy the slot forever.
            pass
        finally:
            if acquired:
                self._translate_gate.release()
            with self._translation_state_lock:
                self._live_task_inflight = False
            if gen == self._live["gen"]:
                self._live["busy"] = False
            if ran:
                spent = max(0.0, time.monotonic() - t0)
                old = float(self._live.get("latency") or 0.0)
                self._live["latency"] = spent if old <= 0 else round(old * 0.6 + spent * 0.4, 3)
