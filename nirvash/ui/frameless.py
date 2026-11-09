"""Our own title bar on Windows while keeping what the system frame gives: dragging, double-click to maximise,
Snap (drag to an edge, Win + arrows), resizing from every edge, the shadow and rounded corners.

The window is Qt-frameless, then gets its resizable caption styles back; WM_NCCALCSIZE makes the whole window
client area, and WM_NCHITTEST tells Windows where the edges and the draggable title strip are."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QWidget

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 10, 11, 12, 13, 14, 15, 16, 17
GWL_STYLE = -16
WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00020000, 0x00010000, 0x00080000
SWP_FRAMECHANGED, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x20, 0x2, 0x1, 0x4, 0x10

ENABLED = sys.platform == "win32"


def enable(win: QWidget) -> bool:
    """Make the window frameless with the native behaviour kept. Returns False where that isn't possible."""
    if not ENABLED:
        return False
    import os
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return False
    win.setWindowFlag(Qt.FramelessWindowHint, True)
    return True


def after_show(win: QWidget) -> None:
    """Give the window its resizable caption styles back (call once it has a native handle)."""
    if not ENABLED or not (win.windowFlags() & Qt.FramelessWindowHint):
        return
    try:
        from ctypes import wintypes
        hwnd = int(win.winId())
        u = ctypes.windll.user32
        get = u.GetWindowLongPtrW if hasattr(u, "GetWindowLongPtrW") else u.GetWindowLongW
        put = u.SetWindowLongPtrW if hasattr(u, "SetWindowLongPtrW") else u.SetWindowLongW
        get.restype = ctypes.c_ssize_t
        get.argtypes = [wintypes.HWND, ctypes.c_int]
        put.restype = ctypes.c_ssize_t
        put.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        style = get(hwnd, GWL_STYLE)
        put(hwnd, GWL_STYLE, style | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]

        try:
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(0, 0, 0, 1)))
        except Exception:
            pass
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def _frame_thickness(hwnd: int) -> tuple[int, int]:
    u = ctypes.windll.user32
    try:
        dpi = u.GetDpiForWindow(hwnd)
        sm = lambda i: u.GetSystemMetricsForDpi(i, dpi)  # noqa: E731
    except Exception:
        sm = u.GetSystemMetrics
    pad = sm(92)                      # SM_CXPADDEDBORDER
    return sm(32) + pad, sm(33) + pad  # SM_CXFRAME, SM_CYFRAME


def native_event(win, event_type, message, title_bar: QWidget | None):
    """Handle the two messages; returns (True, result) when handled, else None."""
    if not ENABLED or event_type != b"windows_generic_MSG" or not (win.windowFlags() & Qt.FramelessWindowHint):
        return None
    from ctypes import wintypes
    msg = wintypes.MSG.from_address(int(message))
    if msg.message == WM_NCCALCSIZE and msg.wParam:
        if win.isMaximized() and not win.isFullScreen():
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            r = RECT.from_address(msg.lParam)          # NCCALCSIZE_PARAMS.rgrc[0] comes first
            fx, fy = _frame_thickness(int(msg.hWnd))
            r.left += fx
            r.top += fy
            r.right -= fx
            r.bottom -= fy
        return True, 0
    if msg.message == WM_NCHITTEST:
        u = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        wr = RECT()
        u.GetWindowRect(msg.hWnd, ctypes.byref(wr))
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        lx, ly = x - wr.left, y - wr.top
        w, h = wr.right - wr.left, wr.bottom - wr.top
        dpr = win.devicePixelRatioF() or 1.0
        b = int(6 * dpr)
        if not win.isMaximized() and not win.isFullScreen():
            left, right, top, bottom = lx < b, lx >= w - b, ly < b, ly >= h - b
            if top and left:
                return True, HTTOPLEFT
            if top and right:
                return True, HTTOPRIGHT
            if bottom and left:
                return True, HTBOTTOMLEFT
            if bottom and right:
                return True, HTBOTTOMRIGHT
            if left:
                return True, HTLEFT
            if right:
                return True, HTRIGHT
            if top:
                return True, HTTOP
            if bottom:
                return True, HTBOTTOM
        if title_bar is not None and title_bar.isVisible():
            p = QPoint(int(lx / dpr), int(ly / dpr))
            tp = title_bar.mapFrom(win, p)
            if title_bar.rect().contains(tp):
                child = title_bar.childAt(tp)
                while child is not None and child is not title_bar:
                    if isinstance(child, QAbstractButton) or child.property("interactive"):
                        return True, HTCLIENT
                    child = child.parentWidget()
                return True, HTCAPTION
        return True, HTCLIENT
    return None
