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


# ---- the picture ----------------------------------------------------------------------------------------------------
class FrameCanvas(QWidget):
    """Shows the frame at the playhead, two ways:

    - "result": the finished frame (cropped, zoomed, blurred), with overlays and blur boxes to drag on it;
    - "whole": the whole video picture, with the crop box to move and resize (it can go past the edges to zoom out).

    boxChanged(kind, id, box, space) while dragging, box in `space` ("output" or "video" fractions);
    boxCommitted() on release; boxPicked(kind, id) on click; boxDrawn(box, space) for a new blur box."""

    boxChanged = Signal(str, str, object, str)
    boxCommitted = Signal()
    boxPicked = Signal(str, str)
    boxDrawn = Signal(object, str)

    H = 6   # handle half size

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.project: Project | None = None
        self.t = 0.0
        self.base: np.ndarray | None = None      # the video picture (colour etc. done), preview size
        self.image: QImage | None = None
        self.view = "result"
        self.tool = "clip"
        self.selected: tuple[str, str] | None = None
        self.ops: R.FrameOps | None = None
        self._drag = None
        self._temp_new = None
        self._overlay_pm: dict = {}
        self.message = ""

    @property
    def space(self) -> str:
        """The coordinates of what's on screen."""
        return "video" if self.view == "whole" else "output"

    # ---- content ---------------------------------------------------------------------------
    def set_project(self, p: Project | None) -> None:
        self.project = p
        self.refresh_ops()

    def set_view(self, view: str) -> None:
        if view != self.view:
            self.view = view
            self.recompose()

    def refresh_ops(self) -> None:
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
            if self.view == "whole":
                self.ops.blurs(img, self.t, "video")
            else:
                img = self.ops.render(img, self.t, overlays=False)
            self.message = ""
        except Exception as e:  # noqa: BLE001 - a bad box mustn't take the editor down
            self.message = f"Preview problem: {e}"
        self.image = to_qimage(img)
        self.update()

    # ---- geometry ------------------------------------------------------------------------------
    def pic_rect(self) -> QRectF:
        """Where the picture is drawn. Cropping leaves room around it, so the box can go past the edges."""
        if self.image is None:
            return QRectF(self.rect())
        iw, ih = self.image.width(), self.image.height()
        pad = 0.16 if (self.view == "whole" and self.tool == "crop") else 0.0
        aw, ah = self.width() * (1 - 2 * pad), self.height() * (1 - 2 * pad)
        k = min(aw / iw, ah / ih)
        w, h = iw * k, ih * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_px(self, box) -> QRectF:
        r = self.pic_rect()
        x, y, w, h = box
        return QRectF(r.x() + x * r.width(), r.y() + y * r.height(), w * r.width(), h * r.height())

    def _to_norm(self, pt: QPointF) -> tuple[float, float]:
        r = self.pic_rect()
        return (pt.x() - r.x()) / max(1, r.width()), (pt.y() - r.y()) / max(1, r.height())

    def _convert(self, box, frm: str, to: str):
        if frm == to or box is None:
            return box
        p = self.project
        return p.canvas_to_output(box, self.t) if to == "output" else p.output_to_canvas(box, self.t)

    def overlay_box(self, o) -> tuple[float, float, float, float]:
        """An overlay's box in output fractions (its height follows the picture's shape)."""
        p = self.project
        src = self.ops.sources.get(o.path) if self.ops else None
        aspect = src.aspect() if src else 1.0
        OW, OH = p.output_size()
        return o.x, o.y, o.w, o.w * OW * aspect / OH

    def items(self) -> list[tuple[str, str, tuple]]:
        """What can be dragged now (in screen space), topmost last."""
        p = self.project
        out = []
        if p is None:
            return out
        sp = self.space
        if self.tool == "crop" and p.crop.enabled and self.view == "whole":
            b = p.crop_box(self.t)
            if b:
                out.append(("crop", "crop", b))
        if self.tool == "blur":
            for b in p.blurs:
                if b.start <= self.t <= p.blur_end(b):
                    box = p.blur_box(b, self.t, sp)
                    if box is not None:
                        out.append(("blur", b.id, box))
        if self.tool == "overlay":
            for o in p.overlays:
                if o.start <= self.t <= p.overlay_end(o):
                    out.append(("overlay", o.id, self._convert(self.overlay_box(o), "output", sp)))
        return out

    # ---- drawing -----------------------------------------------------------------------------------
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C.get("stage", "#05060a")))
        pr = self.project
        if self.image is None:
            p.setPen(QColor(C["faint"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.message or ("Loading the picture…" if pr and pr.clips else ""))
            return
        r = self.pic_rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(r, self.image)
        if pr is None:
            return
        p.setRenderHint(QPainter.Antialiasing)
        sp = self.space
        if self.view == "whole" and pr.crop.enabled:
            box = pr.crop_box(self.t)
            cr = self._to_px(box)
            path = QPainterPath()
            path.addRect(QRectF(self.rect()))
            path.addRect(cr)
            p.fillPath(path, QColor(0, 0, 0, 150 if self.tool == "crop" else 100))
            p.setPen(QPen(QColor(C["acc"]), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRect(cr)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            for k in (1, 2):
                p.drawLine(QPointF(cr.x() + cr.width() * k / 3, cr.y()), QPointF(cr.x() + cr.width() * k / 3, cr.bottom()))
                p.drawLine(QPointF(cr.x(), cr.y() + cr.height() * k / 3), QPointF(cr.right(), cr.y() + cr.height() * k / 3))
            p.setPen(QPen(QColor(C["sec"]), 1, Qt.DotLine))           # the video's own edges
            p.drawRect(r)
        # overlays, drawn here so they move smoothly while dragged (they're part of the exported picture)
        if self.ops is not None:
            for o in pr.overlays:
                if not (o.start <= self.t <= pr.overlay_end(o)) or o.path not in self.ops.sources:
                    continue
                pm = self._overlay_pixmap(o)
                if pm is not None:
                    p.setOpacity(o.opacity)
                    p.drawPixmap(self._to_px(self._convert(self.overlay_box(o), "output", sp)).toRect(), pm)
                    p.setOpacity(1.0)
        for kind, iid, b in self.items():
            rr = self._to_px(b)
            sel = self.selected == (kind, iid) or kind == "crop"
            col = QColor(C["warn"] if kind == "blur" else C["acc"])
            p.setPen(QPen(col, 2 if sel else 1.2, Qt.SolidLine if sel else Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            if kind == "blur":
                bl = next((x for x in pr.blurs if x.id == iid), None)
                if bl is not None and bl.shape == "ellipse":
                    p.drawEllipse(rr)
                    p.setPen(QPen(col, 1, Qt.DotLine))
            p.drawRect(rr)
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                for hx, hy in self._handles(rr):
                    p.drawRect(QRectF(hx - self.H, hy - self.H, self.H * 2, self.H * 2))
        if self._temp_new:
            p.setPen(QPen(QColor(C["warn"]), 2, Qt.DashLine))
            p.setBrush(QColor(255, 191, 90, 40))
            p.drawRect(self._to_px(self._temp_new))
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
            if self.selected == (kind, iid) or kind == "crop":
                for n, (hx, hy) in enumerate(self._handles(rr)):
                    if abs(pos.x() - hx) <= self.H + 3 and abs(pos.y() - hy) <= self.H + 3:
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
            self._drag = {"kind": "new", "start": (x, y)}
            self._temp_new = (x, y, 0.0, 0.0)
        elif self.selected is not None:
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
        if d["kind"] == "new":                              # drawing a new box: shown as it's drawn
            x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
            self._temp_new = (min(sx, x), min(sy, y), abs(x - sx), abs(y - sy))
            self.update()
            return
        bx, by, bw, bh = d["box"]
        dx, dy = x - sx, y - sy
        h = d["handle"]
        lo, hi = (-1.5, 2.5) if d["kind"] == "crop" else (0.0, 1.0)     # the crop can reach past the edges
        if h is None:
            nx, ny = bx + dx, by + dy
            if d["kind"] != "crop":
                nx, ny = min(1 - bw, max(0.0, nx)), min(1 - bh, max(0.0, ny))
            nb = (nx, ny, bw, bh)
        else:
            l, t, r, b = bx, by, bx + bw, by + bh
            if h in (0, 3, 5):
                l = min(r - 0.02, max(lo, bx + dx))
            if h in (2, 4, 7):
                r = max(l + 0.02, min(hi, bx + bw + dx))
            if h in (0, 1, 2):
                t = min(b - 0.02, max(lo, by + dy))
            if h in (5, 6, 7):
                b = max(t + 0.02, min(hi, by + bh + dy))
            nb = self._keep_aspect(d, (l, t, r - l, b - t), h)
        self.boxChanged.emit(d["kind"], d["id"], nb, self.space)

    def _keep_aspect(self, d, nb, h):
        """The crop keeps the finished video's shape (unless that's turned off); overlays keep their picture's."""
        p = self.project
        x, y, w, hh = nb
        if self.image is None:
            return nb
        pw, ph = self.image.width(), self.image.height()
        if d["kind"] == "crop":
            if not p.crop.keep_shape:
                return nb
            a = p.aspect_value()
        elif d["kind"] == "overlay":
            ob = d["box"]
            a = (ob[2] * pw) / max(1e-6, ob[3] * ph)
        else:
            return nb
        if h in (1, 6):                       # top/bottom edge: width follows height
            w2 = hh * ph * a / pw
            cx = x + w / 2
            x, w = cx - w2 / 2, w2
        else:
            h2 = w * pw / a / ph
            if h in (0, 1, 2):
                y = y + hh - h2
            hh = h2
        return x, y, w, hh

    def mouseReleaseEvent(self, e):  # noqa: N802
        d = self._drag
        self._drag = None
        if d is None:
            return
        if d["kind"] == "new":
            b = self._temp_new
            self._temp_new = None
            if b and b[2] > 0.01 and b[3] > 0.01:
                self.boxDrawn.emit(b, self.space)
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
    marked = Signal(object)          # (start, end) of the marked part, or None

    LANE = 20
    RULER = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.t = 0.0
        self.selected: tuple[str, str] | None = None
        self.mark: tuple[float, float] | None = None
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumHeight(self.RULER + self.LANE * 2 + 8)
        self.setToolTip("Click to move the playhead. Drag along the times at the top to mark a part.")

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
        if p.sound.sections:
            out.append(("sound", "", "Sound"))
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
        if self.mark is not None:
            a, b = self.mark
            band = QColor(C["acc"])
            band.setAlpha(40)
            p.fillRect(QRectF(self._x(a), 0, max(2.0, self._x(b) - self._x(a)), self.height()), band)
            p.setPen(QPen(QColor(C["acc"]), 1.5))
            p.drawLine(QPointF(self._x(a), 0), QPointF(self._x(b), 0))
            p.drawLine(QPointF(self._x(a), 0), QPointF(self._x(a), self.RULER))
            p.drawLine(QPointF(self._x(b), 0), QPointF(self._x(b), self.RULER))
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
                mid = y + self.LANE / 2 - 1
                p.setPen(QPen(QColor(C["acc"]), 1))
                p.drawLine(QPointF(self._x(0), mid), QPointF(self._x(d), mid))
                keys = pr.crop.keys
                # followed (tracked) stretches as a thick line, keys set by hand as diamonds
                p.setPen(QPen(QColor(C["violet"]), 4))
                for k0, k1 in zip(keys, keys[1:]):
                    if len(k1) > 5 and k1[5] == "l":
                        p.drawLine(QPointF(self._x(k0[0]), mid), QPointF(self._x(k1[0]), mid))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(C["acc"]))
                for k in keys:
                    if len(k) > 5 and k[5] == "l":
                        continue
                    x = self._x(k[0])
                    p.drawPolygon([QPointF(x, y + 3), QPointF(x + 4, mid), QPointF(x, y + self.LANE - 5),
                                   QPointF(x - 4, mid)])
            elif kind == "sound":
                for sec in pr.sound.sections:
                    g = sec.get("gain_db", 0)
                    col = QColor(C["ok"] if g >= 0 else C["blue"])
                    sel_s = self.selected == ("sound", sec.get("id"))
                    col.setAlpha(170 if sel_s else 90)
                    r = QRectF(self._x(sec["start"]), y + 2, max(3.0, self._x(sec["end"]) - self._x(sec["start"])),
                               self.LANE - 6)
                    p.setPen(QPen(QColor(C["text"]), 1.2) if sel_s else Qt.NoPen)
                    p.setBrush(col)
                    p.drawRoundedRect(r, 3, 3)
                    p.setPen(QColor(C["text"]))
                    p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft, f"{g:+.0f} dB")
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

    def _items(self, kind: str):
        p = self.project
        if kind == "blur":
            return [(b.id, b.start, p.blur_end(b)) for b in p.blurs]
        if kind == "overlay":
            return [(o.id, o.start, p.overlay_end(o)) for o in p.overlays]
        if kind == "sound":
            return [(s.get("id"), s["start"], s["end"]) for s in p.sound.sections]
        return []

    def mousePressEvent(self, e):  # noqa: N802
        if self.project is None or e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if pos.y() < self.RULER:                            # along the times: mark a part
            t = self._t(pos.x())
            self._drag = ("mark", t)
            self.mark = None
            self.marked.emit(None)
            self.seek.emit(t)
            self.update()
            return
        lane = self._lane_at(pos.y())
        if lane and lane[0] in ("blur", "overlay", "sound"):
            for iid, a, b in self._items(lane[0]):
                if lane[0] != "sound" and iid != lane[1]:
                    continue
                xa, xb = self._x(a), self._x(b)
                if abs(pos.x() - xa) < 6 or abs(pos.x() - xb) < 6:
                    self._drag = ("edge", lane[0], iid, "a" if abs(pos.x() - xa) < abs(pos.x() - xb) else "b")
                    self.selected = (lane[0], iid)
                    self.picked.emit(lane[0], iid)
                    self.update()
                    return
                if xa <= pos.x() <= xb:
                    self.selected = (lane[0], iid)
                    self.picked.emit(lane[0], iid)
                    break
        elif lane and lane[0] == "clips":
            i, _ = self.project.clip_at(self._t(pos.x()))
            self.selected = ("clip", str(i))
            self.picked.emit("clip", str(i))
        self._drag = ("seek",)
        self.seek.emit(self._t(pos.x()))

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        if self._drag is None:
            cur = Qt.PointingHandCursor
            if pos.y() < self.RULER:
                cur = Qt.IBeamCursor
            else:
                lane = self._lane_at(pos.y())
                if lane and lane[0] in ("blur", "overlay", "sound") and self.project is not None:
                    for iid, a, b in self._items(lane[0]):
                        if (lane[0] == "sound" or iid == lane[1]) and \
                                min(abs(pos.x() - self._x(a)), abs(pos.x() - self._x(b))) < 6:
                            cur = Qt.SizeHorCursor
            self.setCursor(cur)
            return
        if self._drag[0] == "seek":
            self.seek.emit(self._t(pos.x()))
        elif self._drag[0] == "mark":
            t0, t1 = self._drag[1], self._t(pos.x())
            if abs(t1 - t0) > 0.05:
                self.mark = (min(t0, t1), max(t0, t1))
                self.marked.emit(self.mark)
                self.update()
        else:
            _e, kind, iid, side = self._drag
            it = next(((a, b) for i, a, b in self._items(kind) if i == iid), None)
            if it is None:
                return
            a0, b0 = it
            t = self._t(pos.x())
            a, b = (min(t, b0 - 0.1), b0) if side == "a" else (a0, max(t, a0 + 0.1))
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
            # the edit's volume changes, in this clip file's own time
            vol = R.volume_filter(self.p, offset=self.p.clip_offset(i) - c.start)
            self.audio.set_audio_filter(vol)
        except Exception:
            pass
