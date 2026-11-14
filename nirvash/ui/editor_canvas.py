"""The editor's picture (with boxes you drag), its timeline, and live preview playback."""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..edit import render as R
from ..edit.project import Project, interp
from ..media import ff
from .theme import C, T
from .widgets import fmt_time


def to_qimage(img: np.ndarray) -> QImage:
    h, w = img.shape[:2]
    img = np.ascontiguousarray(img)
    q = QImage(img.data, w, h, w * 3, QImage.Format_BGR888)
    return q.copy()


# ---- the picture --------------------------------------------------------------------------------------------------
class FrameCanvas(QWidget):
    """Shows the frame at the playhead. In edit view it's the whole picture, with the crop box, blur boxes and
    overlays drawn on it to move and resize; in result view (and while playing) it's exactly what gets exported.

    boxChanged(kind, id, (x, y, w, h)) - while dragging; boxCommitted() on release; boxPicked(kind, id) on click;
    boxDrawn((x, y, w, h)) when a new blur box is drawn on empty picture."""

    boxChanged = Signal(str, str, object)
    boxCommitted = Signal()
    boxPicked = Signal(str, str)
    boxDrawn = Signal(object)

    H = 6   # handle half size

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.project: Project | None = None
        self.t = 0.0
        self.base: np.ndarray | None = None      # canvas frame (colour etc. done), preview size
        self.image: QImage | None = None
        self.result = False
        self.tool = "clip"
        self.selected: tuple[str, str] | None = None
        self.ops: R.FrameOps | None = None
        self._drag = None
        self._overlay_pm: dict = {}
        self.message = ""

    # ---- content ---------------------------------------------------------------------------
    def set_project(self, p: Project | None) -> None:
        self.project = p
        self.refresh_ops()

    def refresh_ops(self) -> None:
        """Project changed: rebuild what draws the moving parts (cheap), then the picture."""
        p = self.project
        if p is None or not p.clips:
            self.ops = None
            self.image = None
            self.update()
            return
        if self.base is not None:
            cw, ch = self.base.shape[1], self.base.shape[0]
            self.ops = R.FrameOps(p, R.preview_out_size(p, cw, ch))
        self.recompose()

    def set_frame(self, base: np.ndarray | None, t: float) -> None:
        self.t = t
        new_size = base is not None and (self.base is None or base.shape != self.base.shape)
        self.base = base
        if new_size or self.ops is None:
            self.refresh_ops()
        else:
            self.recompose()

    def recompose(self) -> None:
        if self.base is None or self.ops is None or self.project is None:
            self.image = None
            self.update()
            return
        img = self.base.copy()
        try:
            self.ops.blurs(img, self.t)
            if self.result:
                img = self.ops.finish(img, self.t)
        except Exception as e:  # noqa: BLE001 - a bad box mustn't take the editor down
            self.message = f"Preview problem: {e}"
        self.image = to_qimage(img)
        self.update()

    # ---- geometry ------------------------------------------------------------------------------
    def pic_rect(self) -> QRectF:
        if self.image is None:
            return QRectF(self.rect())
        iw, ih = self.image.width(), self.image.height()
        k = min(self.width() / iw, self.height() / ih)
        w, h = iw * k, ih * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_px(self, box) -> QRectF:
        r = self.pic_rect()
        x, y, w, h = box
        return QRectF(r.x() + x * r.width(), r.y() + y * r.height(), w * r.width(), h * r.height())

    def _to_norm(self, pt: QPointF) -> tuple[float, float]:
        r = self.pic_rect()
        return (pt.x() - r.x()) / max(1, r.width()), (pt.y() - r.y()) / max(1, r.height())

    def overlay_box(self, o) -> tuple[float, float, float, float]:
        """An overlay's place on the canvas (overlays are placed on the output frame, inside the crop)."""
        p = self.project
        src = self.ops.sources.get(o.path) if self.ops else None
        aspect = src.aspect() if src else 1.0
        OW, OH = p.output_size()
        h = o.w * OW * aspect / OH
        box = p.crop_box(self.t) if not self.result else None
        if box is None:
            return o.x, o.y, o.w, h
        cx, cy, cw, ch = box
        return cx + o.x * cw, cy + o.y * ch, o.w * cw, h * ch

    def items(self) -> list[tuple[str, str, tuple]]:
        """What can be dragged now, topmost last."""
        p = self.project
        out = []
        if p is None or self.result:
            return out
        if self.tool == "crop" and p.crop.enabled:
            b = p.crop_box(self.t)
            if b:
                out.append(("crop", "crop", b))
        if self.tool == "blur":
            for b in p.blurs:
                if b.start <= self.t <= p.blur_end(b):
                    out.append(("blur", b.id, interp(b.keys, self.t)))
        if self.tool == "overlay":
            for o in p.overlays:
                if o.start <= self.t <= p.overlay_end(o):
                    out.append(("overlay", o.id, self.overlay_box(o)))
        return out

    # ---- drawing -----------------------------------------------------------------------------------
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C["stage"] if "stage" in C else "#05060a"))
        pr = self.project
        if self.image is None:
            p.setPen(QColor(C["faint"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.message or ("Loading the picture…" if pr and pr.clips else ""))
            return
        r = self.pic_rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(r, self.image)
        if self.result or pr is None:
            return
        p.setRenderHint(QPainter.Antialiasing)
        # where the crop is: everything outside it dimmed (always shown, so you see the framing in any tool)
        box = pr.crop_box(self.t)
        if box is not None:
            cr = self._to_px(box)
            path = QPainterPath()
            path.addRect(r)
            path.addRect(cr)
            dim = QColor(0, 0, 0, 140 if self.tool == "crop" else 90)
            p.fillPath(path, dim)
            p.setPen(QPen(QColor(C["acc"]), 1.5, Qt.SolidLine if self.tool == "crop" else Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(cr)
            if self.tool == "crop":     # rule of thirds
                p.setPen(QPen(QColor(255, 255, 255, 60), 1))
                for k in (1, 2):
                    p.drawLine(QPointF(cr.x() + cr.width() * k / 3, cr.y()), QPointF(cr.x() + cr.width() * k / 3, cr.bottom()))
                    p.drawLine(QPointF(cr.x(), cr.y() + cr.height() * k / 3), QPointF(cr.right(), cr.y() + cr.height() * k / 3))
        # overlays (edit view draws them here; the picture itself doesn't include them)
        for o in pr.overlays:
            if not (o.start <= self.t <= pr.overlay_end(o)) or self.ops is None or o.path not in self.ops.sources:
                continue
            pm = self._overlay_pixmap(o)
            if pm is not None:
                p.setOpacity(o.opacity)
                p.drawPixmap(self._to_px(self.overlay_box(o)).toRect(), pm)
                p.setOpacity(1.0)
        # boxes of the current tool
        for kind, iid, b in self.items():
            rr = self._to_px(b)
            sel = self.selected == (kind, iid)
            col = QColor(C["acc"]) if kind != "blur" else QColor(C["warn"])
            p.setPen(QPen(col, 2 if sel else 1.2, Qt.SolidLine if sel else Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            if kind == "blur" and next((x for x in pr.blurs if x.id == iid), None) is not None and \
                    next(x for x in pr.blurs if x.id == iid).shape == "ellipse":
                p.drawEllipse(rr)
                p.setPen(QPen(col, 1, Qt.DotLine))
            p.drawRect(rr)
            if sel or kind == "crop":
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                for hx, hy in self._handles(rr):
                    p.drawRect(QRectF(hx - self.H, hy - self.H, self.H * 2, self.H * 2))
        tn = getattr(self, "_temp_new", None)
        if tn:
            p.setPen(QPen(QColor(C["warn"]), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._to_px(tn))
        if self.message:
            p.setPen(QColor(C["warn"]))
            p.drawText(self.rect().adjusted(8, 8, -8, -8), Qt.AlignLeft | Qt.AlignBottom, self.message)

    def _overlay_pixmap(self, o) -> QPixmap | None:
        src = self.ops.sources.get(o.path)
        if src is None or not src.frames:
            return None
        i = int((self.t - o.start) * src.fps) % len(src.frames) if len(src.frames) > 1 else 0
        key = (o.path, i)
        pm = self._overlay_pm.get(key)
        if pm is None:
            f = np.ascontiguousarray(src.frames[i])
            q = QImage(f.data, f.shape[1], f.shape[0], f.shape[1] * 4, QImage.Format_ARGB32).copy()
            pm = QPixmap.fromImage(q)
            if len(self._overlay_pm) > 300:
                self._overlay_pm.clear()
            self._overlay_pm[key] = pm
        return pm

    @staticmethod
    def _handles(r: QRectF) -> list[tuple[float, float]]:
        xs = (r.left(), r.center().x(), r.right())
        ys = (r.top(), r.center().y(), r.bottom())
        return [(x, y) for y in ys for x in xs if not (x == xs[1] and y == ys[1])]

    # ---- mouse -------------------------------------------------------------------------------------
    def _hit(self, pos: QPointF):
        for kind, iid, b in reversed(self.items()):
            rr = self._to_px(b)
            for n, (hx, hy) in enumerate(self._handles(rr)):
                if abs(pos.x() - hx) <= self.H + 3 and abs(pos.y() - hy) <= self.H + 3 and \
                        (self.selected == (kind, iid) or kind == "crop"):
                    return kind, iid, b, n
            if rr.contains(pos):
                return kind, iid, b, None
        return None

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.LeftButton or self.project is None or self.image is None:
            return
        pos = e.position()
        hit = self._hit(pos)
        if hit:
            kind, iid, b, handle = hit
            if self.selected != (kind, iid):
                self.selected = (kind, iid)
                self.boxPicked.emit(kind, iid)
            self._drag = {"kind": kind, "id": iid, "start": self._to_norm(pos), "box": b, "handle": handle}
        elif self.tool == "blur" and self.pic_rect().contains(pos):
            x, y = self._to_norm(pos)
            self._drag = {"kind": "new", "start": (x, y), "box": (x, y, 0.0, 0.0), "handle": 8}
        else:
            self.selected = None
            self.boxPicked.emit("", "")
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        if self._drag is None:
            hit = self._hit(pos)
            if hit and hit[3] is not None:
                n = hit[3]
                self.setCursor(Qt.SizeFDiagCursor if n in (0, 7) else Qt.SizeBDiagCursor if n in (2, 5)
                               else Qt.SizeVerCursor if n in (1, 6) else Qt.SizeHorCursor)
            elif hit:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.CrossCursor if self.tool == "blur" else Qt.ArrowCursor)
            return
        d = self._drag
        x, y = self._to_norm(pos)
        sx, sy = d["start"]
        bx, by, bw, bh = d["box"]
        dx, dy = x - sx, y - sy
        h = d["handle"]
        if d["kind"] == "new":
            nb = (min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))
            d["box"] = nb
            self._temp_new = nb
            self.update()
            return
        if h is None:                                       # move
            nb = (min(1 - bw, max(0.0, bx + dx)), min(1 - bh, max(0.0, by + dy)), bw, bh)
        else:                                               # resize from a handle
            l, t, r, b = bx, by, bx + bw, by + bh
            if h in (0, 3, 5):
                l = min(r - 0.02, max(0.0, bx + dx))
            if h in (2, 4, 7):
                r = max(l + 0.02, min(1.0, bx + bw + dx))
            if h in (0, 1, 2):
                t = min(b - 0.02, max(0.0, by + dy))
            if h in (5, 6, 7):
                b = max(t + 0.02, min(1.0, by + bh + dy))
            nb = (l, t, r - l, b - t)
            nb = self._keep_aspect(d, nb, h)
        self.boxChanged.emit(d["kind"], d["id"], nb)

    def _keep_aspect(self, d, nb, h):
        """The crop keeps its chosen shape; overlays keep the picture's shape."""
        p = self.project
        x, y, w, hh = nb
        if self.image is None:
            return nb
        pw, ph = self.image.width(), self.image.height()
        if d["kind"] == "crop":
            a = p.aspect_value()
        elif d["kind"] == "overlay":
            ob = d["box"]
            a = (ob[2] * pw) / max(1e-6, ob[3] * ph)
        else:
            return nb
        if not a:
            return nb
        # aspect in normalized units: w*pw / (h*ph) = a
        if h in (1, 6):                       # top/bottom edge: width follows height
            w2 = hh * ph * a / pw
            cx = x + w / 2
            x, w = cx - w2 / 2, w2
        else:
            h2 = w * pw / a / ph
            if h in (0, 1, 2):
                y = y + hh - h2
            hh = h2
        if w > 1:
            w = 1.0
            hh = w * pw / a / ph
        if hh > 1:
            hh = 1.0
            w = hh * ph * a / pw
        x = min(1 - w, max(0.0, x))
        y = min(1 - hh, max(0.0, y))
        return x, y, w, hh

    def mouseReleaseEvent(self, e):  # noqa: N802
        d = self._drag
        self._drag = None
        if d is None:
            return
        if d["kind"] == "new":
            self._temp_new = None
            b = d["box"]
            if b[2] > 0.01 and b[3] > 0.01:
                self.boxDrawn.emit(b)
            self.update()
            return
        self.boxCommitted.emit()


# ---- the timeline ---------------------------------------------------------------------------------------------------
class Timeline(QWidget):
    """Clips, then a lane each for the crop's keys, blur boxes and overlays. Click or drag to move the playhead;
    drag the ends of a blur or overlay bar to change when it's on."""

    seek = Signal(float)
    picked = Signal(str, str)
    rangeChanged = Signal(str, str, float, float)
    committed = Signal()

    LANE = 20
    RULER = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.t = 0.0
        self.selected: tuple[str, str] | None = None
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumHeight(self.RULER + self.LANE * 2 + 8)

    def lanes(self) -> list[tuple[str, str, str]]:
        p = self.project
        if p is None:
            return []
        out = [("clips", "", "Clips")]
        if p.crop.enabled:
            out.append(("crop", "crop", "Crop"))
        for i, b in enumerate(p.blurs):
            out.append(("blur", b.id, f"{'Pixelate' if b.kind == 'pixelate' else 'Blur'} {i + 1}"))
        for o in p.overlays:
            out.append(("overlay", o.id, Path(o.path).stem or "Overlay"))
        return out

    def set_project(self, p: Project | None) -> None:
        self.project = p
        self.setMinimumHeight(self.RULER + self.LANE * max(2, len(self.lanes())) + 8)
        self.update()

    def _x(self, t: float) -> float:
        d = self.project.duration if self.project else 0
        return 70 + (t / d if d > 0 else 0) * (self.width() - 78)

    def _t(self, x: float) -> float:
        d = self.project.duration if self.project else 0
        return max(0.0, min(d, (x - 70) / max(1, self.width() - 78) * d))

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pr = self.project
        p.fillRect(self.rect(), QColor(C["field"]))
        if pr is None or pr.duration <= 0:
            return
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        d = pr.duration
        w = self.width() - 78
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if d / s <= w / 60), 1200)
        p.setPen(QColor(C["faint"]))
        t = 0.0
        while t <= d + 1e-6:
            x = self._x(t)
            p.drawLine(QPointF(x, self.RULER - 4), QPointF(x, self.RULER))
            p.drawText(QRectF(x - 30, 0, 60, self.RULER - 3), Qt.AlignCenter, fmt_time(t))
            t += step
        stops = T["stops"]
        for n, (kind, iid, name) in enumerate(self.lanes()):
            y = self.RULER + 4 + n * self.LANE
            p.setPen(QColor(C["sec"]))
            p.drawText(QRectF(4, y, 62, self.LANE - 4), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(name, Qt.ElideRight, 62))
            sel = self.selected == (kind, iid)
            if kind == "clips":
                for i, c in enumerate(pr.clips):
                    a, b = pr.clip_offset(i), pr.clip_offset(i) + c.length
                    col = QColor(stops[i % len(stops)])
                    col.setAlpha(150 if self.selected == ("clip", str(i)) else 90)
                    r = QRectF(self._x(a) + 1, y + 2, max(2.0, self._x(b) - self._x(a) - 2), self.LANE - 6)
                    p.setPen(Qt.NoPen)
                    p.setBrush(col)
                    p.drawRoundedRect(r, 3, 3)
                    p.setPen(QColor(C["text"]))
                    p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft,
                               p.fontMetrics().elidedText(Path(c.path).name, Qt.ElideRight, int(r.width()) - 6))
            elif kind == "crop":
                p.setPen(QPen(QColor(C["acc"]), 1))
                p.drawLine(QPointF(self._x(0), y + self.LANE / 2 - 1), QPointF(self._x(d), y + self.LANE / 2 - 1))
                p.setBrush(QColor(C["acc"]))
                keys = pr.crop.keys if pr.crop.moving else pr.crop.keys[:1]
                for k in keys[:: max(1, len(keys) // 200)]:
                    x = self._x(k[0])
                    p.drawPolygon([QPointF(x, y + 3), QPointF(x + 4, y + self.LANE / 2 - 1), QPointF(x, y + self.LANE - 5),
                                   QPointF(x - 4, y + self.LANE / 2 - 1)])
            else:
                it = next((b for b in (pr.blurs if kind == "blur" else pr.overlays) if b.id == iid), None)
                if it is None:
                    continue
                end = pr.blur_end(it) if kind == "blur" else pr.overlay_end(it)
                col = QColor(C["warn"] if kind == "blur" else C["violet"])
                col.setAlpha(170 if sel else 90)
                r = QRectF(self._x(it.start), y + 2, max(3.0, self._x(end) - self._x(it.start)), self.LANE - 6)
                p.setPen(QPen(QColor(C["text"]), 1.2) if sel else Qt.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(r, 3, 3)
                if kind == "blur" and it.moving and len(it.keys) > 1:
                    p.setPen(QPen(QColor(C["text"]), 1))
                    for k in it.keys[:: max(1, len(it.keys) // 120)]:
                        x = self._x(k[0])
                        p.drawLine(QPointF(x, y + self.LANE - 8), QPointF(x, y + self.LANE - 5))
        x = self._x(self.t)
        p.setPen(QPen(QColor(C["text"]), 1.5))
        p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def _lane_at(self, y: float):
        n = int((y - self.RULER - 4) // self.LANE)
        lanes = self.lanes()
        return lanes[n] if 0 <= n < len(lanes) and y >= self.RULER else None

    def mousePressEvent(self, e):  # noqa: N802
        if self.project is None or e.button() != Qt.LeftButton:
            return
        pos = e.position()
        lane = self._lane_at(pos.y())
        if lane and lane[0] in ("blur", "overlay"):
            it = next((b for b in (self.project.blurs if lane[0] == "blur" else self.project.overlays)
                       if b.id == lane[1]), None)
            if it is not None:
                end = self.project.blur_end(it) if lane[0] == "blur" else self.project.overlay_end(it)
                xa, xb = self._x(it.start), self._x(end)
                if abs(pos.x() - xa) < 6 or abs(pos.x() - xb) < 6:
                    self._drag = ("edge", lane[0], lane[1], "a" if abs(pos.x() - xa) < abs(pos.x() - xb) else "b")
                    self.selected = (lane[0], lane[1])
                    self.picked.emit(lane[0], lane[1])
                    return
                if xa <= pos.x() <= xb:
                    self.selected = (lane[0], lane[1])
                    self.picked.emit(lane[0], lane[1])
        elif lane and lane[0] == "clips":
            t = self._t(pos.x())
            i, _ = self.project.clip_at(t)
            self.selected = ("clip", str(i))
            self.picked.emit("clip", str(i))
        self._drag = ("seek",)
        self.seek.emit(self._t(pos.x()))

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        if self._drag is None:
            lane = self._lane_at(pos.y())
            cur = Qt.PointingHandCursor
            if lane and lane[0] in ("blur", "overlay") and self.project is not None:
                it = next((b for b in (self.project.blurs if lane[0] == "blur" else self.project.overlays)
                           if b.id == lane[1]), None)
                if it is not None:
                    end = self.project.blur_end(it) if lane[0] == "blur" else self.project.overlay_end(it)
                    if min(abs(pos.x() - self._x(it.start)), abs(pos.x() - self._x(end))) < 6:
                        cur = Qt.SizeHorCursor
            self.setCursor(cur)
            return
        if self._drag[0] == "seek":
            self.seek.emit(self._t(pos.x()))
        else:
            _e, kind, iid, side = self._drag
            it = next((b for b in (self.project.blurs if kind == "blur" else self.project.overlays) if b.id == iid), None)
            if it is None:
                return
            end = self.project.blur_end(it) if kind == "blur" else self.project.overlay_end(it)
            t = self._t(pos.x())
            a, b = (min(t, end - 0.1), end) if side == "a" else (it.start, max(t, it.start + 0.1))
            self.rangeChanged.emit(kind, iid, a, b)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag and self._drag[0] == "edge":
            self.committed.emit()
        self._drag = None


# ---- live preview ------------------------------------------------------------------------------------------------------
class PreviewPlayer:
    """Plays the edit in the canvas: ffmpeg decodes the clips at the preview size in a thread, frames are shown on
    time (late ones skipped), and the sound plays alongside from the clip files."""

    def __init__(self, canvas: FrameCanvas, audio_player, on_time, on_end):
        self.canvas = canvas
        self.audio = audio_player
        self.on_time = on_time
        self.on_end = on_end
        self.q: queue.Queue = queue.Queue(maxsize=24)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.timer = QTimer(interval=10, timeout=self._tick)
        self.playing = False
        self.t0 = 0.0
        self.wall0 = 0.0
        self.size = (640, 360)
        self.clip_i = -1

    def start(self, p: Project, t: float, size: tuple[int, int]) -> None:
        self.stop()
        if not p.clips or t >= p.duration - 0.05:
            t = 0.0
        self.p = p
        self.size = size
        self.t0 = t
        self._stop.clear()
        self.q = queue.Queue(maxsize=24)
        self._thread = threading.Thread(target=self._decode, args=(p.copy(), t, size), daemon=True)
        self._thread.start()
        self.playing = True
        self.wall0 = 0.0
        self.clip_i = -1
        self.timer.start()

    def stop(self) -> None:
        self.playing = False
        self.timer.stop()
        self._stop.set()
        if self.audio is not None:
            try:
                self.audio.pause()
            except Exception:
                pass

    def _decode(self, p: Project, t: float, size) -> None:
        W, H = size
        _w, _h, fps = p.canvas()
        fps = min(fps, 30.0)
        n = W * H * 3
        i, src_t = p.clip_at(t)
        while i < len(p.clips) and not self._stop.is_set():
            c = p.clips[i]
            off = p.clip_offset(i)
            proc = ff.popen(R.decode_cmd(p, i, src_t, W, H, fps, duration=c.out - src_t),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            k = 0
            try:
                while not self._stop.is_set():
                    buf = proc.stdout.read(n)
                    if len(buf) < n:
                        break
                    ft = off + (src_t - c.start) + k / fps
                    k += 1
                    frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
                    while not self._stop.is_set():
                        try:
                            self.q.put((i, ft, frame), timeout=0.1)
                            break
                        except queue.Full:
                            continue
            finally:
                proc.kill()
                proc.wait()
            i += 1
            if i < len(p.clips):
                src_t = p.clips[i].start
        if not self._stop.is_set():
            self.q.put(None)

    def _tick(self) -> None:
        if not self.playing:
            return
        now = time.monotonic()
        latest = None
        while True:
            try:
                item = self.q.queue[0] if self.q.queue else None
            except IndexError:
                item = None
            if item is None:
                if self.q.queue and self.q.queue[0] is None:
                    self.stop()
                    self.on_end()
                    return
                break
            i, ft, frame = item
            if self.wall0 == 0.0:
                self.wall0 = now - (ft - self.t0)
            due = self.wall0 + (ft - self.t0)
            if due > now:
                break
            latest = self.q.get_nowait()
        if latest is not None:
            i, ft, frame = latest
            if i != self.clip_i:
                self.clip_i = i
                self._audio_for(i, ft)
            self.canvas.set_frame(frame, ft)
            self.on_time(ft)

    def _audio_for(self, i: int, t: float) -> None:
        c = self.p.clips[i]
        if self.audio is None or c.mute or not c.has_audio:
            if self.audio is not None:
                self.audio.pause()
            return
        src = c.start + (t - self.p.clip_offset(i))
        try:
            self.audio.load(c.path, play=True, start=src)
        except Exception:
            pass
