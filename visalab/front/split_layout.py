"""Разделение экрана графиков: дерево сплитов, пресеты, предпросмотр зон перетаскивания и кастомные разделители.

Дерево состоит из листьев ``PaneLeaf(key)`` и узлов ``PaneSplit(orientation, children, weights)``
(``"H"`` - рядом по горизонтали, ``"V"`` - друг под другом по вертикали). Веса нормированы к сумме 1.0,
поэтому пропорции разделения сохраняются при изменении размера окна и переходе в полный экран.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from .theme import (UI_BORDER_RADIUS_PX, UI_COLOR_BACKGROUND, UI_COLOR_BORDER, UI_COLOR_BORDER_LIGHT,
                    UI_COLOR_TEXT, UI_MONOSPACE_FONT_FAMILY)

__all__ = [
    "LAYOUT_MODES",
    "CUSTOM_LAYOUT_MODE",
    "PLOT_LAYOUT_MODES",
    "DROP_ZONES",
    "LAYOUT_DESCRIPTIONS",
    "PaneLeaf",
    "PaneSplit",
    "PaneNode",
    "clone_tree",
    "normalize_tree",
    "build_preset_tree",
    "leaf_order",
    "contains_key",
    "remove_panel",
    "swap_panels_in_tree",
    "dock_panel",
    "project_tree",
    "sync_weights_into_tree",
    "equalize_tree",
    "tree_to_dict",
    "tree_from_dict",
    "tree_structure_sig",
    "detect_drop_zone",
    "drop_zone_rect",
    "SplitHandle",
    "GraphSplitter",
    "DropZoneOverlay",
    "layout_preset_icon",
]

LAYOUT_MODES = ("1 COL", "2 COL", "ROW", "MAIN L", "MAIN T")
CUSTOM_LAYOUT_MODE = "SPLIT"
PLOT_LAYOUT_MODES = ("1 COL", "2 COL", "ROW")
DROP_ZONES = ("left", "right", "top", "bottom", "center")

LAYOUT_DESCRIPTIONS = {
    "1 COL": "Vertical stack (1 column)",
    "2 COL": "2-column grid",
    "ROW": "Side by side (1 row)",
    "MAIN L": "Main left + stacked right",
    "MAIN T": "Main top + split bottom",
    CUSTOM_LAYOUT_MODE: "Custom split screen",
}

_ZONE_LABELS = {
    "left": "SPLIT LEFT",
    "right": "SPLIT RIGHT",
    "top": "SPLIT TOP",
    "bottom": "SPLIT BOTTOM",
    "center": "SWAP",
}


@dataclass
class PaneLeaf:
    """Лист дерева: одна панель графика по ключу."""

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


def clone_tree(node: PaneNode | None) -> PaneNode | None:
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return PaneLeaf(node.key)
    return PaneSplit(node.orientation, [clone_tree(c) for c in node.children if c is not None], list(node.weights))


def normalize_tree(node: PaneNode | None) -> PaneNode | None:
    """Убрать пустые ветви, схлопнуть одиночные сплиты и сгладить вложенные сплиты того же направления."""
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return PaneLeaf(node.key)
    orient = "H" if node.orientation == "H" else "V"
    raw_weights = _norm_weights(node.weights, len(node.children))
    flat_children: list[PaneNode] = []
    flat_weights: list[float] = []
    for child, w in zip(node.children, raw_weights):
        norm = normalize_tree(child)
        if norm is None:
            continue
        if isinstance(norm, PaneSplit) and norm.orientation == orient:
            for gc, gw in zip(norm.children, norm.weights):
                flat_children.append(gc)
                flat_weights.append(w * gw)
        else:
            flat_children.append(norm)
            flat_weights.append(w)
    if not flat_children:
        return None
    if len(flat_children) == 1:
        return flat_children[0]
    return PaneSplit(orient, flat_children, _norm_weights(flat_weights, len(flat_children)))


def build_preset_tree(mode: str, keys: list[str]) -> PaneNode | None:
    """Построить дерево разделения для пресета ``1 COL / 2 COL / ROW / MAIN L / MAIN T``."""
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
    if mode == "MAIN L":
        right_keys = keys[1:]
        right: PaneNode = (PaneLeaf(right_keys[0]) if len(right_keys) == 1
                           else PaneSplit("V", [PaneLeaf(k) for k in right_keys], _norm_weights(None, len(right_keys))))
        return PaneSplit("H", [PaneLeaf(keys[0]), right], [0.58, 0.42])
    if mode == "MAIN T":
        bottom_keys = keys[1:]
        bottom: PaneNode = (PaneLeaf(bottom_keys[0]) if len(bottom_keys) == 1
                            else PaneSplit("H", [PaneLeaf(k) for k in bottom_keys], _norm_weights(None, len(bottom_keys))))
        return PaneSplit("V", [PaneLeaf(keys[0]), bottom], [0.55, 0.45])
    # По умолчанию - "1 COL"
    return PaneSplit("V", [PaneLeaf(k) for k in keys], _norm_weights(None, len(keys)))


def leaf_order(node: PaneNode | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, PaneLeaf):
        return [node.key]
    out: list[str] = []
    for c in node.children:
        out.extend(leaf_order(c))
    return out


def contains_key(node: PaneNode | None, key: str) -> bool:
    if node is None:
        return False
    if isinstance(node, PaneLeaf):
        return node.key == key
    return any(contains_key(c, key) for c in node.children)


def remove_panel(node: PaneNode | None, key: str) -> PaneNode | None:
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return None if node.key == key else PaneLeaf(node.key)
    kept_children: list[PaneNode] = []
    kept_weights: list[float] = []
    for c, w in zip(node.children, _norm_weights(node.weights, len(node.children))):
        pruned = remove_panel(c, key)
        if pruned is not None:
            kept_children.append(pruned)
            kept_weights.append(w)
    if not kept_children:
        return None
    return normalize_tree(PaneSplit(node.orientation, kept_children, kept_weights))


def swap_panels_in_tree(node: PaneNode | None, key_a: str, key_b: str) -> PaneNode | None:
    if node is None or key_a == key_b:
        return clone_tree(node)
    if isinstance(node, PaneLeaf):
        if node.key == key_a:
            return PaneLeaf(key_b)
        if node.key == key_b:
            return PaneLeaf(key_a)
        return PaneLeaf(node.key)
    return PaneSplit(
        node.orientation,
        [swap_panels_in_tree(c, key_a, key_b) for c in node.children],
        list(node.weights),
    )


def _replace_leaf(node: PaneNode, target_key: str, replacement: PaneNode) -> tuple[PaneNode, bool]:
    if isinstance(node, PaneLeaf):
        if node.key == target_key:
            return replacement, True
        return PaneLeaf(node.key), False
    replaced = False
    new_children: list[PaneNode] = []
    for c in node.children:
        if not replaced:
            nc, ok = _replace_leaf(c, target_key, replacement)
            new_children.append(nc)
            replaced = ok
        else:
            new_children.append(clone_tree(c))
    return PaneSplit(node.orientation, new_children, list(node.weights)), replaced


def dock_panel(node: PaneNode | None, src_key: str, dst_key: str, zone: str) -> PaneNode | None:
    """Разместить ``src_key`` относительно ``dst_key`` в зоне ``left / right / top / bottom / center``."""
    if node is None or not src_key or not dst_key or src_key == dst_key or zone not in DROP_ZONES:
        return clone_tree(node)
    if zone == "center":
        if contains_key(node, src_key) and contains_key(node, dst_key):
            return swap_panels_in_tree(node, src_key, dst_key)
        return clone_tree(node)
    pruned = remove_panel(node, src_key)
    if pruned is None:
        return PaneLeaf(src_key)
    if not contains_key(pruned, dst_key):
        return normalize_tree(PaneSplit("V", [pruned, PaneLeaf(src_key)], [0.5, 0.5]))
    orient = "H" if zone in ("left", "right") else "V"
    pair = [PaneLeaf(src_key), PaneLeaf(dst_key)] if zone in ("left", "top") else [PaneLeaf(dst_key), PaneLeaf(src_key)]
    split = PaneSplit(orient, pair, [0.5, 0.5])
    updated, _ok = _replace_leaf(pruned, dst_key, split)
    return normalize_tree(updated)


def _prune_to_visible(node: PaneNode | None, visible_set: set[str]) -> PaneNode | None:
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return PaneLeaf(node.key) if node.key in visible_set else None
    kept_children: list[PaneNode] = []
    kept_weights: list[float] = []
    for c, w in zip(node.children, _norm_weights(node.weights, len(node.children))):
        pruned = _prune_to_visible(c, visible_set)
        if pruned is not None:
            kept_children.append(pruned)
            kept_weights.append(w)
    if not kept_children:
        return None
    return normalize_tree(PaneSplit(node.orientation, kept_children, kept_weights))


def project_tree(tree: PaneNode | None, visible_keys: list[str]) -> PaneNode | None:
    """Спроецировать сохранённое дерево на текущие видимые панели без разрушения оригинала.

    Временно скрытые панели аккуратно пропускаются (соседи делят их место пропорционально), а новые
    добавляются снизу. Когда скрытая панель вернётся - она встанет на своё прежнее место в дереве.
    """
    visible_keys = [str(k) for k in visible_keys if k]
    if not visible_keys:
        return None
    vis_set = set(visible_keys)
    projected = _prune_to_visible(tree, vis_set)
    present = set(leaf_order(projected))
    missing = [k for k in visible_keys if k not in present]
    if not missing:
        return projected
    extra = [PaneLeaf(k) for k in missing]
    if projected is None:
        return normalize_tree(PaneSplit("V", extra, _norm_weights(None, len(extra))))
    if isinstance(projected, PaneSplit) and projected.orientation == "V":
        n_old = len(projected.children)
        n_new = len(extra)
        scale_old = n_old / (n_old + n_new)
        new_w = 1.0 / (n_old + n_new)
        children = list(projected.children) + extra
        weights = [w * scale_old for w in projected.weights] + [new_w] * n_new
        return normalize_tree(PaneSplit("V", children, weights))
    total = 1 + len(extra)
    return normalize_tree(PaneSplit("V", [projected, *extra], [1.0 / total] * total))


def sync_weights_into_tree(full_tree: PaneNode | None, active_tree: PaneNode | None) -> PaneNode | None:
    """Перенести актуальные веса из спроецированного дерева в полное, не теряя временно скрытые панели."""
    if full_tree is None or active_tree is None:
        return clone_tree(full_tree)
    vis_set = set(leaf_order(active_tree))
    full_set = set(leaf_order(full_tree))
    if vis_set == full_set:
        return clone_tree(active_tree)
    # Соберём таблицу весов из active_tree по (orientation, frozenset-ы листьев каждого ребёнка)
    weight_map: dict[tuple[str, tuple[frozenset[str], ...]], list[float]] = {}

    def collect(node: PaneNode) -> None:
        if isinstance(node, PaneSplit):
            sig = (node.orientation, tuple(frozenset(leaf_order(c)) for c in node.children))
            weight_map[sig] = list(node.weights)
            for c in node.children:
                collect(c)

    collect(active_tree)

    def apply(node: PaneNode) -> PaneNode:
        if isinstance(node, PaneLeaf):
            return PaneLeaf(node.key)
        children = [apply(c) for c in node.children]
        weights = _norm_weights(node.weights, len(children))
        vis_indices: list[int] = []
        vis_sets: list[frozenset[str]] = []
        for idx, c in enumerate(children):
            c_vis = frozenset(k for k in leaf_order(c) if k in vis_set)
            if c_vis:
                vis_indices.append(idx)
                vis_sets.append(c_vis)
        key = (node.orientation, tuple(vis_sets))
        if len(vis_indices) >= 2 and key in weight_map:
            new_sub = _norm_weights(weight_map[key], len(vis_indices))
            vis_total = sum(weights[i] for i in vis_indices)
            if vis_total > 0:
                for idx, nw in zip(vis_indices, new_sub):
                    weights[idx] = nw * vis_total
        return PaneSplit(node.orientation, children, _norm_weights(weights, len(children)))

    return apply(full_tree)


def equalize_tree(node: PaneNode | None) -> PaneNode | None:
    """Выровнять все сплиттеры в дереве до равных долей."""
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return PaneLeaf(node.key)
    children = [equalize_tree(c) for c in node.children if c is not None]
    return normalize_tree(PaneSplit(node.orientation, children, _norm_weights(None, len(children))))


def tree_to_dict(node: PaneNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    if isinstance(node, PaneLeaf):
        return {"p": node.key}
    norm = normalize_tree(node)
    if norm is None:
        return None
    if isinstance(norm, PaneLeaf):
        return {"p": norm.key}
    return {
        "d": norm.orientation,
        "w": [round(float(w), 5) for w in norm.weights],
        "c": [tree_to_dict(c) for c in norm.children],
    }


def tree_from_dict(data: Any) -> PaneNode | None:
    if not isinstance(data, dict):
        return None
    if "p" in data:
        key = str(data["p"])
        return PaneLeaf(key) if key else None
    orient = data.get("d")
    raw_children = data.get("c")
    if orient not in ("H", "V") or not isinstance(raw_children, list):
        return None
    children: list[PaneNode] = []
    raw_weights = data.get("w")
    weights: list[float] = []
    has_w = isinstance(raw_weights, list) and len(raw_weights) == len(raw_children)
    for i, item in enumerate(raw_children):
        child = tree_from_dict(item)
        if child is not None:
            children.append(child)
            weights.append(float(raw_weights[i]) if has_w and isinstance(raw_weights[i], (int, float)) else 1.0)
    if not children:
        return None
    return normalize_tree(PaneSplit(orient, children, weights))


def tree_structure_sig(node: PaneNode | None) -> tuple:
    """Структурная подпись дерева без весов (чтобы перетаскивание ручки не перестраивало виджеты)."""
    if node is None:
        return ()
    if isinstance(node, PaneLeaf):
        return ("L", node.key)
    return (node.orientation, tuple(tree_structure_sig(c) for c in node.children))


# ------------------------------------------------------------ геометрия зон перетаскивания
def detect_drop_zone(rect: QRectF, pos: QPointF, edge_ratio: float = 0.26) -> str:
    """Определить зону сброса (left / right / top / bottom / center) по координате внутри панели."""
    w = max(float(rect.width()), 1.0)
    h = max(float(rect.height()), 1.0)
    nx = (float(pos.x()) - float(rect.left())) / w
    ny = (float(pos.y()) - float(rect.top())) / h
    nx = min(max(nx, 0.0), 1.0)
    ny = min(max(ny, 0.0), 1.0)
    dists = {
        "left": nx,
        "right": 1.0 - nx,
        "top": ny,
        "bottom": 1.0 - ny,
    }
    nearest, dist = min(dists.items(), key=lambda kv: kv[1])
    return nearest if dist < edge_ratio else "center"


def drop_zone_rect(rect: QRectF, zone: str) -> QRectF:
    """Прямоугольник предпросмотра для зоны ``zone`` внутри ``rect``."""
    r = QRectF(rect).adjusted(4, 4, -4, -4)
    if r.width() <= 4 or r.height() <= 4:
        return QRectF(rect)
    if zone == "left":
        return QRectF(r.left(), r.top(), r.width() * 0.5, r.height())
    if zone == "right":
        return QRectF(r.left() + r.width() * 0.5, r.top(), r.width() * 0.5, r.height())
    if zone == "top":
        return QRectF(r.left(), r.top(), r.width(), r.height() * 0.5)
    if zone == "bottom":
        return QRectF(r.left(), r.top() + r.height() * 0.5, r.width(), r.height() * 0.5)
    # center -> SWAP
    mx = r.width() * 0.16
    my = r.height() * 0.16
    return r.adjusted(mx, my, -mx, -my)


# ------------------------------------------------------------ Qt-виджеты сплита
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


class DropZoneOverlay(QWidget):
    """Полупрозрачный предпросмотр зоны разделения/обмена при перетаскивании панели поверх другой."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.zone: str | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._font = QFont(UI_MONOSPACE_FONT_FAMILY.split(",")[0].strip('" '))
        self._font.setPointSizeF(9.5)
        self._font.setBold(True)
        self.hide()

    def set_zone(self, zone: str | None) -> None:
        if zone not in DROP_ZONES:
            zone = None
        if zone == self.zone:
            if zone is not None:
                self.setGeometry(self.parentWidget().rect())
                self.raise_()
            return
        self.zone = zone
        if zone is None:
            self.hide()
        else:
            self.setGeometry(self.parentWidget().rect())
            self.show()
            self.raise_()
            self.update()

    def paintEvent(self, e):  # noqa: N802 - Qt
        if self.zone is None:
            return
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        target = drop_zone_rect(QRectF(self.rect()), self.zone)
        fill = QColor(UI_COLOR_TEXT)
        fill.setAlpha(34)
        p.setBrush(fill)
        pen = QPen(QColor(UI_COLOR_TEXT), 1.5, Qt.PenStyle.DashLine if self.zone == "center" else Qt.PenStyle.SolidLine)
        p.setPen(pen)
        p.drawRoundedRect(target, UI_BORDER_RADIUS_PX, UI_BORDER_RADIUS_PX)

        # Компактный бейдж по центру зоны
        label = _ZONE_LABELS.get(self.zone, "")
        if label:
            p.setFont(self._font)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label) + 18
            th = fm.height() + 10
            badge = QRectF(target.center().x() - tw / 2.0, target.center().y() - th / 2.0, tw, th)
            bg = QColor(UI_COLOR_BACKGROUND)
            bg.setAlpha(220)
            p.setPen(QPen(QColor(UI_COLOR_BORDER_LIGHT), 1.0))
            p.setBrush(bg)
            p.drawRoundedRect(badge, 4, 4)
            p.setPen(QColor(UI_COLOR_TEXT))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)


