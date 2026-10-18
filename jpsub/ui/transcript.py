# -*- coding: utf-8 -*-
"""字幕显示区：流式逐字浮现、整句校对、历史行。"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.segment import Segment


class TypingLabel(QtWidgets.QLabel):
    """逐字浮现的标签：目标文本增长时逐字显示，营造"边说边出字"效果。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._target = ""
        self._shown = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self.setText("")

    def set_target(self, text: str) -> None:
        text = text or ""
        if text == self._target:
            return
        shown = self._target[:self._shown]
        if not text.startswith(shown) or len(text) < self._shown:
            # 保留公共前缀，避免整行闪回
            cp = 0
            m = min(len(shown), len(text))
            while cp < m and shown[cp] == text[cp]:
                cp += 1
            self._shown = cp
        self._target = text
        if self._shown >= len(text):
            self.setText(text)
            self._timer.stop()
            return
        if not self._timer.isActive():
            self._timer.start()

    def snap(self, text: str) -> None:
        self._target = text or ""
        self._shown = len(self._target)
        super().setText(self._target)
        self._timer.stop()

    def _tick(self) -> None:
        if self._shown >= len(self._target):
            self._timer.stop()
            return
        step = max(1, (len(self._target) - self._shown) // 10)
        self._shown = min(len(self._target), self._shown + step)
        super().setText(self._target[:self._shown])


class LineWidget(QtWidgets.QWidget):
    """一条字幕：日语原文 + 中文译文。"""

    def __init__(self, seg: Segment, jp_size: int, zh_size: int, parent=None):
        super().__init__(parent)
        self.seg = seg
        self._jp_size = jp_size
        self._zh_size = zh_size
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 11, 6, 11)
        lay.setSpacing(6)
        self.jp = QtWidgets.QLabel(seg.jp or "")
        self.zh = TypingLabel()
        self.zh.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.jp.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.jp.setWordWrap(True)
        self.zh.setWordWrap(True)
        lay.addWidget(self.jp)
        lay.addWidget(self.zh)
        self._apply_sizes(jp_size, zh_size)
        self._apply_style()

    def _apply_style(self) -> None:
        self.jp.setObjectName("captionSource")
        self.jp.setStyleSheet("")
        self.zh.setObjectName("captionTranslation")
        self.zh.setStyleSheet("")

    def _apply_sizes(self, jp: int, zh: int) -> None:
        self._jp_size, self._zh_size = jp, zh
        f_jp = self.font()
        f_jp.setPointSize(max(9, jp))
        self.jp.setFont(f_jp)
        f_zh = self.font()
        f_zh.setPointSize(max(10, zh))
        self.zh.setFont(f_zh)

    def set_sizes(self, jp: int, zh: int) -> None:
        self._apply_sizes(jp, zh)

    def set_zh(self, text: str, typing: bool = True) -> None:
        if not text:
            return
        self.seg.zh = text
        if typing:
            self.zh.set_target(text)
        else:
            self.zh.snap(text)

    def set_jp(self, text: str) -> None:
        self.seg.jp = text
        self.jp.setText(text)

    def apply_display(self, mode: str) -> None:
        self.jp.setVisible(mode in ("both", "jp"))
        self.zh.setVisible(mode in ("both", "zh"))

    def contextMenuEvent(self, e) -> None:
        menu = QtWidgets.QMenu(self)
        act_copy_jp = menu.addAction("复制日语原文")
        act_copy_zh = menu.addAction("复制中文译文")
        act_copy_both = menu.addAction("复制双语")
        act = menu.exec(e.globalPos())
        if act == act_copy_jp:
            QtWidgets.QApplication.clipboard().setText(self.seg.jp)
        elif act == act_copy_zh:
            QtWidgets.QApplication.clipboard().setText(self.seg.zh)
        elif act == act_copy_both:
            QtWidgets.QApplication.clipboard().setText(f"{self.seg.jp}\n{self.seg.zh}")


