"""Entry point."""
from __future__ import annotations

import os
import sys
import traceback


def selftest(out: str) -> int:
    """Check the bundled tools and libraries load (used by the build)."""
    import json
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    res: dict[str, str] = {}
    # If a check ever hangs, say where (the build waits on this)
    import faulthandler
    hang = open(out + ".hang.txt", "w", encoding="utf-8")  # noqa: SIM115
    faulthandler.dump_traceback_later(120, exit=True, file=hang)

    def check(name, fn):
        res[name] = "running"
        try:
            res[name] = "ok: " + str(fn())[:200]
        except Exception as e:  # noqa: BLE001
            res[name] = f"FAIL: {e!r}"[:300]
        with open(out, "w", encoding="utf-8") as fh:  # written after every check, so a hang shows which
            json.dump(res, fh, indent=2)

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
    check("fonts", lambda: f"{len(__import__('nirvash.core.appearance', fromlist=['x']).bundled_fonts())} bundled")
    check("gpu", lambda: [(a.index, a.name, a.software) for a in __import__('nirvash.core.gpu', fromlist=['x']).adapters()] or "none listed")
    check("clip_tokenizer", lambda: __import__("nirvash.analysis.clip_tokenizer", fromlist=["x"]).ClipTokenizer().encode("a photo of a cat"))

    def _tracker():
        import cv2
        from .edit.track import _tracker as make
        t, name = make()
        return f"{cv2.__version__}, {name}"

    check("opencv_tracker", _tracker)

    def _ui():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from .ui import theme
        theme.apply(app)
        from .ui.main_window import MainWindow
        MainWindow._check_models = lambda self: None
        w = MainWindow()
        w.identify.stop()
        return f"{w.library.player.backend} playback"

    check("ui", _ui)
    faulthandler.cancel_dump_traceback_later()
    return 1 if any(v.startswith("FAIL") for v in res.values()) else 0


def _crash_log() -> None:
    """Write native crashes (which skip Python's error handling) to logs/crash.log."""
    from .core import crashlog, paths

    try:
        crashlog.install(paths.ensure(paths.data_dir() / "logs"))
    except Exception:
        pass


SERVER = "Nirvash-single-instance"


def _already_running() -> bool:
    """If Nirvash is already open, ask it to come forward and return True (STANDARDS.md, "One window only")."""
    import getpass

    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(f"{SERVER}-{getpass.getuser()}")
    if not sock.waitForConnected(400):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY: let the open copy come to the front
        except Exception:
            pass
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(400)
    sock.disconnectFromServer()
    return True


def _listen(window) -> object:
    import getpass

    from PySide6.QtNetwork import QLocalServer
    name = f"{SERVER}-{getpass.getuser()}"
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)

    def wake():
        c = server.nextPendingConnection()
        if c is not None:
            c.readyRead.connect(lambda: c.readAll())
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(wake)
    return server


def main() -> int:
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return selftest(sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from . import APP_NAME
    from .core import log as _log
    from .core import safemode
    from .ui import theme

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if _already_running():
        return 0
    log = _log.setup()
    _crash_log()
    safe = safemode.start()
    if safe:
        log.warning("Started in safe mode after %d unclean exits: playback and the graphics card are off.",
                    safemode.unclean_count())
        import os
        os.environ["NIRVASH_NO_PLAYBACK"] = "1"
    try:
        from .jobs import storage
        storage.finish_data_move()
    except Exception as e:
        log.warning("couldn't finish moving the data folder: %s", e)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Sezekai.Nirvash")
        except Exception:
            pass
    app.setStyle("Fusion")
    theme.apply(app)
    from PySide6.QtGui import QIcon

    from .core.paths import asset
    app.setWindowIcon(QIcon(str(asset("nirvash.ico"))))

    def excepthook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        log.error("Unhandled error:\n%s", msg)
        try:
            from .ui import dialogs
            dialogs.info(None, "Something went wrong", f"{v}\n\nThe log has the details (L).")
        except Exception:
            pass

    sys.excepthook = excepthook
    from .ui.main_window import MainWindow
    w = MainWindow()
    server = _listen(w)  # noqa: F841  (kept alive for the life of the app)
    w.show()
    log.info("%s started", APP_NAME)
    code = app.exec()
    log.info("%s closed", APP_NAME)
    return code


def _quick_exit(code: int) -> None:
    """Leave without Python tearing down Qt objects one by one at exit, which can crash on Windows after the
    window has already closed cleanly. Everything is saved by then; logs are flushed first."""
    import logging
    import threading
    # flush in a helper thread so a stuck handler can't keep Nirvash open
    t = threading.Thread(target=logging.shutdown, daemon=True)
    t.start()
    t.join(3)
    for stream in (sys.stdout, sys.stderr):   # both are None in the packaged app (no console)
        try:
            if stream:
                stream.flush()
        except Exception:
            pass
    os._exit(code or 0)


if __name__ == "__main__":
    _quick_exit(main())
