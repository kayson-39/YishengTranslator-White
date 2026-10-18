# -*- coding: utf-8 -*-
"""Scrollable, incremental subtitle overlay with independent interaction state."""
from __future__ import annotations
from PySide6 import QtCore, QtGui, QtWidgets


class _SubtitleBrowser(QtWidgets.QTextBrowser):
    """Paint translation outlines behind filled glyphs, never in place of the fill."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    def paintEvent(self, event):
        owner = self.owner
        if owner._bg_alpha < 120 and owner.cfg.get("overlay.text_shadow", True):
            layout = self.document().documentLayout()
            context = QtGui.QAbstractTextDocumentLayout.PaintContext()
            context.clip = QtCore.QRectF(event.rect()).translated(0, self.verticalScrollBar().value())
            # Each visible fragment gets one non-overlapping selection. Only
            # translated text contributes to the outline pass; Japanese is
            # transparent here and is filled exactly once by the normal pass.
            selections = []
            has_outline = False
            block = self.cursorForPosition(QtCore.QPoint(0, 0)).block()
            bottom = self.verticalScrollBar().value() + self.viewport().height()
            while block.isValid() and layout.blockBoundingRect(block).top() <= bottom:
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid():
                        selected = QtGui.QAbstractTextDocumentLayout.Selection()
                        selected.cursor = QtGui.QTextCursor(self.document())
                        selected.cursor.setPosition(fragment.position())
                        selected.cursor.setPosition(fragment.position()+fragment.length(), QtGui.QTextCursor.MoveMode.KeepAnchor)
                        if fragment.charFormat().fontWeight() >= QtGui.QFont.Weight.DemiBold:
                            color = QtGui.QColor("#ffffff" if owner.cfg.get("overlay.theme", "dark") == "light" else "#000000")
                            selected.format.setForeground(color)
                            selected.format.setTextOutline(QtGui.QPen(color, 1.7))
                            has_outline = True
                        else:
                            selected.format.setForeground(QtGui.QColor(0, 0, 0, 0))
                            selected.format.setTextOutline(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                        selections.append(selected)
                    iterator += 1
                block = block.next()
            if has_outline:
                context.selections = selections
                painter = QtGui.QPainter(self.viewport())
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                painter.translate(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
                layout.draw(painter, context)
                painter.end()
        # The normal, solid text is always painted last, over the optional outline pass.
        super().paintEvent(event)


class OverlayWindow(QtWidgets.QWidget):
    interaction_changed = QtCore.Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.FramelessWindowHint
                         | QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.WindowStaysOnTopHint
                         | QtCore.Qt.WindowType.NoDropShadowWindowHint)
        self.cfg = cfg
        self.setWindowTitle("译声 · 悬浮字幕")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(440, 180)
        self.setMouseTracking(True)
        self._source = []
        self._seen = 0
        self._first_sid = None
        self._blocks = {}
        self._order = []
        self._values = {}
        self._last_items = []
        self._last_live = None
        self._following = True
        self._unread = 0
        self._mutating = False
        self._drag_pos = None
        self._resize_start = None
        self._bg_alpha = 224
        self._appearance = None
        self._style_theme = None
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 10)
        outer.setSpacing(5)
        self.tools = QtWidgets.QWidget()
        policy = self.tools.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.tools.setSizePolicy(policy)
        self.tools.setFixedHeight(32)
        toolbar = QtWidgets.QHBoxLayout(self.tools)
        toolbar.setContentsMargins(2, 0, 0, 0)
        toolbar.setSpacing(5)
        self.drag_handle = QtWidgets.QLabel("实时字幕 · 拖动此处移动")
        self.drag_handle.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        self.drag_handle.installEventFilter(self)
        toolbar.addWidget(self.drag_handle, 1)
        self.btn_smaller = QtWidgets.QPushButton("A−")
        self.btn_larger = QtWidgets.QPushButton("A＋")
        self.btn_lock = QtWidgets.QPushButton("锁定")
        self.btn_thru = QtWidgets.QPushButton("穿透")
        self.btn_close = QtWidgets.QPushButton("隐藏")
        self.btn_lock.setCheckable(True)
        self.btn_thru.setCheckable(True)
        for b in (self.btn_smaller, self.btn_larger, self.btn_lock, self.btn_thru, self.btn_close):
            b.setFixedHeight(28)
            toolbar.addWidget(b)
        self.btn_smaller.setToolTip("缩小字幕字号")
        self.btn_larger.setToolTip("放大字幕字号")
        self.btn_thru.setToolTip("开启后不能点击浮窗；在主窗口设置中恢复操作")
        self.btn_lock.setToolTip("只锁定移动和尺寸，仍可回看字幕")
        self.btn_smaller.clicked.connect(lambda: self._change_size(-1))
        self.btn_larger.clicked.connect(lambda: self._change_size(1))
        self.btn_lock.clicked.connect(self._toggle_lock)
        self.btn_thru.clicked.connect(self._toggle_through)
        self.btn_close.clicked.connect(self._hide_overlay)
        outer.addWidget(self.tools)
        self.browser = _SubtitleBrowser(self)
        self.browser.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.browser.setPlaceholderText("等待语音… 播放视频并在主窗口开始聆听。")
        self.browser.setOpenLinks(False)
        self.browser.setUndoRedoEnabled(False)
        self.browser.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.browser.document().setDocumentMargin(5)
        self.browser.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.browser.viewport().setAutoFillBackground(False)
        self.browser.viewport().installEventFilter(self)
        self.browser.verticalScrollBar().actionTriggered.connect(lambda _: QtCore.QTimer.singleShot(0, self._user_scroll))
        self.browser.verticalScrollBar().sliderMoved.connect(lambda _: self._user_scroll())
        self.browser.installEventFilter(self)
        outer.addWidget(self.browser, 1)
        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        self.lbl_mode = QtWidgets.QLabel("实时跟随 · 滚轮向上回看")
        footer.addWidget(self.lbl_mode, 1)
        self.btn_latest = QtWidgets.QPushButton("↓ 回到最新")
        self.btn_latest.clicked.connect(self.jump_latest)
        footer.addWidget(self.btn_latest)
        self.grip = QtWidgets.QSizeGrip(self)
        self.grip.setFixedSize(16, 16)
        footer.addWidget(self.grip)
        outer.addLayout(footer)
        self.btn_latest.hide()
        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(850)
        self._hide_timer.timeout.connect(self._hide_tools)
        self.resize(int(cfg.get("overlay.w", 760)), int(cfg.get("overlay.h", 360)))
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        x, y = cfg.get("overlay.x"), cfg.get("overlay.y")
        point = QtCore.QPoint(int(x), int(y)) if x is not None and y is not None else QtCore.QPoint(
            screen.center().x() - self.width() // 2, screen.bottom() - self.height() - 60)
        if not any(s.availableGeometry().intersects(QtCore.QRect(point, self.size())) for s in QtWidgets.QApplication.screens()):
            point = screen.topLeft() + QtCore.QPoint(30, 30)
        self.move(point)
        self._geo_timer = QtCore.QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geometry)
        self.refresh()

    def _capture_anchor(self):
        cursor = self.browser.cursorForPosition(QtCore.QPoint(6, 6))
        block = cursor.block()
        return block, self.browser.cursorRect(QtGui.QTextCursor(block)).top()

    def _restore_anchor(self, anchor):
        if self._following:
            self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum())
        else:
            block, y = anchor
            if block.isValid():
                new_y = self.browser.cursorRect(QtGui.QTextCursor(block)).top()
                bar = self.browser.verticalScrollBar()
                bar.setValue(bar.value() + new_y - y)

    def _write_block(self, sid, jp, zh, live=False):
        if self._values.get(sid) == (jp, zh, live):
            return
        doc = self.browser.document()
        block = self._blocks.get(sid)
        if block is not None and block.isValid():
            cursor = QtGui.QTextCursor(block)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock, QtGui.QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        else:
            cursor = QtGui.QTextCursor(doc)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            if self._order:
                cursor.insertBlock()
            self._blocks[sid] = cursor.block()
            self._order.append(sid)
        fmt = QtGui.QTextBlockFormat()
        fmt.setTopMargin(5)
        fmt.setBottomMargin(12)
        cursor.setBlockFormat(fmt)
        light = self.cfg.get("overlay.theme", "dark") == "light"
        def text_format(size, color, bold=False):
            f = QtGui.QTextCharFormat()
            f.setFontFamilies([str(self.cfg.get("ui.font", "Microsoft YaHei UI"))])
            f.setFontPointSize(size)
            f.setFontWeight(QtGui.QFont.Weight.DemiBold if bold else QtGui.QFont.Weight.Normal)
            f.setForeground(QtGui.QColor(color))
            # Small Japanese glyphs must keep their native filled strokes.
            f.setTextOutline(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
            return f
        show_jp = bool(self.cfg.get("overlay.show_jp", True))
        if jp and show_jp:
            cursor.insertText(jp.replace("\r\n", "\u2028").replace("\n", "\u2028"), text_format(int(self.cfg.get("overlay.jp_size", 15)), "#46536a" if light else ("#f1f5fc" if self._bg_alpha < 120 else "#cad2e1")))
            if zh:
                cursor.insertText("\u2028")
        if zh:
            cursor.insertText(zh.replace("\r\n", "\u2028").replace("\n", "\u2028"), text_format(int(self.cfg.get("overlay.zh_size", 24)), "#202637" if light else "#ffffff", True))
        if not jp and not zh:
            cursor.insertText("…", text_format(16, "#909bad"))
        self._values[sid] = (jp, zh, live)

    def _remove(self, sid):
        block = self._blocks.pop(sid, None)
        if block is None or not block.isValid():
            return
        cursor = QtGui.QTextCursor(block)
        if block.next().isValid():
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.NextBlock, QtGui.QTextCursor.MoveMode.KeepAnchor)
        elif block.previous().isValid():
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.StartOfBlock, QtGui.QTextCursor.MoveMode.KeepAnchor)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.PreviousCharacter, QtGui.QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock, QtGui.QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._values.pop(sid, None)
        if sid in self._order:
            self._order.remove(sid)

    def set_segments(self, segments, live_jp="", live_zh=""):
        first = segments[0].sid if segments else None
        # Committed edits arrive through update_segment; avoid re-scrolling and
        # repainting the entire transparent window for an unchanged snapshot.
        if (segments is self._source and len(segments) == self._seen
                and first == self._first_sid and self._last_live == (live_jp, live_zh)):
            return
        if len(segments) < self._seen or (self._first_sid is not None and first != self._first_sid):
            self._reset()
        self._source = segments
        anchor = self._capture_anchor()
        self._mutating = True
        added = len(segments) - self._seen
        if added:
            self._remove("__live__")
            keep = int(self.cfg.get("overlay.lines", 0))
            start = max(self._seen, len(segments) - keep) if keep > 0 else self._seen
            for seg in segments[start:]:
                self._write_block(seg.sid, seg.jp, seg.zh)
            self._seen = len(segments)
            self._first_sid = first
            if not self._following:
                self._unread += added
        if live_jp or live_zh:
            self._write_block("__live__", live_jp, live_zh, True)
        else:
            self._remove("__live__")
        self._last_live = (live_jp, live_zh)
        keep = int(self.cfg.get("overlay.lines", 0))
        if keep > 0:
            committed = [sid for sid in self._order if sid != "__live__"]
            for sid in committed[:-keep]:
                self._remove(sid)
        self._restore_anchor(anchor)
        self._mutating = False
        self._update_status()
        self.update()

    def update_segment(self, sid, jp, zh):
        if sid not in self._blocks or self._values.get(sid) == (jp, zh, False):
            return
        anchor = self._capture_anchor()
        self._mutating = True
        self._write_block(sid, jp, zh)
        self._restore_anchor(anchor)
        self._mutating = False
        self.update()

    def _reset(self):
        self.browser.clear()
        self._blocks.clear()
        self._order.clear()
        self._values.clear()
        self._seen = 0
        self._first_sid = None
        self._unread = 0
        self._following = True
        self._last_live = None

    def set_content(self, items, live_index=None, force=False):
        # Compatibility for screenshot fixtures; real sessions use stable segment IDs.
        from ..core.segment import Segment
        self._last_items = list(items)
        committed = [Segment(sid=f"preview-{i}", jp=jp, zh=zh) for i,(jp,zh) in enumerate(items) if i != live_index]
        live = items[live_index] if live_index is not None else ("", "")
        self.set_segments(committed, *live)

    def _user_scroll(self):
        if self._mutating:
            return
        bar = self.browser.verticalScrollBar()
        self._following = bar.maximum() - bar.value() < 8
        if self._following:
            self._unread = 0
        self._update_status()

    def _update_status(self):
        self.btn_latest.setVisible(not self._following)
        self.btn_latest.setText(f"↓ 新增 {self._unread} 条 · 回到最新" if self._unread else "↓ 回到最新")
        self.lbl_mode.setText("实时跟随 · 滚轮向上回看" if self._following else "正在查看历史 · 翻译继续")

    def jump_latest(self):
        self._following = True
        self._unread = 0
        self.browser.verticalScrollBar().setValue(self.browser.verticalScrollBar().maximum())
        self._update_status()

    def _change_size(self, delta):
        size = max(14, min(60, int(self.cfg.get("overlay.zh_size", 24)) + delta))
        self.cfg.update_many({"overlay.zh_size": size, "overlay.jp_size": max(10, size - 9)})
        self.refresh()
        self.interaction_changed.emit()

    def refresh(self):
        self._bg_alpha = int(self.cfg.get("overlay.opacity", 88) * 255 / 100)
        light = self.cfg.get("overlay.theme", "dark") == "light"
        text, muted, button = ("#202637", "#667085", "rgba(30,40,60,0.06)") if light else ("#f8fafc", "#cad2e1", "rgba(255,255,255,0.10)")
        if light != self._style_theme:
            self.setStyleSheet(f"QWidget {{ background: transparent; color: {text}; }} QLabel {{ border: none; }} QPushButton {{ background: {button}; color: {text}; border: none; border-radius: 6px; padding: 4px 9px; font-size: 11px; }} QPushButton:hover {{ background: rgba(217,84,130,0.25); }} QPushButton:checked {{ background: #d95482; color: white; }} QTextBrowser {{ background: transparent; border: none; padding: 0px; }} QScrollBar:vertical {{ background: transparent; width: 8px; }} QScrollBar::handle:vertical {{ background: {muted}; border-radius: 4px; min-height: 24px; }} QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }} QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}")
            self.lbl_mode.setStyleSheet(f"font-size: 11px; color: {muted};")
            self._style_theme = light
        appearance = (self.cfg.get("overlay.theme"), self.cfg.get("overlay.zh_size"), self.cfg.get("overlay.jp_size"), self.cfg.get("overlay.show_jp"), self.cfg.get("overlay.text_shadow"), self.cfg.get("ui.font"), self._bg_alpha < 120, self.cfg.get("overlay.lines"))
        if appearance != self._appearance:
            anchor = self._capture_anchor()
            self._mutating = True
            for sid in list(self._order):
                jp, zh, live = self._values[sid]
                self._values.pop(sid)
                self._write_block(sid, jp, zh, live)
            self._restore_anchor(anchor)
            self._mutating = False
            if self._appearance is not None and appearance[-1] != self._appearance[-1]:
                source, last_live = self._source, self._last_live
                self._reset()
                self.set_segments(source, *(last_live or ("", "")))
            self._appearance = appearance
        self._apply_flags()
        self.update()

    def _apply_flags(self):
        locked = bool(self.cfg.get("overlay.locked", False))
        through = bool(self.cfg.get("overlay.click_through", False))
        self._drag_pos = None
        self.grip.setVisible(not locked and not through)
        self.drag_handle.setCursor(QtCore.Qt.CursorShape.ArrowCursor if locked else QtCore.Qt.CursorShape.SizeAllCursor)
        self.btn_lock.setChecked(locked)
        self.btn_lock.setText("解锁" if locked else "锁定")
        self.btn_thru.setChecked(through)
        if bool(self.windowFlags() & QtCore.Qt.WindowType.WindowTransparentForInput) != through:
            shown = self.isVisible()
            self.setWindowFlag(QtCore.Qt.WindowType.WindowTransparentForInput, through)
            if shown:
                self.show()
        if through:
            self.tools.hide()
        else:
            self.tools.show()

    def _toggle_lock(self):
        self.cfg.set("overlay.locked", not bool(self.cfg.get("overlay.locked", False)))
        self._apply_flags()
        self.interaction_changed.emit()

    def _toggle_through(self):
        self.cfg.set("overlay.click_through", not bool(self.cfg.get("overlay.click_through", False)))
        self._apply_flags()
        self.interaction_changed.emit()

    def _hide_overlay(self):
        self.cfg.set("overlay.enabled", False)
        self.hide()
        self.interaction_changed.emit()

    def _hide_tools(self):
        if not self.rect().contains(self.mapFromGlobal(QtGui.QCursor.pos())):
            self.tools.hide()

    def enterEvent(self, event):
        self._hide_timer.stop()
        if not self.cfg.get("overlay.click_through", False):
            self.tools.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hide_timer.start()
        super().leaveEvent(event)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QtCore.QEvent.Type.Wheel and hasattr(self, "browser") and obj is self.browser.viewport():
            self._mutating = True
            bar = self.browser.verticalScrollBar()
            delta = event.pixelDelta().y() or event.angleDelta().y()
            bar.setValue(bar.value() - delta)
            self._mutating = False
            self._user_scroll()
            event.accept()
            return True
        if obj is self.drag_handle:
            if kind == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton and not self.cfg.get("overlay.locked", False):
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                handle = self.windowHandle()
                if handle and handle.startSystemMove():
                    self._drag_pos = None
                return True
            if kind == QtCore.QEvent.Type.MouseMove and self._drag_pos is not None:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if kind == QtCore.QEvent.Type.MouseButtonRelease:
                self._drag_pos = None
                self._save_geometry()
        return super().eventFilter(obj, event)

    def _edges_at(self, pos):
        edges = QtCore.Qt.Edge(0)
        if pos.x() < 8:
            edges |= QtCore.Qt.Edge.LeftEdge
        if pos.x() > self.width() - 9:
            edges |= QtCore.Qt.Edge.RightEdge
        if pos.y() < 8:
            edges |= QtCore.Qt.Edge.TopEdge
        if pos.y() > self.height() - 9:
            edges |= QtCore.Qt.Edge.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and not self.cfg.get("overlay.locked", False):
            edges = self._edges_at(event.position().toPoint())
            if edges:
                handle = self.windowHandle()
                if handle and handle.startSystemResize(edges):
                    return
                self._resize_start = (event.globalPosition().toPoint(), self.geometry(), edges)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_start:
            point, rect, edges = self._resize_start
            delta = event.globalPosition().toPoint() - point
            new = QtCore.QRect(rect)
            if edges & QtCore.Qt.Edge.LeftEdge:
                new.setLeft(min(rect.left()+delta.x(), rect.right()-self.minimumWidth()+1))
            if edges & QtCore.Qt.Edge.RightEdge:
                new.setRight(max(rect.right()+delta.x(), rect.left()+self.minimumWidth()-1))
            if edges & QtCore.Qt.Edge.TopEdge:
                new.setTop(min(rect.top()+delta.y(), rect.bottom()-self.minimumHeight()+1))
            if edges & QtCore.Qt.Edge.BottomEdge:
                new.setBottom(max(rect.bottom()+delta.y(), rect.top()+self.minimumHeight()-1))
            self.setGeometry(new)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resize_start:
            self._resize_start = None
            self._save_geometry()
        super().mouseReleaseEvent(event)

    def _save_geometry(self):
        self.cfg.update_many({"overlay.x": self.x(), "overlay.y": self.y(), "overlay.w": self.width(), "overlay.h": self.height()})

    def resizeEvent(self, event):
        if hasattr(self, "_geo_timer"):
            self._geo_timer.start(400)
        super().resizeEvent(event)

    def moveEvent(self, event):
        if hasattr(self, "_geo_timer"):
            self._geo_timer.start(400)
        super().moveEvent(event)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        # Clear the complete alpha surface before painting the fixed panel. No inherited frame/effect.
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.GlobalColor.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        color = QtGui.QColor("#ffffff" if self.cfg.get("overlay.theme", "dark") == "light" else "#171e2c")
        color.setAlpha(self._bg_alpha)
        painter.setBrush(color)
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)
