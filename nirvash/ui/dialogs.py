"""In-app dialogs (no Windows pop-ups): a title, a sentence, optional content, and buttons that say what they do.
Enter confirms, Esc cancels. The window behind is dimmed while one is open."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from .theme import C, icon


class _Dim(QWidget):
    """Darkens the main window while a dialog is open."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        if obj is self.parent() and ev.type() == QEvent.Resize:
            self.setGeometry(self.parent().rect())
        return False

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))


def _host(parent: QWidget | None) -> QWidget | None:
    w = parent.window() if parent is not None else QApplication.activeWindow()
    return w


class Dialog(QDialog):
    """Base for every dialog: frameless panel, title, body, buttons on the right."""

    def __init__(self, parent: QWidget | None, title: str, text: str = "", width: int = 460):
        super().__init__(_host(parent))
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame()
        self.frame.setObjectName("dialog")
        outer.addWidget(self.frame)
        self.lay = QVBoxLayout(self.frame)
        self.lay.setContentsMargins(18, 16, 18, 14)
        self.lay.setSpacing(10)
        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("h1")
        self.title_lbl.setWordWrap(True)
        self.lay.addWidget(self.title_lbl)
        self.text_lbl = QLabel(text)
        self.text_lbl.setObjectName("muted")
        self.text_lbl.setWordWrap(True)
        self.text_lbl.setTextFormat(Qt.PlainText)
        self.text_lbl.setVisible(bool(text))
        self.lay.addWidget(self.text_lbl)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.lay.addLayout(self.body)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(8)
        self.btns.addStretch(1)
        self.lay.addSpacing(4)
        self.lay.addLayout(self.btns)
        self.setMinimumWidth(width)
        self.setMaximumWidth(max(width, 720))
        self._dim: _Dim | None = None
        self._drag = None

    def add_button(self, text: str, role: str = "default", icon_name: str | None = None) -> QPushButton:
        """role: primary | default | ghost | danger | cancel"""
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        if icon_name:
            b.setIcon(icon(icon_name, C["on_acc"] if role == "primary" else C["text"], 15))
        b.setObjectName({"primary": "primary", "danger": "dangerSolid", "ghost": "ghost", "cancel": "ghost"}.get(role, ""))
        if role == "cancel":
            b.clicked.connect(self.reject)
        elif role in ("primary", "danger"):
            b.clicked.connect(self.accept)
            b.setDefault(True)
            b.setAutoDefault(True)
        else:
            b.setAutoDefault(False)
        self.btns.addWidget(b)
        return b

    def exec(self) -> int:
        import os
        auto = os.environ.get("NIRVASH_AUTO_DIALOGS")       # tests: answer without showing anything
        if auto in ("accept", "reject"):
            if auto == "accept":
                for i in range(self.btns.count()):
                    w = self.btns.itemAt(i).widget()
                    if isinstance(w, QPushButton) and w.isDefault():
                        w.click()
                        return 1
                return 1
            return 0
        host = self.parentWidget()
        if host is not None and host.isVisible():
            self._dim = _Dim(host)
        self.adjustSize()
        if host is not None:
            g = host.geometry()
            self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2 - 40)
        try:
            return super().exec()
        finally:
            if self._dim is not None:
                self._dim.hide()
                self._dim.deleteLater()
                self._dim = None

    # drag the dialog by its title
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 48:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


def ask(parent, title: str, text: str, ok: str, cancel: str = "Cancel", danger: bool = False) -> bool:
    """A question with a button that says what it does ("Rename 34 files")."""
    d = Dialog(parent, title, text)
    d.add_button(cancel, "cancel")
    d.add_button(ok, "danger" if danger else "primary")
    return bool(d.exec())


def info(parent, title: str, text: str, ok: str = "Close") -> None:
    d = Dialog(parent, title, text)
    d.add_button(ok, "primary")
    d.exec()


def ask_text(parent, title: str, label: str, text: str = "", ok: str = "Save", placeholder: str = "") -> str | None:
    d = Dialog(parent, title, label)
    e = QLineEdit(text)
    e.setPlaceholderText(placeholder)
    e.selectAll()
    d.body.addWidget(e)
    d.add_button("Cancel", "cancel")
    b = d.add_button(ok, "primary")
    b.setEnabled(bool(text.strip()))
    e.textChanged.connect(lambda t: b.setEnabled(bool(t.strip())))
    e.setFocus()
    return e.text().strip() if d.exec() and e.text().strip() else None


def choose(parent, title: str, text: str, options: list[tuple[str, str]], cancel: str = "Not now") -> str | None:
    """Several actions; returns the key of the one picked. options: [(key, button text)], the last is primary."""
    d = Dialog(parent, title, text)
    picked: list[str] = []
    d.add_button(cancel, "cancel")
    for i, (key, txt) in enumerate(options):
        b = d.add_button(txt, "primary" if i == len(options) - 1 else "default")
        b.clicked.connect(lambda _=False, k=key: (picked.append(k), d.accept()))
    return picked[-1] if d.exec() and picked else None


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w