class TranscriptView(QtWidgets.QScrollArea):
    """滚动字幕区：底部实时行 + 上方历史行。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QtWidgets.QWidget()
        container.setObjectName("transcriptContainer")
        container.setStyleSheet("QWidget#transcriptContainer { background: transparent; }")
        self.vbox = QtWidgets.QVBoxLayout(container)
        self.vbox.setContentsMargins(6, 8, 10, 8)
        self.vbox.setSpacing(2)
        self.vbox.addStretch(1)
        self.setWidget(container)
        self.lines: list[LineWidget] = []
        self._map: dict[str, LineWidget] = {}
        # 显示行数只限制窗口里的控件，不限制本次会话可回看、可导出的记录。
        self._history: list[Segment] = []
        self._history_by_sid: dict[str, Segment] = {}
        self._live: LineWidget | None = None
        jp = int(cfg.get("ui.jp_size", 13))
        zh = int(cfg.get("ui.zh_size", 19))
        self._sizes = (jp, zh)
        mode = str(cfg.get("ui.display", "both"))
        self._display = mode
        self.placeholder = QtWidgets.QLabel(
            "点击「开始聆听」后，这里会实时显示日语原文与中文翻译。\n"
            "先播放在看的直播，让声音从扬声器/耳机正常出声即可。\n"
            "默认使用随附的本地模型。需要调整字幕或使用云端翻译时，可打开「设置」。")
        self.placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setObjectName("muted")
        self.placeholder.setStyleSheet("font-size: 14px; line-height: 1.8;")
        self.vbox.insertWidget(0, self.placeholder)

    # ------------------------------------------------------------- 实时行
    def _ensure_live(self) -> LineWidget:
        if self._live is None:
            seg = Segment(sid="live", jp="")
            self._live = LineWidget(seg, *self._sizes)
            self._live.apply_display(self._display)
            self.live_placeholder_hide()
            self.vbox.addWidget(self._live)
        return self._live

    def live_placeholder_hide(self) -> None:
        self.placeholder.setVisible(False)

    def set_live_jp(self, text: str) -> None:
        if not text:
            return
        line = self._ensure_live()
        line.jp.setText(text)
        line.seg.jp = text
        self._scroll_if_bottom()

    def set_live_zh(self, text: str) -> None:
        if not text:
            return
        line = self._ensure_live()
        line.zh.snap(text)
        line.seg.zh = text
        self._scroll_if_bottom()

    # ------------------------------------------------------------- 整句
    def commit_segment(self, seg: Segment) -> LineWidget:
        """整句校对结果到达：把实时行定格为历史行。"""
        if self._live is not None:
            line = self._live
            line.seg.sid = seg.sid
            line.seg.t0, line.seg.t1 = seg.t0, seg.t1
            line.seg.rtf, line.seg.asr_seconds = seg.rtf, seg.asr_seconds
            line.seg.live_jp = seg.live_jp
            line.set_jp(seg.jp)
            line.zh.snap(line.seg.zh or "")
            self._live = None
        else:
            line = LineWidget(seg, *self._sizes)
            line.apply_display(self._display)
            self.vbox.addWidget(line)
        line.set_sizes(*self._sizes)
        line.apply_display(self._display)
        if seg.zh:
            line.set_zh(seg.zh, typing=False)
        self.lines.append(line)
        self._map[seg.sid] = line
        self._history.append(line.seg)
        self._history_by_sid[seg.sid] = line.seg
        self._trim()
        self._scroll_if_bottom()
        return line

    def set_zh_for(self, sid: str, text: str) -> None:
        saved = self._history_by_sid.get(sid)
        if saved is not None and text:
            saved.zh = text
        line = self._map.get(sid)
        if line is not None:
            line.set_zh(text, typing=False)
            self._scroll_if_bottom()

    # ------------------------------------------------------------- 外观
    def set_sizes(self, jp: int, zh: int) -> None:
        self._sizes = (jp, zh)
        for line in self.lines + ([self._live] if self._live else []):
            line.set_sizes(jp, zh)

    def set_display_mode(self, mode: str) -> None:
        self._display = mode
        for line in self.lines + ([self._live] if self._live else []):
            line.apply_display(mode)

    def set_max_lines(self) -> None:
        """设置显示行数时，把已隐藏的会话记录重新铺到窗口中。0 表示无限。"""
        keep = int(self.cfg.get("ui.max_lines", 8))
        wanted = self._history if keep <= 0 else self._history[-keep:]
        for line in self.lines:
            self.vbox.removeWidget(line)
            line.deleteLater()
        self.lines.clear()
        self._map.clear()
        insert_at = self.vbox.indexOf(self._live) if self._live else self.vbox.count()
        for seg in wanted:
            line = LineWidget(seg, *self._sizes)
            line.set_zh(seg.zh, typing=False)
            line.apply_display(self._display)
            self.vbox.insertWidget(insert_at, line)
            insert_at += 1
            self.lines.append(line)
            self._map[seg.sid] = line

    def clear_all(self) -> None:
        for line in self.lines:
            self.vbox.removeWidget(line)
            line.deleteLater()
        self.lines.clear()
        self._map.clear()
        self._history.clear()
        self._history_by_sid.clear()
        if self._live is not None:
            self.vbox.removeWidget(self._live)
            self._live.deleteLater()
            self._live = None
        self.placeholder.setVisible(True)

    def _trim(self) -> None:
        keep = int(self.cfg.get("ui.max_lines", 8))
        if keep <= 0:
            return
        while len(self.lines) > keep:
            old = self.lines.pop(0)
            self._map.pop(old.seg.sid, None)
            self.vbox.removeWidget(old)
            old.deleteLater()

    def _scroll_if_bottom(self, force: bool = False) -> None:
        bar = self.verticalScrollBar()
        near = bar.maximum() - bar.value() < 60
        if near or force:
            QtCore.QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def session_segments(self) -> list[Segment]:
        """UI-thread read-only reference for incremental overlay updates."""
        return self._history

    def all_segments(self) -> list[Segment]:
        return list(self._history)

    def recent_segments(self, count: int) -> list[Segment]:
        return self._history[-max(1, count):]