def layout_preset_icon(mode: str, size: int = 16) -> QIcon:
    """Миниатюрная векторная иконка раскладки для меню выбора разделения экрана."""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    if p.isActive():
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = float(size)
        pad = 1.5
        gap = 1.5
        w = s - 2 * pad
        h = s - 2 * pad
        hw = (w - gap) / 2.0
        hh = (h - gap) / 2.0

        rects: list[QRectF]
        if mode == "1 COL":
            rects = [QRectF(pad, pad, w, hh), QRectF(pad, pad + hh + gap, w, hh)]
        elif mode == "ROW":
            rects = [QRectF(pad, pad, hw, h), QRectF(pad + hw + gap, pad, hw, h)]
        elif mode == "2 COL":
            rects = [
                QRectF(pad, pad, hw, hh),
                QRectF(pad + hw + gap, pad, hw, hh),
                QRectF(pad, pad + hh + gap, hw, hh),
                QRectF(pad + hw + gap, pad + hh + gap, hw, hh),
            ]
        elif mode == "MAIN L":
            lw = w * 0.54
            rw = w - lw - gap
            rects = [
                QRectF(pad, pad, lw, h),
                QRectF(pad + lw + gap, pad, rw, hh),
                QRectF(pad + lw + gap, pad + hh + gap, rw, hh),
            ]
        elif mode == "MAIN T":
            th = h * 0.52
            bh = h - th - gap
            rects = [
                QRectF(pad, pad, w, th),
                QRectF(pad, pad + th + gap, hw, bh),
                QRectF(pad + hw + gap, pad + th + gap, hw, bh),
            ]
        else:
            # SPLIT / custom
            rects = [
                QRectF(pad, pad, hw, h * 0.6),
                QRectF(pad, pad + h * 0.6 + gap, hw, h * 0.4 - gap),
                QRectF(pad + hw + gap, pad, hw, h),
            ]
        fill = QColor(UI_COLOR_TEXT)
        fill.setAlpha(45)
        p.setBrush(fill)
        p.setPen(QPen(QColor(UI_COLOR_TEXT), 1.1))
        for r in rects:
            p.drawRoundedRect(r, 1.5, 1.5)
        p.end()
    return QIcon(pm)
