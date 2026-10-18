# -*- coding: utf-8 -*-
"""照片背景层：模糊、压暗、渐变，切换时淡入。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..paths import BACKGROUNDS, CACHE


def background_files() -> list[Path]:
    if not BACKGROUNDS.exists():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    return sorted([p for p in BACKGROUNDS.iterdir() if p.suffix.lower() in exts])


def load_credits() -> dict:
    f = BACKGROUNDS / "credits.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def resolve_background(name: str) -> Path | None:
    if not name or name == "none":
        return None
    files = background_files()
    if not files:
        return None
    if name == "auto":
        return files[0]
    for f in files:
        if f.name == name or f.stem == name:
            return f
    return files[0]


class BackgroundLayer(QtWidgets.QWidget):
    """铺满窗口的照片背景；模糊/压暗/不透明度可调，切换时淡入。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = "light"
        self._cur: QtGui.QPixmap | None = None
        self._prev: QtGui.QPixmap | None = None
        self._scaled: dict[tuple, QtGui.QPixmap] = {}   # 按尺寸缓存的缩放结果
        self._fade = 1.0
        self._dim = 45
        self._opacity = 100
        self._name = ""
        self._blur = 16
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_fade)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _on_fade(self, v) -> None:
        self._fade = float(v)
        self.update()

    def _scaled_for(self, pm: QtGui.QPixmap, size: QtCore.QSize) -> QtGui.QPixmap:
        """把背景图缩放到窗口大小（带缓存）。

        以前每次重绘都要平滑缩放一张上千像素的图；淡入动画 450 毫秒里要重绘几十次，
        白白占着 UI 线程。缓存之后同一张图同一尺寸只算一次。
        """
        key = (size.width(), size.height(), pm.cacheKey())
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        out = pm.scaled(size, QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        QtCore.Qt.TransformationMode.SmoothTransformation)
        if len(self._scaled) > 3:
            self._scaled.clear()
        self._scaled[key] = out
        return out

    def _blurred_pixmap(self, path: Path, size: QtCore.QSize, blur: int) -> QtGui.QPixmap | None:
        if size.width() <= 0 or size.height() <= 0:
            return None
        key = f"{path.name}_{size.width()}x{size.height()}_{blur}"
        cache_file = CACHE / (hashlib.md5(key.encode()).hexdigest() + ".jpg")
        if not cache_file.exists():
            try:
                from PIL import Image, ImageFilter

                img = Image.open(path).convert("RGB")
                sw, sh = img.size
                scale = max(size.width() / sw, size.height() / sh)
                nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
                img = img.resize((nw, nh), Image.LANCZOS)
                left = (nw - size.width()) // 2
                top = (nh - size.height()) // 2
                img = img.crop((left, top, left + size.width(), top + size.height()))
                if blur > 0:
                    radius = max(0.5, blur * size.height() / 900.0)
                    img = img.filter(ImageFilter.GaussianBlur(radius))
                CACHE.mkdir(parents=True, exist_ok=True)
                img.save(cache_file, quality=88)
            except Exception:
                return QtGui.QPixmap(str(path))
        pm = QtGui.QPixmap(str(cache_file))
        return pm if not pm.isNull() else None

    def set_background(self, name: str, blur: int, dim: int, opacity: int = 100,
                       animate: bool = True) -> None:
        self._name, self._blur = name, blur
        self._dim = dim
        self._opacity = opacity
        size = self.size()
        if size.isEmpty():
            size = QtCore.QSize(1100, 700)
        path = resolve_background(name)
        pm = self._blurred_pixmap(path, size, blur) if path else None
        if pm is None:
            self._anim.stop()
            self._prev = self._cur = None
            self._fade = 1.0
            self.update()
            return
        if self._cur is not None and animate:
            self._prev = self._cur
            self._cur = pm
            self._anim.stop()
            self._anim.start()
        else:
            self._prev = None
            self._cur = pm
            self._fade = 1.0
            self.update()

    def set_tuning(self, dim: int, opacity: int) -> None:
        self._dim = dim
        self._opacity = opacity
        self.update()

    def reflow(self) -> None:
        """窗口尺寸变化后重新生成背景图（带缓存，代价很低）。"""
        if self._name:
            self.set_background(self._name, self._blur, self._dim, self._opacity, animate=False)

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = self.rect()
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(QtCore.QRectF(rect), 14, 14)
        p.setClipPath(clip)
        p.fillRect(rect, QtGui.QColor("#f7f8fa" if self._theme == "light" else "#14161c"))
        if self._cur is None and self._prev is None:
            return
        layers = [(self._prev, 1.0 - self._fade), (self._cur, self._fade if self._prev else 1.0)]
        for pm, alpha in layers:
            if pm is None or alpha <= 0.01:
                continue
            scaled = self._scaled_for(pm, rect.size())
            x = (scaled.width() - rect.width()) // 2
            y = (scaled.height() - rect.height()) // 2
            p.setOpacity(alpha * (self._opacity / 100.0))
            p.drawPixmap(-x, -y, scaled)
        p.setOpacity(self._opacity / 100.0)
        dim_a = max(0, min(230, int(self._dim * 2.3)))
        p.fillRect(rect, QtGui.QColor(8, 10, 16, dim_a))
        grad = QtGui.QLinearGradient(0, rect.height() * 0.35, 0, rect.height())
        grad.setColorAt(0.0, QtGui.QColor(8, 10, 16, 0))
        grad.setColorAt(1.0, QtGui.QColor(8, 10, 16, min(185, dim_a + 95)))
        p.fillRect(rect, QtGui.QBrush(grad))
