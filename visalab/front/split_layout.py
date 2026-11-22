"""Делители экрана и раскладка внутренних графиков панели ВАЦ.

Содержит только используемые примитивы:
* ``GraphSplitter`` / ``SplitHandle`` — разделитель с выравниванием по двойному клику и
  пропорциональными весами (для двухпанельного LIVE-экрана и мультиграфиков ВАЦ);
* ``PaneLeaf`` / ``PaneSplit`` / ``build_preset_tree`` — пресеты раскладки графиков ВАЦ
  (``1 COL``, ``2 COL``, ``ROW``).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSplitter, QSplitterHandle

from .theme import UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT, UI_COLOR_TEXT

__all__ = [
    "PLOT_LAYOUT_MODES",
    "PaneLeaf",
    "PaneSplit",
    "PaneNode",
    "build_preset_tree",
    "SplitHandle",
    "GraphSplitter",
]

PLOT_LAYOUT_MODES = ("1 COL", "2 COL", "ROW")


@dataclass
class PaneLeaf:
    """Лист дерева раскладки: один график по ключу."""

    key: str


@dataclass
class PaneSplit:
    """Разделение области на 2+ дочерних узла по горизонтали (H) или вертикали (V)."""

    orientation: str  # "H" | "V"
    children: list[PaneNode]
    weights: list[float]


PaneNode = PaneLeaf | PaneSplit


def _norm_weights(weights: list[float] | None, count: int) -> list[float]:
    if count <= 0:
        return []
    if not weights or len(weights) != count:
        return [1.0 / count] * count
    cleaned = [float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w) and w > 0
               else 0.0 for w in weights]
    if any(w <= 0 for w in cleaned):
        pos = [w for w in cleaned if w > 0]
        fallback = (sum(pos) / len(pos)) if pos else 1.0
        cleaned = [w if w > 0 else fallback for w in cleaned]
    total = sum(cleaned)
    return [w / total for w in cleaned]


def build_preset_tree(mode: str, keys: list[str]) -> PaneNode | None:
    """Построить дерево раскладки графиков ВАЦ для пресета ``1 COL / 2 COL / ROW``."""
    keys = [str(k) for k in keys if k]
    if not keys:
        return None
    if len(keys) == 1:
        return PaneLeaf(keys[0])
    if mode == "ROW":
        return PaneSplit("H", [PaneLeaf(k) for k in keys], _norm_weights(None, len(keys)))
    if mode == "2 COL":
        rows: list[PaneNode] = []
        for i in range(0, len(keys), 2):
            chunk = keys[i:i + 2]
            if len(chunk) == 2:
                rows.append(PaneSplit("H", [PaneLeaf(chunk[0]), PaneLeaf(chunk[1])], [0.5, 0.5]))
            else:
                rows.append(PaneLeaf(chunk[0]))
        if len(rows) == 1:
            return rows[0]
        return PaneSplit("V", rows, _norm_weights(None, len(rows)))
    # По умолчанию - "1 COL"
    return PaneSplit("V", [PaneLeaf(k) for k in keys], _norm_weights(None, len(keys)))


class SplitHandle(QSplitterHandle):
    """Разделитель с аккуратной центральной риской; двойной клик выравнивает панели поровну."""

    def __init__(self, orientation: Qt.Orientation, parent: GraphSplitter):
        super().__init__(orientation, parent)
        self._hovered = False
        self.setMouseTracking(True)
        self.setToolTip("Drag to resize split; double-click to equalize")

    def enterEvent(self, e):  # noqa: N802 - Qt
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802 - Qt
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802 - Qt
        if e.button() == Qt.MouseButton.LeftButton:
            sp = self.splitter()
            if isinstance(sp, GraphSplitter):
                sp.equalize()
                e.accept()
                return
        super().mouseDoubleClickEvent(e)

    def paintEvent(self, e):  # noqa: N802 - Qt
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        if self._hovered:
            p.setPen(Qt.PenStyle.NoPen)
            bg = QColor(UI_COLOR_BORDER_LIGHT)
            bg.setAlpha(45)
            p.setBrush(bg)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 2, 2)
        grip_color = QColor(UI_COLOR_TEXT if self._hovered else UI_COLOR_BORDER)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grip_color)
        if self.orientation() == Qt.Orientation.Horizontal:
            gh = min(32.0, max(12.0, r.height() * 0.18))
            gw = 2.0
            grip = QRectF(r.center().x() - gw / 2.0, r.center().y() - gh / 2.0, gw, gh)
        else:
            gw = min(32.0, max(12.0, r.width() * 0.18))
            gh = 2.0
            grip = QRectF(r.center().x() - gw / 2.0, r.center().y() - gh / 2.0, gw, gh)
        p.drawRoundedRect(grip, 1.0, 1.0)


class GraphSplitter(QSplitter):
    """QSplitter с выравниванием по двойному клику и пропорциональными весами."""

    equalized = pyqtSignal()
    _WEIGHT_SCALE = 10000

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(False)
        self.setHandleWidth(8)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802 - Qt
        return SplitHandle(self.orientation(), self)

    def apply_weights(self, weights: list[float] | None) -> None:
        n = self.count()
        if n <= 0:
            return
        norm = _norm_weights(weights, n)
        self.setSizes([max(1, round(w * self._WEIGHT_SCALE)) for w in norm])

    def current_weights(self) -> list[float] | None:
        sizes = self.sizes()
        if len(sizes) != self.count() or len(sizes) < 2 or not all(s > 0 for s in sizes):
            return None
        total = float(sum(sizes))
        return [s / total for s in sizes]

    def equalize(self) -> None:
        n = self.count()
        if n < 2:
            return
        sizes = self.sizes()
        total = sum(sizes)
        if total > 0:
            each = max(1, total // n)
            self.setSizes([each] * n)
        else:
            self.setSizes([self._WEIGHT_SCALE // n] * n)
        self.equalized.emit()
