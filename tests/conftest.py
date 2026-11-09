"""Leave pytest without Python tearing down Qt objects at exit: on Windows that can crash after every test has
passed (the same reason the app itself exits with nirvash.app._quick_exit)."""
import os
import sys

_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _status["code"] = int(exitstatus)


def pytest_unconfigure(config):
    if "PySide6.QtWidgets" in sys.modules and sys.platform == "win32":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_status["code"])
