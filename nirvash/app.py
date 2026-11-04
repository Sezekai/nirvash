"""Entry point."""
from __future__ import annotations

import sys
import traceback


def selftest(out: str) -> int:
    """Check the bundled tools and libraries load (used by the build)."""
    import json
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    res: dict[str, str] = {}

    def check(name, fn):
        try:
            res[name] = "ok: " + str(fn())[:200]
        except Exception as e:  # noqa: BLE001
            res[name] = f"FAIL: {e!r}"[:300]

    from .core.paths import bin_path
    from .media import ff

    check("ffmpeg", lambda: ff.run([ff.ffmpeg(), "-version"]).stdout.decode()[:60])
    check("ffprobe", lambda: ff.run([ff.ffprobe(), "-version"]).stdout.decode()[:60])
    check("fpcalc", lambda: ff.run([bin_path("fpcalc"), "-version"]).stdout.decode().strip())
    check("encoders", lambda: [e for e in ("libx264", "libx265", "h264_nvenc", "libopus", "libmp3lame", "aac")
                               if e not in ff.run([ff.ffmpeg(), "-hide_banner", "-encoders"]).stdout.decode()] or "all present")

    def _mpv():
        from .ui import player
        m = player._load_mpv()
        if m is None:
            raise RuntimeError(player._mpv_error)
        return "loaded"

    check("libmpv", _mpv)
    check("onnxruntime", lambda: __import__("onnxruntime").get_available_providers())
    check("tokenizers", lambda: __import__("tokenizers").__version__)
    check("shazamio", lambda: (__import__("shazamio"), "imported")[1])
    check("send2trash", lambda: (__import__("send2trash"), "imported")[1])
    check("clip_tokenizer", lambda: __import__("nirvash.analysis.clip_tokenizer", fromlist=["x"]).ClipTokenizer().encode("a photo of a cat"))

    def _ui():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from .ui import theme
        theme.check_svg_resource()
        app.setStyleSheet(theme.fixed_stylesheet())
        from .ui.main_window import MainWindow
        MainWindow._check_models = lambda self: None
        w = MainWindow()
        w.identify.stop()
        return f"{w.library.player.backend} playback"

    check("ui", _ui)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)
    return 1 if any(v.startswith("FAIL") for v in res.values()) else 0


def main() -> int:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return selftest(sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import APP_NAME
    from .core import log as _log
    from .ui import theme

    log = _log.setup()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Sezekai.Nirvash")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    theme.apply_font(app)
    theme.check_svg_resource()
    app.setStyleSheet(theme.fixed_stylesheet())
    from PySide6.QtGui import QIcon

    from .core.paths import asset
    app.setWindowIcon(QIcon(str(asset("nirvash.ico"))))

    def excepthook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        log.error("Unhandled error:\n%s", msg)
        try:
            QMessageBox.critical(None, APP_NAME, f"Something went wrong:\n\n{v}\n\nDetails are in the log.")
        except Exception:
            pass

    sys.excepthook = excepthook
    from .ui.main_window import MainWindow
    w = MainWindow()
    w.show()
    log.info("%s started", APP_NAME)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
