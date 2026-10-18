# -*- coding: utf-8 -*-
"""设置抽屉：音频、识别、翻译、界面外观、悬浮字幕、关于。"""
from __future__ import annotations

import threading
import shutil
import re
import subprocess
import tempfile
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..core import audio as audio_mod
from ..core.asr import FinalASR
from ..core.config import TRANSLATE_PRESETS
from ..core.ollama import RECOMMENDED as OLLAMA_RECOMMENDED
from ..core.translate import Translator
from ..paths import BACKGROUNDS, MODELS
from . import theme as theme_mod
from .background import background_files, load_credits


def _hline() -> QtWidgets.QFrame:
    f = QtWidgets.QFrame()
    f.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    f.setStyleSheet("color: rgba(255,255,255,0.06);")
    f.setFixedHeight(1)
    return f


def breakable(text: str) -> str:
    """在长路径/文件名里插入零宽空格，让它们能换行——否则会把设置面板撑宽、右边被裁掉。"""
    out: list[str] = []
    for ch in str(text):
        out.append(ch)
        if ch in "/\\_-.":
            out.append("\u200b")
    return "".join(out)


class SettingsPanel(QtWidgets.QWidget):
    """右侧设置面板；任何改动都会写回 config 并发出 changed 信号。"""

    changed = QtCore.Signal(str, object)
    test_done = QtCore.Signal(str)
    ollama_models_ready = QtCore.Signal(object)
    pull_progress = QtCore.Signal(str, int, int)
    pull_finished = QtCore.Signal(str)
    mt_progress = QtCore.Signal(str, int, int)
    mt_finished = QtCore.Signal(str)
    speed_ollama = QtCore.Signal(str)
    speed_mt = QtCore.Signal(str)
    speed_llama = QtCore.Signal(str)
    models_pulled = QtCore.Signal(object)
    eng_progress = QtCore.Signal(str, int, int)
    eng_finished = QtCore.Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._llama_job = None
        self.setMinimumWidth(600)
        self.pages = QtWidgets.QStackedWidget()
        self.nav = QtWidgets.QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(116)
        self.nav.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        shell = QtWidgets.QHBoxLayout(self)
        shell.setContentsMargins(14, 14, 14, 14)
        shell.setSpacing(14)
        shell.addWidget(self.nav)
        shell.addWidget(self.pages, 1)
        self._build(None)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._guard_wheel()
        self.apply_palette()

    def _new_page(self, title):
        self.nav.addItem(title)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root = QtWidgets.QWidget()
        root.setObjectName("settingsPage")
        root.setStyleSheet("QWidget#settingsPage { background: transparent; }")
        self.vbox = QtWidgets.QVBoxLayout(root)
        self.vbox.setContentsMargins(0, 0, 10, 8)
        self.vbox.setSpacing(14)
        area.setWidget(root)
        self.pages.addWidget(area)

    def verticalScrollBar(self):
        return self.pages.currentWidget().verticalScrollBar()

    def widget(self):
        return self.pages.currentWidget().widget()

    def apply_palette(self):
        light = self.cfg.get("ui.theme", "light") == "light"
        for label in self.findChildren(QtWidgets.QLabel):
            original = label.property("originalStyle")
            if original is None:
                original = label.styleSheet()
                label.setProperty("originalStyle", original)
            style = str(original)
            if light:
                style = re.sub(r"color:\s*rgba\([^)]*\)", "color: #667085", style)
                for old, new in (("#9fdcae", "#167744"), ("#ffb3b3", "#c53444")):
                    style = style.replace(old, new)
            label.setStyleSheet(style)

    # ------------------------------------------------------------- 工具
    def _make_progress(self, parent_lay) -> tuple[QtWidgets.QProgressBar, QtWidgets.QLabel]:
        bar = QtWidgets.QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        bar.hide()
        lbl = QtWidgets.QLabel("")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size: 11px; color: rgba(170,178,195,0.95);")
        lbl.hide()
        parent_lay.addWidget(bar)
        parent_lay.addWidget(lbl)
        return bar, lbl

    def _combo(self) -> QtWidgets.QComboBox:
        c = QtWidgets.QComboBox()
        c.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(8)
        c.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        return c

    def _section(self, title: str) -> QtWidgets.QVBoxLayout:
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)
        lbl = QtWidgets.QLabel(title)
        lbl.setObjectName("sectionTitle")
        lay.addWidget(lbl)
        self.vbox.addWidget(card)
        return lay

    def _row(self, parent_lay, label: str, sub: str = "") -> QtWidgets.QHBoxLayout:
        holder = QtWidgets.QVBoxLayout()
        holder.setSpacing(2)
        line = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(label)
        name.setFixedWidth(84)
        name.setStyleSheet("color: rgba(205,212,225,0.9);")
        line.addWidget(name)
        line.addStretch(0)
        holder.addLayout(line)
        if sub:
            tip = QtWidgets.QLabel(sub)
            tip.setObjectName("faint")
            tip.setWordWrap(True)
            tip.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.9);")
            holder.addWidget(tip)
        parent_lay.addLayout(holder)
        return line

    def _set(self, path: str, value, signal: str) -> None:
        self.cfg.set(path, value)
        self.changed.emit(path, value)
        if path == "ui.theme":
            self.apply_palette()

    def _bind_combo(self, combo: QtWidgets.QComboBox, path: str) -> None:
        combo.currentIndexChanged.connect(
            lambda _i: self._set(path, combo.itemData(combo.currentIndex()), None))

    def _bind_check(self, box: QtWidgets.QCheckBox, path: str) -> None:
        box.toggled.connect(lambda v: self._set(path, bool(v), None))

    def _bind_slider(self, slider: QtWidgets.QSlider, path: str, mapper=None) -> None:
        def _on(v: int) -> None:
            val = mapper(v) if mapper else v
            self._set(path, val, None)
        slider.valueChanged.connect(_on)

    # ---- 防止「滚动时误改设置」 -------------------------------------------
    def _guard_wheel(self) -> None:
        """在设置面板里滚动时，鼠标划过下拉框/滑条不应把值改掉（要改得先点它）。"""
        for cls in (QtWidgets.QComboBox, QtWidgets.QSlider, QtWidgets.QSpinBox,
                    QtWidgets.QDoubleSpinBox):
            for w in self.findChildren(cls):
                w.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        if (ev.type() == QtCore.QEvent.Type.Wheel
                and isinstance(obj, (QtWidgets.QComboBox, QtWidgets.QSlider,
                                     QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox))):
            # 即使刚点过控件、它仍有焦点，滚轮也只滚动设置页，不修改选项。
            delta = ev.pixelDelta().y() or ev.angleDelta().y()
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - int(delta))
            ev.accept()
            return True
        return bool(super().eventFilter(obj, ev))

    # ------------------------------------------------------------- 构建
    def _build(self, vbox) -> None:
        for title, builders in (
            ("音频与识别", (self._build_audio, self._build_asr)),
            ("翻译与模型", (self._build_translate,)),
            ("字幕与外观", (self._build_ui,)),
            ("悬浮字幕", (self._build_overlay,)),
            ("关于", (self._build_about,)),
        ):
            self._new_page(title)
            for build in builders:
                build()
            self.vbox.addStretch(1)

    # ---- 音频 ---------------------------------------------------------
    def _build_audio(self) -> None:
        lay = self._section("音频来源")
        line = self._row(lay, "来源")
        self.cmb_source = self._combo()
        self.cmb_source.addItem("系统声音（直播）", "loopback")
        self.cmb_source.addItem("麦克风（说话）", "mic")
        cur = str(self.cfg.get("audio.source", "loopback"))
        self.cmb_source.setCurrentIndex(0 if cur == "loopback" else 1)
        self._bind_combo(self.cmb_source, "audio.source")
        line.addWidget(self.cmb_source)

        line2 = self._row(lay, "设备")
        self.cmb_device = self._combo()
        btn_ref = QtWidgets.QPushButton("↻")
        btn_ref.setObjectName("ghost")
        btn_ref.setFixedWidth(34)
        btn_ref.clicked.connect(self._refresh_devices)
        line2.addWidget(self.cmb_device, 1)
        line2.addWidget(btn_ref)
        self._refresh_devices()
        sub = QtWidgets.QLabel("系统声音默认捕获「扬声器/耳机」里正在播放的内容；若你戴蓝牙耳机，请先播放再点刷新。")
        sub.setWordWrap(True)
        sub.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.9);")
        lay.addWidget(sub)

        line3 = self._row(lay, "断句速度", f"当前：{self.cfg.get('vad.min_silence_ms', 350)} 毫秒静音后断句")
        self.sld_silence = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sld_silence.setRange(150, 900)
        self.sld_silence.setValue(int(self.cfg.get("vad.min_silence_ms", 350)))
        self._lbl_silence = QtWidgets.QLabel(f"{self.sld_silence.value()} ms")
        self._lbl_silence.setFixedWidth(58)
        self._lbl_silence.setStyleSheet("color: rgba(200,208,220,0.85);")

        def _sil(v: int) -> None:
            self._lbl_silence.setText(f"{v} ms")
            self._set("vad.min_silence_ms", int(v), None)
        self.sld_silence.valueChanged.connect(_sil)
        line3.addWidget(self.sld_silence, 1)
        line3.addWidget(self._lbl_silence)

        line4 = self._row(lay, "人声灵敏度")
        self.sld_thresh = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sld_thresh.setRange(30, 80)
        self.sld_thresh.setValue(int(float(self.cfg.get("vad.threshold", 0.5)) * 100))
        self._bind_slider(self.sld_thresh, "vad.threshold", lambda v: round(v / 100.0, 2))
        line4.addWidget(self.sld_thresh, 1)

    def _refresh_devices(self) -> None:
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self.cmb_device.addItem("默认设备（推荐）", "")
        devs = audio_mod.list_devices()
        src = self.cmb_source.currentData() if hasattr(self, "cmb_source") else "loopback"
        for name in devs.get("mic" if src == "mic" else "loopback", []):
            self.cmb_device.addItem(name, name)
        cur = str(self.cfg.get("audio.device", "") or "")
        idx = self.cmb_device.findData(cur)
        self.cmb_device.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_device.blockSignals(False)
        self.cmb_device.currentIndexChanged.connect(
            lambda _i: self._set("audio.device", self.cmb_device.currentData(), None))

    # ---- 识别 ---------------------------------------------------------
    def _build_asr(self) -> None:
        lay = self._section("语音识别")
        self.cfg.update_many({"asr.final_engine": "parakeet"}, save=False)
        self.cfg.save()

        ready = FinalASR.available("parakeet")
        status = QtWidgets.QLabel(
            ("✅ 日语高速识别已就绪" if ready else "❌ 日语识别模型缺失")
            + "\n日语专用 Parakeet：使用 CPU 识别人声，把显卡留给翻译。无需选择识别模型。"
        )
        status.setWordWrap(True)
        status.setStyleSheet(
            "font-size: 12px; color: " + ("#9fdcae;" if ready else "#ffb3b3;")
        )
        lay.addWidget(status)

        line0 = self._row(lay, "实时字幕", "约每 0.5 秒更新识别；译文边生成边显示，关闭可降低 CPU 占用")
        self.cmb_draft = self._combo()
        self.cmb_draft.addItem("开启（推荐）", "rolling")
        self.cmb_draft.addItem("关闭", "off")
        cur_draft = str(self.cfg.get("asr.draft_mode", "rolling"))
        if not bool(self.cfg.get("asr.partial", True)) or cur_draft == "off":
            cur_draft = "off"
        else:
            cur_draft = "rolling"
        i = self.cmb_draft.findData(cur_draft)
        self.cmb_draft.setCurrentIndex(max(0, i))
        self._bind_combo(self.cmb_draft, "asr.draft_mode")
        line0.addWidget(self.cmb_draft, 1)

        line2 = self._row(lay, "最长句长", "超长会自动切断，避免翻译跟不上")
        self.spn_max = QtWidgets.QSpinBox()
        self.spn_max.setRange(8, 30)
        self.spn_max.setSuffix(" 秒")
        self.spn_max.setValue(int(self.cfg.get("asr.max_segment_s", 24)))
        self.spn_max.valueChanged.connect(lambda v: self._set("asr.max_segment_s", int(v), None))
        line2.addWidget(self.spn_max)

    # ---- 翻译 ---------------------------------------------------------
    def _build_translate(self) -> None:
        lay = self._section("翻译服务")
        box = QtWidgets.QCheckBox("启用翻译")
        box.setChecked(bool(self.cfg.get("translate.enabled", True)))
        self._bind_check(box, "translate.enabled")
        lay.addWidget(box)
        tip = QtWidgets.QLabel("默认使用本地大模型，无需密钥。可按需要切换轻量模型、其他本地模型或云端 API。")
        tip.setWordWrap(True)
        tip.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.95);")
        lay.addWidget(tip)

        line = self._row(lay, "服务")
        self.cmb_provider = self._combo()
        for key, info in TRANSLATE_PRESETS.items():
            self.cmb_provider.addItem(info["label"], key)
        cur = str(self.cfg.get("translate.provider", "ms-free"))
        self.cmb_provider.setCurrentIndex(max(0, self.cmb_provider.findData(cur)))
        self.cmb_provider.currentIndexChanged.connect(self._on_provider_changed)
        line.addWidget(self.cmb_provider, 1)

        self.lbl_hint = QtWidgets.QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.95);")
        lay.addWidget(self.lbl_hint)

        self.svc_box = QtWidgets.QFrame()
        sl = QtWidgets.QVBoxLayout(self.svc_box)
        sl.setContentsMargins(0, 2, 0, 0)
        sl.setSpacing(6)
        self.ed_url = QtWidgets.QLineEdit()
        self.ed_url.setPlaceholderText("Base URL（云端如 https://api.deepseek.com/v1；本地如 http://127.0.0.1:1234/v1）")
        self.ed_model = QtWidgets.QLineEdit()
        self.ed_model.setPlaceholderText("模型名")
        self.ed_key = QtWidgets.QLineEdit()
        self.ed_key.setPlaceholderText("API Key（本地服务可留空）")
        self.ed_key.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        for ed in (self.ed_url, self.ed_model, self.ed_key):
            ed.editingFinished.connect(self._save_provider_fields)
            sl.addWidget(ed)
        row_pull = QtWidgets.QHBoxLayout()
        self.btn_pull_list = QtWidgets.QPushButton("拉取模型列表")
        self.btn_pull_list.setObjectName("ghost")
        self.btn_pull_list.setToolTip("从该地址读取可用模型（支持 Ollama / LM Studio / llama.cpp / vLLM 等本地服务，云端 OpenAI 兼容服务也支持）")
        self.btn_pull_list.clicked.connect(self._fetch_model_list)
        self.cmb_pulled = self._combo()
        self.cmb_pulled.addItem("（点左侧按钮拉取模型列表）", "")
        self.cmb_pulled.currentIndexChanged.connect(self._on_pulled_model)
        row_pull.addWidget(self.btn_pull_list)
        row_pull.addWidget(self.cmb_pulled, 1)
        sl.addLayout(row_pull)
        row = QtWidgets.QHBoxLayout()
        self.btn_test = QtWidgets.QPushButton("测试连接")
        self.btn_test.setObjectName("ghost")
        self.btn_test.clicked.connect(self._test_provider)
        self.lbl_test = QtWidgets.QLabel("")
        self.lbl_test.setWordWrap(True)
        self.lbl_test.setStyleSheet("font-size: 11px;")
        row.addWidget(self.btn_test)
        row.addWidget(self.lbl_test, 1)
        sl.addLayout(row)
        lay.addWidget(self.svc_box)

        # ---- 内置极速本地翻译面板
        self.mt_box = QtWidgets.QFrame()
        ml = QtWidgets.QVBoxLayout(self.mt_box)
        ml.setContentsMargins(0, 2, 0, 0)
        ml.setSpacing(6)
        self.lbl_mt_status = QtWidgets.QLabel("正在检查内置模型…")
        self.lbl_mt_status.setWordWrap(True)
        self.lbl_mt_status.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        ml.addWidget(self.lbl_mt_status)
        row_mt = QtWidgets.QHBoxLayout()
        self.btn_mt_download = QtWidgets.QPushButton("一键下载内置翻译模型（约 100MB）")
        self.btn_mt_download.setObjectName("ghost")
        self.btn_mt_download.clicked.connect(self._download_localmt)
        self.btn_mt_speed = QtWidgets.QPushButton("测速")
        self.btn_mt_speed.setObjectName("ghost")
        self.btn_mt_speed.clicked.connect(self._speed_localmt)
        row_mt.addWidget(self.btn_mt_download, 1)
        row_mt.addWidget(self.btn_mt_speed)
        ml.addLayout(row_mt)
        self.mt_bar, self.lbl_mt_prog = self._make_progress(ml)
        self.lbl_mt_result = QtWidgets.QLabel("")
        self.lbl_mt_result.setWordWrap(True)
        self.lbl_mt_result.setStyleSheet("font-size: 11px; color: rgba(160,220,170,0.95);")
        ml.addWidget(self.lbl_mt_result)
        note_mt = QtWidgets.QLabel("模型为 Opus-MT（Argos Translate，CC-BY 4.0），下载一次后完全离线，不联网也能翻译。")
        note_mt.setWordWrap(True)
        note_mt.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.92);")
        ml.addWidget(note_mt)
        lay.addWidget(self.mt_box)

        # ---- 内置引擎（llama.cpp）面板
        self.llama_box = QtWidgets.QFrame()
        el = QtWidgets.QVBoxLayout(self.llama_box)
        el.setContentsMargins(0, 2, 0, 0)
        el.setSpacing(6)
        self.lbl_llama_status = QtWidgets.QLabel("正在检查内置引擎…")
        self.lbl_llama_status.setWordWrap(True)
        self.lbl_llama_status.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        el.addWidget(self.lbl_llama_status)
        engine_grid = QtWidgets.QGridLayout()
        engine_grid.setHorizontalSpacing(12)
        self.engine_status_labels = {}
        self.engine_download_buttons = {}
        for row, (variant, title) in enumerate((
            ('cpu', '通用引擎（CPU）'),
            ('vulkan', '显卡加速（Vulkan）'),
            ('cuda', 'NVIDIA 加速（CUDA）'),
        )):
            label = QtWidgets.QLabel(title)
            state = QtWidgets.QLabel('检查中')
            button = QtWidgets.QPushButton('下载')
            button.setObjectName('ghost')
            button.setMinimumWidth(88)
            button.clicked.connect(lambda checked=False, v=variant: self._download_llama_engine(v))
            engine_grid.addWidget(label, row, 0)
            engine_grid.addWidget(state, row, 1)
            engine_grid.addWidget(button, row, 2)
            self.engine_status_labels[variant] = state
            self.engine_download_buttons[variant] = button
        engine_grid.setColumnStretch(0, 1)
        el.addLayout(engine_grid)
        self.btn_engine_cuda = self.engine_download_buttons['cuda']
        row_var = QtWidgets.QHBoxLayout()
        row_var.addWidget(QtWidgets.QLabel("用哪个引擎"))
        self.cmb_engine_variant = self._combo()
        row_var.addWidget(self.cmb_engine_variant, 1)
        el.addLayout(row_var)
        self._reload_engine_variants()
        self.cmb_engine_variant.currentIndexChanged.connect(self._on_engine_variant_changed)
        note_var = QtWidgets.QLabel(
            "引擎任选一种即可，无需全部下载。CPU 通用，显卡加速可选。更改在下一次翻译时生效；"
            "自动模式会选择可用引擎，显卡启动失败时尝试 CPU。")
        note_var.setWordWrap(True)
        note_var.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.92);")
        el.addWidget(note_var)
        row_m = QtWidgets.QHBoxLayout()
        b_gf = QtWidgets.QPushButton("选择本地模型文件…")
        self.btn_pick_gguf = b_gf
        b_gf.setObjectName("ghost")
        b_gf.clicked.connect(self._pick_gguf)
        b_im = QtWidgets.QPushButton("导入 Ollama 模型")
        self.btn_import_ollama = b_im
        b_im.setObjectName("ghost")
        b_im.setToolTip("把 Ollama 里已经下载好的模型文件复制进来，不用重新下载")
        b_im.clicked.connect(self._import_ollama_model)
        row_m.addWidget(b_gf)
        row_m.addWidget(b_im)
        el.addLayout(row_m)
        row_md = QtWidgets.QHBoxLayout()
        b_dl = QtWidgets.QPushButton("检查推荐模型…")
        self.btn_llama_model = b_dl
        b_dl.setObjectName("ghost")
        b_dl.clicked.connect(self._download_llama_model)
        row_md.addWidget(b_dl)
        el.addLayout(row_md)
        self.llama_bar, self.lbl_llama_prog = self._make_progress(el)
        row_s = QtWidgets.QHBoxLayout()
        b_sp = QtWidgets.QPushButton("测速（翻译一句试试）")
        b_sp.setObjectName("ghost")
        b_sp.clicked.connect(self._speed_llamacpp)
        row_s.addWidget(b_sp)
        row_s.addStretch(1)
        el.addLayout(row_s)
        self.lbl_llama_result = QtWidgets.QLabel("")
        self.lbl_llama_result.setWordWrap(True)
        self.lbl_llama_result.setStyleSheet("font-size: 11px; color: rgba(160,220,170,0.95);")
        el.addWidget(self.lbl_llama_result)
        note_el = QtWidgets.QLabel("完全免安装：引擎和模型都放在软件文件夹里，用户不需要装 Ollama。"
                                   "首次运行需要加载模型，随后常驻内存。译文边生成边显示，速度取决于电脑性能。")
        note_el.setWordWrap(True)
        note_el.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.92);")
        el.addWidget(note_el)
        lay.addWidget(self.llama_box)

        # ---- Ollama 本地大模型面板
        self.ollama_box = QtWidgets.QFrame()
        ol = QtWidgets.QVBoxLayout(self.ollama_box)
        ol.setContentsMargins(0, 2, 0, 0)
        ol.setSpacing(6)
        self.lbl_ollama_status = QtWidgets.QLabel("正在检测 Ollama…")
        self.lbl_ollama_status.setWordWrap(True)
        self.lbl_ollama_status.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        ol.addWidget(self.lbl_ollama_status)
        row_o = QtWidgets.QHBoxLayout()
        self.cmb_ollama_model = self._combo()
        self.cmb_ollama_model.currentIndexChanged.connect(self._on_ollama_model_pick)
        self.btn_ollama_refresh = QtWidgets.QPushButton("↻ 刷新")
        self.btn_ollama_refresh.setObjectName("ghost")
        self.btn_ollama_refresh.clicked.connect(lambda: self._refresh_ollama(verbose=True))
        row_o.addWidget(self.cmb_ollama_model, 1)
        row_o.addWidget(self.btn_ollama_refresh)
        ol.addLayout(row_o)
        note_o = QtWidgets.QLabel(
            "体积越小越快；也支持任意第三方模型（下载更大的或导入 GGUF 都行）——能跑，"
            "但这台机器上模型越大每句耗时越长。连接走原生接口，自动「关闭思考、模型常驻」。")
        note_o.setWordWrap(True)
        note_o.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.92);")
        ol.addWidget(note_o)
        self.ollama_dl_box = QtWidgets.QFrame()
        od = QtWidgets.QVBoxLayout(self.ollama_dl_box)
        od.setContentsMargins(0, 0, 0, 0)
        od.setSpacing(4)
        lbl_dl = QtWidgets.QLabel("推荐模型（点一下自动下载）；想用任何第三方模型，看下面两行：")
        lbl_dl.setWordWrap(True)
        lbl_dl.setStyleSheet("font-size: 11px; color: rgba(165,172,190,0.95);")
        od.addWidget(lbl_dl)
        for name, desc, gb, tag in OLLAMA_RECOMMENDED:
            b = QtWidgets.QPushButton(f"{name}（{tag} · 约 {gb:g}GB）")
            b.setToolTip(f"{desc}\n点击开始下载（需要 Ollama 正在运行）")
            b.setObjectName("ghost")
            b.clicked.connect(lambda _c=False, m=name: self._pull_recommended(m))
            od.addWidget(b)
        row_any = QtWidgets.QHBoxLayout()
        self.ed_ollama_any = QtWidgets.QLineEdit()
        self.ed_ollama_any.setPlaceholderText("任意模型名，如 qwen3:14b、gemma3:12b、hf.co/作者/仓库:Q4_K_M")
        b_any = QtWidgets.QPushButton("下载")
        b_any.setObjectName("ghost")
        b_any.clicked.connect(self._pull_any)
        row_any.addWidget(self.ed_ollama_any, 1)
        row_any.addWidget(b_any)
        od.addLayout(row_any)
        b_gguf = QtWidgets.QPushButton("导入自己下载的 GGUF 模型文件…")
        b_gguf.setObjectName("ghost")
        b_gguf.setToolTip("从 HuggingFace / ModelScope / 网盘等任何来源下载的 .gguf 文件，导入后即可在模型列表中使用")
        b_gguf.clicked.connect(self._import_gguf)
        od.addWidget(b_gguf)
        ol.addWidget(self.ollama_dl_box)
        self.ollama_bar, self.lbl_ollama_prog = self._make_progress(ol)
        row_os = QtWidgets.QHBoxLayout()
        self.btn_ollama_speed = QtWidgets.QPushButton("测速（翻译一句试试）")
        self.btn_ollama_speed.setObjectName("ghost")
        self.btn_ollama_speed.clicked.connect(self._speed_ollama)
        self.btn_ollama_site = QtWidgets.QPushButton("打开 Ollama 官网下载")
        self.btn_ollama_site.setObjectName("ghost")
        self.btn_ollama_site.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://ollama.com/download")))
        self.lbl_ollama_result = QtWidgets.QLabel("")
        self.lbl_ollama_result.setWordWrap(True)
        self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: rgba(160,220,170,0.95);")
        row_os.addWidget(self.btn_ollama_speed)
        row_os.addWidget(self.btn_ollama_site)
        row_os.addStretch(1)
        ol.addLayout(row_os)
        ol.addWidget(self.lbl_ollama_result)
        lay.addWidget(self.ollama_box)

        # ---- 信号接线
        self.ollama_models_ready.connect(self._on_ollama_models)
        self.pull_progress.connect(self._on_pull_progress)
        self.pull_finished.connect(self._on_pull_finished)
        self.mt_progress.connect(self._on_mt_progress)
        self.mt_finished.connect(self._on_mt_finished)
        self.speed_ollama.connect(self._on_speed_ollama)
        self.speed_mt.connect(self._on_speed_mt)
        self.speed_llama.connect(self._on_speed_llama)
        self.models_pulled.connect(self._on_models_pulled)
        self.eng_progress.connect(self._on_eng_progress)
        self.eng_finished.connect(self._on_eng_finished)

        self._on_provider_changed(None, init=True)
        if self.cfg.get("translate.provider") == "ollama":
            self._refresh_ollama()
        if self.cfg.get("translate.provider") == "llamacpp":
            self._refresh_llama_status()
        self._refresh_localmt_status()

        box2 = QtWidgets.QCheckBox("边识别边翻译（更实时，消耗略高）")
        box2.setChecked(bool(self.cfg.get("translate.live", True)))
        self._bind_check(box2, "translate.live")
        lay.addWidget(box2)
        line_live = self._row(lay, "草稿译文",
                              "推荐与整句使用同一翻译模型，避免草稿和整句质量不一致。自动模式会直接使用混元模型。")
        self.cmb_live_prov = self._combo()
        self.cmb_live_prov.addItem("自动", "auto")
        self.cmb_live_prov.addItem("与整句相同（推荐）", "same")
        self.cmb_live_prov.addItem("始终用极速内置", "localmt")
        i = self.cmb_live_prov.findData(str(self.cfg.get("translate.live_provider", "auto")))
        self.cmb_live_prov.setCurrentIndex(max(0, i))
        self._bind_combo(self.cmb_live_prov, "translate.live_provider")
        line_live.addWidget(self.cmb_live_prov, 1)

        fallback = QtWidgets.QCheckBox("翻译失败时，允许尝试其他免密钥在线服务")
        fallback.setChecked(bool(self.cfg.get("translate.auto_fallback", False)))
        self._bind_check(fallback, "translate.auto_fallback")
        lay.addWidget(fallback)

        # 术语表
        line3 = self._row(lay, "术语表", "固定一些昵称/专有名词的译法，例如 ぺこら→佩克拉")
        self.tbl = QtWidgets.QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["日语", "中文"])
        self.tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setFixedHeight(120)
        for a, b in (self.cfg.get("translate.glossary", []) or []):
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            self.tbl.setItem(r, 0, QtWidgets.QTableWidgetItem(str(a)))
            self.tbl.setItem(r, 1, QtWidgets.QTableWidgetItem(str(b)))
        self.tbl.itemChanged.connect(lambda _it: self._save_glossary())
        lay.addWidget(self.tbl)
        row3 = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("+ 添加")
        b_add.setObjectName("ghost")
        b_del = QtWidgets.QPushButton("删除选中")
        b_del.setObjectName("ghost")
        b_add.clicked.connect(lambda: self.tbl.insertRow(self.tbl.rowCount()))
        b_del.clicked.connect(self._del_glossary_row)
        row3.addWidget(b_add)
        row3.addWidget(b_del)
        row3.addStretch(1)
        lay.addLayout(row3)

    def _save_glossary(self) -> None:
        items = []
        for r in range(self.tbl.rowCount()):
            a = self.tbl.item(r, 0)
            b = self.tbl.item(r, 1)
            a = a.text().strip() if a else ""
            b = b.text().strip() if b else ""
            if a and b:
                items.append([a, b])
        self._set("translate.glossary", items, None)

    def _del_glossary_row(self) -> None:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)
        self._save_glossary()

    # ---- 翻译服务字段 ---------------------------------------------------
    def _on_provider_changed(self, _idx, init: bool = False) -> None:
        key = self.cmb_provider.currentData()
        info = TRANSLATE_PRESETS.get(key, {})
        if not init:
            self.cfg.set("translate.provider", key)
            self.changed.emit("translate.provider", key)
        svc = self.cfg.service(key)
        self.ed_url.setText(svc.get("base_url", ""))
        self.ed_model.setText(svc.get("model", ""))
        self.ed_key.setText(svc.get("api_key", ""))
        self.lbl_hint.setText(str(info.get("key_hint", "")))
        kind = info.get("kind", "openai")
        self.svc_box.setVisible(kind in ("openai", "deepl"))
        self.ollama_box.setVisible(kind == "ollama")
        self.mt_box.setVisible(kind == "localmt")
        self.llama_box.setVisible(kind == "llamacpp")
        self.lbl_test.setText("")
        if kind == "ollama" and not init:
            self._refresh_ollama()
        if kind == "localmt":
            self._refresh_localmt_status()
        if kind == "llamacpp":
            self._refresh_llama_status()

    def _save_provider_fields(self) -> None:
        key = self.cmb_provider.currentData()
        self.cfg.set_service(key, base_url=self.ed_url.text().strip(),
                             model=self.ed_model.text().strip(),
                             api_key=self.ed_key.text().strip())
        self.changed.emit("translate.services", None)

    def _test_provider(self) -> None:
        key = self.cmb_provider.currentData()
        self._save_provider_fields()
        self.btn_test.setEnabled(False)
        self.lbl_test.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_test.setText("测试中…")

        def work() -> None:
            try:
                out = Translator(self.cfg).test(key)
            except Exception as e:  # noqa: BLE001
                out = f"连接失败：{e}"
            self.test_done.emit(out)

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, out: str) -> None:
        color = "#7ddc9a" if out.startswith("连接成功") else "#ff9a9a"
        self.lbl_test.setStyleSheet(f"font-size: 11px; color: {color};")
        self.lbl_test.setText(out)
        self.btn_test.setEnabled(True)

    # ---- 界面外观 -------------------------------------------------------
    def _build_ui(self) -> None:
        lay = self._section("界面外观")
        self.test_done.connect(self._on_test_done)

        line = self._row(lay, "主题")
        self.cmb_theme = self._combo()
        self.cmb_theme.addItem("深色", "dark")
        self.cmb_theme.addItem("白色（默认）", "light")
        idx = self.cmb_theme.findData(str(self.cfg.get("ui.theme", "light")))
        self.cmb_theme.setCurrentIndex(max(0, idx))
        self._bind_combo(self.cmb_theme, "ui.theme")
        line.addWidget(self.cmb_theme, 1)

        self._row(lay, "强调色")
        sw = QtWidgets.QHBoxLayout()
        sw.setSpacing(6)
        cur_accent = str(self.cfg.get("ui.accent", "#ff86b3")).lower()
        for name, color in theme_mod.ACCENT_PRESETS:
            b = QtWidgets.QPushButton()
            b.setFixedSize(26, 26)
            b.setToolTip(name)
            ring = "3px solid rgba(255,255,255,0.85)" if color.lower() == cur_accent else "1px solid rgba(255,255,255,0.25)"
            b.setStyleSheet(f"QPushButton {{ background: {color}; border-radius: 13px; border: {ring}; }}")
            b.clicked.connect(lambda _c=False, col=color: self._pick_accent(col))
            sw.addWidget(b)
        b_custom = QtWidgets.QPushButton("自定义…")
        b_custom.setObjectName("ghost")
        b_custom.clicked.connect(self._custom_accent)
        sw.addWidget(b_custom)
        sw.addStretch(1)
        lay.addLayout(sw)

        self._row(lay, "自定义背景", "默认使用纯色背景。可选择自己的图片，也可把图片直接拖入主窗口；摄影背景遵守各自素材授权。")
        row_bg = QtWidgets.QHBoxLayout()
        self.bg_list = QtWidgets.QListWidget()
        self.bg_list.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.bg_list.setIconSize(QtCore.QSize(96, 54))
        self.bg_list.setGridSize(QtCore.QSize(104, 84))
        self.bg_list.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.bg_list.setMovement(QtWidgets.QListView.Movement.Static)
        self.bg_list.setFixedHeight(178)
        self.bg_list.itemClicked.connect(self._on_bg_pick)
        col_bg = QtWidgets.QVBoxLayout()
        b_pick = QtWidgets.QPushButton("选择图片…")
        b_pick.setObjectName("ghost")
        b_pick.clicked.connect(self.pick_background_file)
        b_open = QtWidgets.QPushButton("打开文件夹")
        b_open.setObjectName("ghost")
        b_open.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(BACKGROUNDS))))
        b_reload_bg = QtWidgets.QPushButton("↻ 刷新")
        b_reload_bg.setObjectName("ghost")
        b_reload_bg.clicked.connect(self._reload_backgrounds)
        col_bg.addWidget(b_pick)
        col_bg.addWidget(b_open)
        col_bg.addWidget(b_reload_bg)
        col_bg.addStretch(1)
        row_bg.addWidget(self.bg_list, 1)
        row_bg.addLayout(col_bg)
        lay.addLayout(row_bg)
        self._reload_backgrounds()

        for label, path, lo, hi in (
            ("背景模糊", "ui.bg_blur", 0, 40),
            ("背景暗度", "ui.bg_dim", 0, 90),
            ("背景不透明度", "ui.bg_opacity", 30, 100),
        ):
            r = self._row(lay, label)
            s = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            s.setRange(lo, hi)
            s.setValue(int(self.cfg.get(path, (lo + hi) // 2)))
            lbl = QtWidgets.QLabel(str(s.value()))
            lbl.setFixedWidth(34)
            s.valueChanged.connect(lambda v, p=path, L=lbl: (L.setText(str(v)), self._set(p, int(v), None)))
            r.addWidget(s, 1)
            r.addWidget(lbl)

        r_font = self._row(lay, "字体")
        self.cmb_font = self._combo()
        cur_font = str(self.cfg.get("ui.font", "系统默认"))
        fonts = list(theme_mod.FONT_PRESETS)
        if cur_font and cur_font not in fonts:
            fonts.insert(1, cur_font)
        for f in fonts:
            self.cmb_font.addItem(f, "" if f == "系统默认" else f)
        i = self.cmb_font.findText(cur_font)
        self.cmb_font.setCurrentIndex(max(0, i))
        self._bind_combo(self.cmb_font, "ui.font")
        r_font.addWidget(self.cmb_font, 1)

        for label, path, lo, hi in (
            ("日语字号", "ui.jp_size", 10, 28),
            ("译文字号", "ui.zh_size", 12, 40),
        ):
            r = self._row(lay, label)
            sp = QtWidgets.QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(int(self.cfg.get(path, lo)))
            sp.valueChanged.connect(lambda v, p=path: self._set(p, int(v), None))
            r.addWidget(sp)

        r = self._row(lay, "字幕保留行数")
        self.txt_history_lines = QtWidgets.QLineEdit()
        self.txt_history_lines.setObjectName("history_lines")
        self.txt_history_lines.setValidator(
            QtGui.QRegularExpressionValidator(QtCore.QRegularExpression("[0-9]*"), self))
        self.txt_history_lines.setToolTip("直接输入任意正整数，不设最大行数；按回车或点击其他位置生效")
        self.txt_history_lines.setPlaceholderText("输入行数，如 1000")
        self.chk_history_all = QtWidgets.QCheckBox("保留全部")
        keep = int(self.cfg.get("ui.max_lines", 8))
        self.txt_history_lines.setText(str(keep) if keep > 0 else "1000")
        self.chk_history_all.setChecked(keep <= 0)
        self.txt_history_lines.setEnabled(keep > 0)

        def save_history_lines():
            if self.chk_history_all.isChecked():
                return
            value = self.txt_history_lines.text().strip()
            if value and int(value) > 0:
                self._set("ui.max_lines", int(value), None)
            else:
                previous = int(self.cfg.get("ui.max_lines", 8))
                self.txt_history_lines.setText(str(previous if previous > 0 else 1000))

        def toggle_history_all(checked):
            self.txt_history_lines.setEnabled(not checked)
            if checked:
                self._set("ui.max_lines", 0, None)
            else:
                save_history_lines()

        self.txt_history_lines.editingFinished.connect(save_history_lines)
        self.chk_history_all.toggled.connect(toggle_history_all)
        r.addWidget(self.txt_history_lines, 1)
        r.addWidget(self.chk_history_all)

        r_disp = self._row(lay, "显示模式")
        self.cmb_display = self._combo()
        self.cmb_display.addItem("双语（原文+译文）", "both")
        self.cmb_display.addItem("只看译文", "zh")
        self.cmb_display.addItem("只看原文", "jp")
        i = self.cmb_display.findData(str(self.cfg.get("ui.display", "both")))
        self.cmb_display.setCurrentIndex(max(0, i))
        self._bind_combo(self.cmb_display, "ui.display")
        r_disp.addWidget(self.cmb_display, 1)

    def _pick_accent(self, color: str) -> None:
        self._set("ui.accent", color, None)

    def _custom_accent(self) -> None:
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(str(self.cfg.get("ui.accent", "#ff86b3"))), self)
        if c.isValid():
            self._set("ui.accent", c.name(), None)

    def _reload_backgrounds(self) -> None:
        self.bg_list.clear()
        cur = str(self.cfg.get("ui.bg", "auto"))

        def make_icon(path) -> QtGui.QIcon:
            pm = QtGui.QPixmap(str(path))
            if pm.isNull():
                return QtGui.QIcon()
            pm = pm.scaled(192, 108, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           QtCore.Qt.TransformationMode.SmoothTransformation)
            x = max(0, (pm.width() - 192) // 2)
            y = max(0, (pm.height() - 108) // 2)
            return QtGui.QIcon(pm.copy(x, y, 192, 108))

        item_none = QtWidgets.QListWidgetItem("纯色 · 默认")
        item_none.setData(QtCore.Qt.ItemDataRole.UserRole, "none")
        self.bg_list.addItem(item_none)
        if not cur or cur == "none":
            self.bg_list.setCurrentItem(item_none)
        for f in background_files():
            item = QtWidgets.QListWidgetItem(make_icon(f), f.stem[:14])
            item.setData(QtCore.Qt.ItemDataRole.UserRole, f.name)
            item.setToolTip(f.name)
            self.bg_list.addItem(item)
            if source_matches(f.name, cur):
                self.bg_list.setCurrentItem(item)

    def _on_bg_pick(self, item: QtWidgets.QListWidgetItem) -> None:
        self._set("ui.bg", item.data(QtCore.Qt.ItemDataRole.UserRole), None)

    # ---- 自定义背景照片 ---------------------------------------------------
    def pick_background_file(self) -> None:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择背景照片", "", "图片文件 (*.jpg *.jpeg *.png *.webp *.bmp)")
        if not path:
            return
        self.add_background_file(Path(path))

    def add_background_file(self, src: Path) -> None:
        """把用户选择的图片复制进背景库并立即应用。"""
        from PIL import Image

        src = Path(src)
        try:
            with Image.open(src) as im:
                im.verify()
        except Exception:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "无法使用该图片", f"文件不是可识别的图片：\n{src}")
            return
        try:
            BACKGROUNDS.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w\-.一-龥]+", "_", src.stem)[:40] or "photo"
            dest = BACKGROUNDS / f"custom-{safe}{src.suffix.lower()}"
            shutil.copyfile(src, dest)
            self._reload_backgrounds()
            for i in range(self.bg_list.count()):
                it = self.bg_list.item(i)
                if it.data(QtCore.Qt.ItemDataRole.UserRole) == dest.name:
                    self.bg_list.setCurrentItem(it)
                    break
            self._set("ui.bg", dest.name, None)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "复制图片失败", str(e))

    # ---- 悬浮字幕 -------------------------------------------------------
    def _build_overlay(self) -> None:
        lay = self._section("悬浮字幕窗")
        box = QtWidgets.QCheckBox("显示悬浮字幕（置顶，可盖在直播画面上）")
        box.setChecked(bool(self.cfg.get("overlay.enabled", False)))
        self._bind_check(box, "overlay.enabled")
        lay.addWidget(box)
        note = QtWidgets.QLabel("拖动顶部移动；滚轮回看历史；A− / A＋调字号。向上翻阅时继续翻译，点击“回到最新”恢复跟随。")
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.9);")
        lay.addWidget(note)

        r = self._row(lay, "保留字幕")
        history_input = QtWidgets.QLineEdit()
        history_input.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression("[0-9]*"), self))
        count = int(self.cfg.get("overlay.lines", 0))
        history_input.setText(str(count) if count > 0 else "1000")
        retain_all = QtWidgets.QCheckBox("保留全部")
        retain_all.setChecked(count <= 0)
        history_input.setEnabled(count > 0)
        def save_count():
            if retain_all.isChecked():
                return
            value = history_input.text()
            if value and int(value) > 0:
                self._set("overlay.lines", int(value), None)
            else:
                history_input.setText(str(max(1, int(self.cfg.get("overlay.lines", 1000)))))
        def set_all(checked):
            history_input.setEnabled(not checked)
            if checked:
                self._set("overlay.lines", 0, None)
            else:
                save_count()
        history_input.editingFinished.connect(save_count)
        retain_all.toggled.connect(set_all)
        r.addWidget(history_input, 1)
        r.addWidget(retain_all)
        theme_row = self._row(lay, "浮窗底色")
        overlay_theme = self._combo()
        overlay_theme.addItem("深色", "dark")
        overlay_theme.addItem("白色", "light")
        overlay_theme.setCurrentIndex(max(0, overlay_theme.findData(self.cfg.get("overlay.theme", "dark"))))
        self._bind_combo(overlay_theme, "overlay.theme")
        theme_row.addWidget(overlay_theme, 1)
        restore = QtWidgets.QPushButton("恢复可操作状态（关闭鼠标穿透）")
        restore.clicked.connect(lambda: self._set("overlay.click_through", False, None))
        lay.addWidget(restore)

        r2 = self._row(lay, "译文字号")
        sp2 = QtWidgets.QSpinBox()
        self._overlay_font_spin = sp2
        sp2.setRange(14, 60)
        sp2.setValue(int(self.cfg.get("overlay.zh_size", 24)))
        sp2.valueChanged.connect(lambda v: self._set("overlay.zh_size", int(v), None))
        r2.addWidget(sp2)

        r3 = self._row(lay, "字幕框透明度")
        s3 = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        s3.setRange(0, 100)      # 拖到 0 = 完全去掉灰框，只留文字
        s3.setValue(int(self.cfg.get("overlay.opacity", 85)))
        s3.valueChanged.connect(lambda v: self._set("overlay.opacity", int(v), None))
        r3.addWidget(s3, 1)
        note_bg = QtWidgets.QLabel(
            "拖到最左边（0）= 完全去掉灰框，只留字幕文字（会自动加黑色描边，叠在画面上也看得清）；"
            "底板保持固定尺寸，字幕在内部滚动，不会随长句反复伸缩。")
        note_bg.setWordWrap(True)
        note_bg.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.9);")
        lay.addWidget(note_bg)

        c0 = QtWidgets.QCheckBox("译文描边（透明底可读；日文保持原字形）")
        c0.setChecked(bool(self.cfg.get("overlay.text_shadow", True)))
        self._bind_check(c0, "overlay.text_shadow")
        lay.addWidget(c0)

        c_jp = QtWidgets.QCheckBox("显示日语原文（只影响悬浮字幕，主窗口照旧）")
        c_jp.setChecked(bool(self.cfg.get("overlay.show_jp", True)))
        self._bind_check(c_jp, "overlay.show_jp")
        lay.addWidget(c_jp)

        c1 = QtWidgets.QCheckBox("鼠标穿透（点不到字幕窗，方便操作直播间）")
        c1.setChecked(bool(self.cfg.get("overlay.click_through", False)))
        self._bind_check(c1, "overlay.click_through")
        self._overlay_through_check = c1
        lay.addWidget(c1)
        c2 = QtWidgets.QCheckBox("锁定位置")
        c2.setChecked(bool(self.cfg.get("overlay.locked", False)))
        self._overlay_lock_check = c2
        def change_lock(locked: bool) -> None:
            self._set("overlay.locked", locked, None)
            if not locked and c1.isChecked():
                # 解锁后必须能拖动；从设置页解锁时顺手关闭鼠标穿透。
                c1.setChecked(False)
        c2.toggled.connect(change_lock)
        lay.addWidget(c2)

    def sync_overlay_controls(self) -> None:
        blocker = QtCore.QSignalBlocker(self._overlay_font_spin)
        self._overlay_font_spin.setValue(int(self.cfg.get("overlay.zh_size", 24)))
        del blocker
        for box, path in ((self._overlay_through_check, "overlay.click_through"),
                          (self._overlay_lock_check, "overlay.locked")):
            blocker = QtCore.QSignalBlocker(box)
            box.setChecked(bool(self.cfg.get(path, False)))
            del blocker

    # ---- 关于 -----------------------------------------------------------
    def _build_about(self) -> None:
        lay = self._section("关于与素材来源")
        from .. import __version__

        ver = QtWidgets.QLabel(f"译声实时翻译 v{__version__}")
        ver.setStyleSheet("color: rgba(215,222,235,0.95);")
        lay.addWidget(ver)

        self.lbl_models = QtWidgets.QLabel("")
        self.lbl_models.setWordWrap(True)
        self.lbl_models.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        lay.addWidget(self.lbl_models)
        b = QtWidgets.QPushButton("重新检测模型")
        b.setObjectName("ghost")
        b.clicked.connect(self._check_models)
        lay.addWidget(b)
        self._check_models()

        credits = load_credits().get("files", {})
        if credits:
            lines = ["背景照片来源（真实摄影作品）："]
            for fname, info in credits.items():
                lines.append(f"· {info.get('title', fname)} — {info.get('artist', '')}（{info.get('license', '')}）")
            lbl = QtWidgets.QLabel("\n".join(lines))
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.95);")
            lay.addWidget(lbl)

        tips = QtWidgets.QLabel("快捷键：F9 开始/停止聆听 · F10 显示/隐藏悬浮字幕 · Esc 收起设置面板")
        tips.setWordWrap(True)
        tips.setStyleSheet("font-size: 11px; color: rgba(150,158,175,0.95);")
        lay.addWidget(tips)

    def _check_models(self) -> None:
        rows = [
            ("日语高速识别 Parakeet", FinalASR.available("parakeet")),
            ("静音检测 silero_vad", (MODELS / "silero_vad.onnx").exists()),
        ]
        text = "本地模型状态：" + "  ".join(f"{'✅' if ok else '❌'}{name}" for name, ok in rows)
        self.lbl_models.setText(text)

    # ---- 本地模型：内置极速（Opus-MT / Argos） ---------------------------
    def _refresh_localmt_status(self) -> None:
        from ..core import local_mt

        if local_mt.installed("ja", "zh"):
            self.lbl_mt_status.setText("✅ 内置模型已就绪（日语→中文），选它就能完全离线翻译。")
            self.lbl_mt_status.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.btn_mt_download.setText("已安装，无需重复下载")
            self.btn_mt_download.setEnabled(False)
        elif local_mt.installed("ja", "en") and local_mt.installed("en", "zh"):
            self.lbl_mt_status.setText("✅ 内置模型已就绪（日语→英语→中文 两段式）。")
            self.lbl_mt_status.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.btn_mt_download.setText("已安装，无需重复下载")
            self.btn_mt_download.setEnabled(False)
        else:
            self.lbl_mt_status.setText("内置模型尚未下载：点下面按钮即可（约 100MB，失败可重试）。")
            self.lbl_mt_status.setStyleSheet("font-size: 11px; color: rgba(255,209,102,0.95);")

    def _download_localmt(self) -> None:
        self.btn_mt_download.setEnabled(False)
        self.mt_bar.show()
        self.lbl_mt_prog.show()
        self.lbl_mt_prog.setText("准备下载…")

        def work() -> None:
            from ..core import local_mt

            try:
                local_mt.ensure_pair("ja", "zh", lambda s, d, t: self.mt_progress.emit(s, d, t))
                self.mt_finished.emit("OK")
            except Exception as e:  # noqa: BLE001
                self.mt_finished.emit("ERR:" + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_mt_progress(self, status: str, done: int, total: int) -> None:
        if total > 1:
            pct = int(done * 100 / total)
            self.mt_bar.setRange(0, 100)
            self.mt_bar.setValue(min(100, pct))
            self.lbl_mt_prog.setText(f"{status} {pct}%（{done / 1048576:.0f}MB / {total / 1048576:.0f}MB）")
        else:
            self.mt_bar.setRange(0, 0)
            self.lbl_mt_prog.setText(status)

    def _on_mt_finished(self, result: str) -> None:
        self.btn_mt_download.setEnabled(True)
        self.mt_bar.hide()
        self.lbl_mt_prog.hide()
        if result.startswith("ERR:"):
            self.lbl_mt_result.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_mt_result.setText("下载失败：" + result[4:])
        else:
            self.lbl_mt_result.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_mt_result.setText("下载完成！选「本地 · 极速内置模型」即可离线翻译。")
            self._refresh_localmt_status()

    def _speed_localmt(self) -> None:
        self.btn_mt_speed.setEnabled(False)
        self.lbl_mt_result.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_mt_result.setText("测速中…")

        def work() -> None:
            from ..core import local_mt

            try:
                mt = local_mt.LocalMT("ja", "zh")
                out, dt = mt.test()
                self.speed_mt.emit(f"每句约 {dt:.2f} 秒：{out}")
            except Exception as e:  # noqa: BLE001
                self.speed_mt.emit(f"测速失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_speed_mt(self, text: str) -> None:
        self.lbl_mt_result.setStyleSheet(
            "font-size: 11px; color: #ffb3b3;" if text.startswith("测速失败") else
            "font-size: 11px; color: #9fdcae;")
        self.lbl_mt_result.setText(text)
        self.btn_mt_speed.setEnabled(True)

    # ---- 内置引擎（llama.cpp） -------------------------------------------
    def _reload_engine_variants(self) -> None:
        """刷新「用哪个引擎」下拉框（列出已下载的引擎变体）。"""
        from ..core import llama_engine as le

        labels = {
            "cpu": "CPU（不用显卡 · 最通用）",
            "vulkan": "Vulkan（A 卡 / 核显 / N 卡都能用）",
            "cuda": "CUDA（N 卡专用 · 最快）",
        }
        cur = str(self.cfg.get("translate.services.llamacpp.variant", "auto") or "auto")
        self.cmb_engine_variant.blockSignals(True)
        self.cmb_engine_variant.clear()
        self.cmb_engine_variant.addItem("自动选择（显卡不可用时回退 CPU）", "auto")
        for v in le.VARIANTS:
            mark = "已安装" if le.installed(v) else "未安装"
            self.cmb_engine_variant.addItem(f"{mark} · {labels.get(v, v)}", v)
        idx = self.cmb_engine_variant.findData(cur)
        self.cmb_engine_variant.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_engine_variant.blockSignals(False)

    def _on_engine_variant_changed(self, _i: int) -> None:
        from ..core import llama_engine as le

        v = str(self.cmb_engine_variant.currentData() or "")
        if not v:
            return
        self.cfg.set_service("llamacpp", variant=v)
        self.changed.emit("translate.services.llamacpp", v)
        if v == "auto":
            self.lbl_llama_result.setText("自动选择可用引擎；显卡启动失败时使用 CPU")
            return
        if le.installed(v):
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_llama_result.setText(f"已切换到 {v} 引擎（下一次翻译就生效）")
        else:
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: rgba(255,209,102,0.95);")
            self.lbl_llama_result.setText(f"已选 {v}，但本地还没下载：点上面的「下载引擎」按钮下载一下。")
        self._refresh_llama_status()

    def _refresh_llama_status(self) -> None:
        from ..core import llama_engine as le
        variants = le.installed_variants()
        self._reload_engine_variants()
        model = le.resolve_model(str(self.cfg.service('llamacpp').get('model') or ''))
        busy = self._llama_job is not None
        for variant, state in self.engine_status_labels.items():
            button = self.engine_download_buttons[variant]
            downloading = self._llama_job == 'engine:' + variant
            installed = le.installed(variant)
            incomplete = le.engine_dir(variant).is_dir() and any(le.engine_dir(variant).iterdir()) and not installed
            state.setText('下载中…' if downloading else '已安装' if installed else '文件不完整' if incomplete else '未安装')
            state.setStyleSheet('color: #7ddc9a;' if installed and not downloading else 'color: #ffd166;')
            button.setText('下载中…' if downloading else '已安装' if installed else '修复下载' if incomplete else '下载')
            button.setEnabled(not busy and not installed)
            button.setVisible(not installed or downloading)
            button.setToolTip('已安装，无需重复下载' if installed else '下载后才能使用此引擎')
            if variant == 'cuda' and not le.nvidia_available() and not installed:
                button.setEnabled(False)
                button.setToolTip('仅用于 NVIDIA 显卡；当前电脑请使用 CPU 或 Vulkan')
        if model:
            size = model.stat().st_size / (1024 ** 3)
            name = model.stem
            model_text = f'翻译模型：已安装 · {name}（{size:.2f} GB）'
        else:
            model_text = '翻译模型：未安装，请下载推荐模型或选择已有模型'
        self.lbl_llama_status.setText(f'本地引擎：已安装 {len(variants)} 个（任选一个即可）\n' + model_text)
        self.lbl_llama_status.setToolTip(str(model) if model else '')
        self.lbl_llama_status.setStyleSheet('font-size: 11px; color: ' + ('#9fdcae;' if model and variants else '#ffd166;'))
        recommended = le.MODELS_LLM / le.RECOMMENDED_MODELS[0]['name'] if le.RECOMMENDED_MODELS else None
        ready = recommended is not None and le.valid_model(recommended)
        selected = ready and model is not None and recommended.resolve() == model.resolve()
        if self._llama_job == 'model':
            caption = '混元 Hy-MT2 · 下载中…'
        elif ready:
            caption = '混元 Hy-MT2 · 已安装' if selected else '使用已安装的混元 Hy-MT2'
        else:
            caption = '混元 Hy-MT2 · 未安装，点击下载（约 1.13 GB）'
        self.btn_llama_model.setText(caption)
        self.btn_llama_model.setEnabled(not busy and not selected and bool(le.RECOMMENDED_MODELS))
        self.btn_pick_gguf.setEnabled(not busy)
        self.btn_import_ollama.setEnabled(not busy)

    def _download_llama_engine(self, variant: str) -> None:
        from ..core import llama_engine as le
        if self._llama_job is not None or le.installed(variant):
            return
        self._llama_job = 'engine:' + variant
        self._refresh_llama_status()
        self.llama_bar.show()
        self.llama_bar.setRange(0, 100)
        self.lbl_llama_prog.show()
        self.lbl_llama_prog.setText(f"开始下载引擎（{variant}）…")

        def work() -> None:
            from ..core import llama_engine as le

            try:
                le.download_engine(variant, lambda s, d, t: self.eng_progress.emit(s, d, t))
                self.eng_finished.emit(f"ENGINE:{variant}")
            except Exception as e:  # noqa: BLE001
                self.eng_finished.emit("ERR:" + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _pick_gguf(self) -> None:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 GGUF 模型文件", "", "GGUF 模型 (*.gguf);;所有文件 (*.*)")
        if not path:
            return
        self._import_gguf_file(Path(path))

    def _import_gguf_file(self, src: Path, name: str = "") -> None:
        if self._llama_job is not None:
            return
        self._llama_job = 'import'
        self._refresh_llama_status()
        self.llama_bar.show()
        self.llama_bar.setRange(0, 0)
        self.lbl_llama_prog.show()
        self.lbl_llama_prog.setText(f"正在导入 {breakable(Path(src).name)} …")

        def work() -> None:
            from ..core import llama_engine as le

            try:
                dst = le.import_gguf(Path(src), name or Path(src).stem[:40],
                                     lambda s, d, t: self.eng_progress.emit(s, d, t))
                self.eng_finished.emit(f"MODEL:{dst}")
            except Exception as e:  # noqa: BLE001
                self.eng_finished.emit("ERR:" + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _import_ollama_model(self) -> None:
        from ..core import llama_engine as le

        blobs = le.list_ollama_gguf()
        if not blobs:
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_llama_result.setText("没有在 Ollama 里找到可用的 GGUF 模型（可改用「选择 GGUF 模型文件」）")
            return
        src = blobs[0]
        gb = round(src.stat().st_size / (1024 ** 3), 1)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "导入 Ollama 模型",
            f"找到 {src.name[:28]}…（{gb}GB），将复制到软件文件夹。\n模型名：",
            text="Hy-MT2-7B")
        if not ok or not name.strip():
            return
        self._import_gguf_file(src, name.strip())

    def _download_llama_model(self) -> None:
        from ..core import llama_engine as le

        if not le.RECOMMENDED_MODELS or self._llama_job is not None:
            return
        item = le.RECOMMENDED_MODELS[0]
        ready = le.MODELS_LLM / item['name']
        if le.valid_model(ready):
            self._on_eng_finished('MODEL:' + str(ready))
            return
        self._llama_job = 'model'
        self._refresh_llama_status()
        self.llama_bar.show()
        self.llama_bar.setRange(0, 100)
        self.lbl_llama_prog.show()
        self.lbl_llama_prog.setText(f"开始下载 {item['name']} …")

        def work() -> None:
            try:
                dst = le.download_model(item, lambda s, d, t: self.eng_progress.emit(s, d, t))
                self.eng_finished.emit(f"MODEL:{dst}")
            except Exception as e:  # noqa: BLE001
                self.eng_finished.emit("ERR:" + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_eng_progress(self, status: str, done: int, total: int) -> None:
        if total > 1:
            pct = int(done * 100 / total)
            self.llama_bar.setRange(0, 100)
            self.llama_bar.setValue(min(100, pct))
            self.lbl_llama_prog.setText(f"{status} {pct}%（{done / 1048576:.0f}MB / {total / 1048576:.0f}MB）")
        else:
            self.llama_bar.setRange(0, 0)
            self.lbl_llama_prog.setText(status)

    def _on_eng_finished(self, result: str) -> None:
        self._llama_job = None
        self._refresh_llama_status()
        self.llama_bar.hide()
        self.lbl_llama_prog.hide()
        if result.startswith("ERR:"):
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_llama_result.setText("失败：" + result[4:])
            return
        kind, _, payload = result.partition(":")
        if kind == "MODEL":
            self.cfg.set_service("llamacpp", model=payload)
            self.changed.emit("translate.services", None)
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_llama_result.setText(
                f"已导入并选中模型：{breakable(Path(payload).name)}（点「测速」立即验证）")
        else:
            self.lbl_llama_result.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_llama_result.setText(f"引擎（{breakable(payload)}）已就绪。")
        self._refresh_llama_status()

    def _speed_llamacpp(self) -> None:
        self.lbl_llama_result.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_llama_result.setText("测速中：首次会启动引擎并加载模型（5~30 秒）…")

        def work() -> None:
            import time as _t

            from ..core import llama_engine as le
            from ..core.translate import LlamaCppProvider

            try:
                svc = self.cfg.service("llamacpp")
                model = le.resolve_model(str(svc.get("model") or ""))
                if model is None:
                    raise RuntimeError("尚未选择 GGUF 模型")
                variant = le.preferred_variant(self.cfg)
                t0 = _t.perf_counter()
                out = LlamaCppProvider(dict(svc, variant=variant)).translate(
                    'あのさ、昨日コンビニで買ったスイーツがめっちゃ美味しかったんだよね。', [], [])
                self.speed_llama.emit(f"每句约 {_t.perf_counter() - t0:.1f} 秒：{out}")
            except Exception as e:  # noqa: BLE001
                self.speed_llama.emit(f"测速失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_speed_llama(self, text: str) -> None:
        self.lbl_llama_result.setStyleSheet(
            "font-size: 11px; color: #ffb3b3;" if text.startswith("测速失败") else
            "font-size: 11px; color: #9fdcae;")
        self.lbl_llama_result.setText(text)

    # ---- 本地模型：Ollama -------------------------------------------------
    def _refresh_ollama(self, verbose: bool = False) -> None:  # noqa: ARG002
        self.lbl_ollama_status.setText("正在检测 Ollama…")
        self.lbl_ollama_status.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")

        def work() -> None:
            from ..core import ollama as om

            try:
                svc = self.cfg.service("ollama")
                base = svc.get("base_url") or om.DEFAULT_BASE
                if not om.ping(base):
                    binary = om.find_binary()
                    if binary:
                        msg = f"检测到 Ollama（{binary}），但服务没有运行：在开始菜单打开 Ollama，等图标出现后点「刷新」。"
                    else:
                        msg = "未检测到 Ollama（未安装或不在默认位置）。可点「打开 Ollama 官网下载」，装好后本软件会自动连接。"
                    self.ollama_models_ready.emit({"error": msg})
                    return
                self.ollama_models_ready.emit(om.list_models(base))
            except Exception as e:  # noqa: BLE001
                self.ollama_models_ready.emit({"error": f"检测失败：{e}"})

        threading.Thread(target=work, daemon=True).start()

    def _on_ollama_models(self, obj) -> None:
        from ..core.ollama import speed_tag

        self.cmb_ollama_model.blockSignals(True)
        self.cmb_ollama_model.clear()
        if isinstance(obj, dict) and obj.get("error"):
            self.lbl_ollama_status.setText(str(obj["error"]))
            self.lbl_ollama_status.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.cmb_ollama_model.addItem("（没有可用的本地模型）", "")
            self.btn_ollama_speed.setEnabled(False)
            self.ollama_dl_box.setVisible(False)
            self.btn_ollama_site.setVisible(True)
        else:
            models = list(obj)
            cur = str(self.cfg.service("ollama").get("model") or "")
            if models:
                self.lbl_ollama_status.setText(f"Ollama 已就绪，共 {len(models)} 个模型；选择后立即生效。")
                self.lbl_ollama_status.setStyleSheet("font-size: 11px; color: #9fdcae;")
                names = []
                for m in models:
                    names.append(m["name"])
                    self.cmb_ollama_model.addItem(
                        f"{m['name']}（{m['size_gb']}GB · {speed_tag(m['size_gb'])}）", m["name"])
                idx = names.index(cur) if cur in names else 0
                self.cmb_ollama_model.setCurrentIndex(idx)
                if cur not in names:
                    self._on_ollama_model_pick(idx)
            else:
                self.lbl_ollama_status.setText("Ollama 已就绪，但还没有安装任何模型：点下面的按钮下载一个吧。")
                self.lbl_ollama_status.setStyleSheet("font-size: 11px; color: rgba(255,209,102,0.95);")
                self.cmb_ollama_model.addItem("（还没有模型）", "")
            self.btn_ollama_speed.setEnabled(bool(models))
            self.btn_ollama_site.setVisible(False)
            self.ollama_dl_box.setVisible(True)
        self.cmb_ollama_model.blockSignals(False)

    def _on_ollama_model_pick(self, _i: int) -> None:
        name = self.cmb_ollama_model.currentData()
        if not name:
            return
        base = str(self.cfg.service("ollama").get("base_url") or "")
        self.cfg.set_service("ollama", model=name, base_url=base)
        self.changed.emit("translate.services", None)
        self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_ollama_result.setText(f"已选择：{name}")

    def _pull_recommended(self, name: str) -> None:
        self.ollama_dl_box.setEnabled(False)
        self.ollama_bar.show()
        self.lbl_ollama_prog.show()
        self.lbl_ollama_prog.setText(f"开始下载 {name} …")

        def work() -> None:
            from ..core import ollama as om

            try:
                svc = self.cfg.service("ollama")
                base = svc.get("base_url") or om.DEFAULT_BASE
                om.pull_model(name, base, lambda s, d, t: self.pull_progress.emit(s, d, t))
                self.pull_finished.emit(name)
            except Exception as e:  # noqa: BLE001
                self.pull_finished.emit("ERR:" + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _pull_any(self) -> None:
        name = self.ed_ollama_any.text().strip()
        if not name:
            self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_ollama_result.setText("请先输入模型名")
            return
        self._pull_recommended(name)

    def _import_gguf(self) -> None:
        """把本地的 .gguf 模型文件导入 Ollama（等价于 ollama create）。"""
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 GGUF 模型文件", "", "GGUF 模型 (*.gguf);;所有文件 (*.*)")
        if not path:
            return
        default = Path(path).stem
        name, ok = QtWidgets.QInputDialog.getText(
            self, "模型名称", "导入后在模型列表里显示的名字（建议用英文/数字）：", text=default)
        if not ok or not name.strip():
            return
        model_name = re.sub(r"[^0-9A-Za-z_.\-]+", "-", name.strip()).strip("-")[:60] or "my-model"
        self.ollama_dl_box.setEnabled(False)
        self.ollama_bar.show()
        self.ollama_bar.setRange(0, 0)
        self.lbl_ollama_prog.show()
        self.lbl_ollama_prog.setText(
            f"正在导入 {breakable(Path(path).name)} …（大文件可能需要几分钟）")

        def work() -> None:
            from ..core import ollama as om

            mf = Path(tempfile.gettempdir()) / f"jpsub-modelfile-{model_name}.txt"
            try:
                binary = om.find_binary()
                if not binary:
                    self.pull_finished.emit("ERR:未找到 ollama.exe（请先安装 Ollama）")
                    return
                mf.write_text(f'FROM "{path}"\n', encoding="utf-8")
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.Popen(
                    [binary, "create", model_name, "-f", str(mf)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=flags)
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self.pull_progress.emit(line[:80], 0, 0)
                code = proc.wait()
                if code != 0:
                    raise RuntimeError(f"ollama create 退出码 {code}")
                self.pull_finished.emit(model_name)
            except Exception as e:  # noqa: BLE001
                self.pull_finished.emit("ERR:" + str(e))
            finally:
                mf.unlink(missing_ok=True)

        threading.Thread(target=work, daemon=True).start()

    def _on_pull_progress(self, status: str, done: int, total: int) -> None:
        if total > 0:
            pct = int(done * 100 / total)
            self.ollama_bar.setRange(0, 100)
            self.ollama_bar.setValue(pct)
            self.lbl_ollama_prog.setText(
                f"{status}  {pct}%（{done / 1048576:.0f}MB / {total / 1048576:.0f}MB）")
        else:
            self.ollama_bar.setRange(0, 0)
            self.lbl_ollama_prog.setText(status)

    def _on_pull_finished(self, result: str) -> None:
        self.ollama_dl_box.setEnabled(True)
        self.ollama_bar.hide()
        self.lbl_ollama_prog.hide()
        if result.startswith("ERR:"):
            self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_ollama_result.setText("下载失败：" + result[4:])
        else:
            self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_ollama_result.setText(f"{result} 下载完成，已自动选中。")
            self.cfg.set_service("ollama", model=result)
            self.changed.emit("translate.services", None)
            self._refresh_ollama()

    def _speed_ollama(self) -> None:
        name = self.cmb_ollama_model.currentData()
        if not name:
            return
        self.btn_ollama_speed.setEnabled(False)
        self.lbl_ollama_result.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_ollama_result.setText("测速中（首次会加载模型，可能要一会儿）…")

        def work() -> None:
            from ..core import ollama as om

            try:
                svc = self.cfg.service("ollama")
                base = svc.get("base_url") or om.DEFAULT_BASE
                r = om.chat_once(name, "あのさ、昨日コンビニで買ったスイーツがめっちゃ美味しかったんだよね。", base)
                self.speed_ollama.emit(
                    f"每句约 {r['seconds']:.1f} 秒（{r['eval_count']} 字，约 {r['tps']} 字/秒）：{r['text']}")
            except Exception as e:  # noqa: BLE001
                self.speed_ollama.emit(f"测速失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_speed_ollama(self, text: str) -> None:
        self.lbl_ollama_result.setStyleSheet(
            "font-size: 11px; color: #ffb3b3;" if text.startswith("测速失败") else
            "font-size: 11px; color: #9fdcae;")
        self.lbl_ollama_result.setText(text)
        self.btn_ollama_speed.setEnabled(True)

    # ---- 通用：拉取 OpenAI 兼容模型列表 -----------------------------------
    def _fetch_model_list(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.lbl_test.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_test.setText("请先填写 Base URL，再拉取模型列表")
            return
        self.btn_pull_list.setEnabled(False)
        self.lbl_test.setStyleSheet("font-size: 11px; color: rgba(180,188,205,0.95);")
        self.lbl_test.setText("正在拉取模型列表…")

        def work() -> None:
            from ..core.http_util import make_client

            try:
                base = url.rstrip("/")
                headers = {}
                key = self.ed_key.text().strip()
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                with make_client(base, timeout=10) as c:
                    r = c.get(f"{base}/models", headers=headers)
                r.raise_for_status()
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                self.models_pulled.emit(sorted(ids))
            except Exception as e:  # noqa: BLE001
                self.models_pulled.emit({"error": str(e)[:100]})

        threading.Thread(target=work, daemon=True).start()

    def _on_models_pulled(self, obj) -> None:
        self.btn_pull_list.setEnabled(True)
        self.cmb_pulled.blockSignals(True)
        self.cmb_pulled.clear()
        if isinstance(obj, dict) and obj.get("error"):
            self.lbl_test.setStyleSheet("font-size: 11px; color: #ffb3b3;")
            self.lbl_test.setText(f"拉取失败：{obj['error']}")
            self.cmb_pulled.addItem("（拉取失败）", "")
        else:
            self.lbl_test.setStyleSheet("font-size: 11px; color: #9fdcae;")
            self.lbl_test.setText(f"拉取到 {len(obj)} 个模型，选择即填入。")
            val = self.ed_model.text().strip()
            self.cmb_pulled.addItem("（选择模型）", "")
            for i, mid in enumerate(obj):
                self.cmb_pulled.addItem(mid, mid)
                if mid == val:
                    self.cmb_pulled.setCurrentIndex(i + 1)
        self.cmb_pulled.blockSignals(False)

    def _on_pulled_model(self, _i: int) -> None:
        mid = self.cmb_pulled.currentData()
        if mid:
            self.ed_model.setText(str(mid))
            self._save_provider_fields()


def source_matches(fname: str, cur: str) -> bool:
    """背景选择判断：支持完整文件名或 auto。"""
    if not cur:
        return False
    if cur == "auto":
        files = background_files()
        return bool(files) and files[0].name == fname
    return fname == cur
