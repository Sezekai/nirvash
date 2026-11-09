"""Crash log for native crashes.

On Windows, Python's faulthandler hooks *every* exception (a vectored handler) and treats any with the high bit set
as fatal. Libraries raise and handle such exceptions on purpose (LuaJIT inside libmpv does it all the time), so
faulthandler would dump every thread's stack mid-flight - and crash Python doing it. So on Windows we only hook
exceptions nothing else handled (SetUnhandledExceptionFilter): the process is going down anyway, and Windows still
writes its own report afterwards."""
from __future__ import annotations

import sys
import time

_keep: list = []   # keeps the file and the C callback alive for the life of the process


def install(log_dir) -> None:
    """Crashes go to logs/crash-<date>.txt, one file per day; empty ones from earlier days are tidied away."""
    today = log_dir / f"crash-{time.strftime('%Y-%m-%d')}.txt"
    for f in log_dir.glob("crash-*.txt"):
        try:
            if f != today and f.stat().st_size == 0:
                f.unlink()
        except OSError:
            pass
    try:
        fh = open(today, "a", encoding="utf-8")
    except Exception:
        return
    _keep.append(fh)
    if sys.platform != "win32":
        import faulthandler
        faulthandler.enable(file=fh, all_threads=True)
        return
    try:
        _install_windows(fh)
    except Exception as e:  # never stop the app over crash logging
        fh.write(f"(crash logging unavailable: {e})\n")
        fh.flush()


def _install_windows(fh) -> None:
    import ctypes
    import faulthandler
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class EXCEPTION_RECORD(ctypes.Structure):
        _fields_ = [("ExceptionCode", wintypes.DWORD), ("ExceptionFlags", wintypes.DWORD),
                    ("ExceptionRecord", ctypes.c_void_p), ("ExceptionAddress", ctypes.c_void_p),
                    ("NumberParameters", wintypes.DWORD), ("ExceptionInformation", ctypes.c_void_p * 15)]

    class EXCEPTION_POINTERS(ctypes.Structure):
        _fields_ = [("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)), ("ContextRecord", ctypes.c_void_p)]

    FILTER = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.POINTER(EXCEPTION_POINTERS))
    k32.GetModuleHandleExW.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HMODULE)]
    k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    k32.SetUnhandledExceptionFilter.argtypes = [ctypes.c_void_p]
    k32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p

    def on_crash(ptrs):
        try:
            rec = ptrs.contents.ExceptionRecord.contents
            addr = rec.ExceptionAddress or 0
            mod, off = "?", 0
            h = wintypes.HMODULE()
            if k32.GetModuleHandleExW(0x4 | 0x2, ctypes.c_void_p(addr), ctypes.byref(h)):
                buf = ctypes.create_unicode_buffer(520)
                k32.GetModuleFileNameW(h, buf, 520)
                mod, off = buf.value, addr - (h.value or 0)
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} Nirvash crashed ===\n"
                     f"Fatal exception 0x{rec.ExceptionCode:08x} in {mod} +0x{off:x}\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
        except Exception:
            pass
        return 0  # EXCEPTION_CONTINUE_SEARCH: let Windows finish (and write its report)

    cb = FILTER(on_crash)
    _keep.append(cb)
    k32.SetUnhandledExceptionFilter(ctypes.cast(cb, ctypes.c_void_p))
