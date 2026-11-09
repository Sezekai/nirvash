"""Which graphics card the AI models run on, and how busy it is.

DirectML runs on whichever adapter it's given; by default that's adapter 0, which on laptops is often the built-in
AMD/Intel GPU. We list the adapters (DXGI) and pick the NVIDIA one when there is one, so the RTX 3060 does the work.
Load and memory come from nvidia-smi, which ships with the NVIDIA driver."""
from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass

from . import log as _log

log = _log.get("gpu")


@dataclass
class Adapter:
    index: int
    name: str
    vendor: int
    memory_gb: float
    software: bool


_adapters: list[Adapter] | None = None
_lock = threading.Lock()


def adapters() -> list[Adapter]:
    """Graphics adapters in DXGI order (the order DirectML's device_id uses)."""
    global _adapters
    with _lock:
        if _adapters is None:
            try:
                _adapters = _enum_dxgi() if sys.platform == "win32" else []
            except Exception as e:  # pragma: no cover - depends on the machine
                log.warning("couldn't list graphics adapters: %s", e)
                _adapters = []
        return list(_adapters)


def _enum_dxgi() -> list[Adapter]:  # pragma: no cover - Windows only
    from ctypes import POINTER, byref, c_size_t, c_uint, c_void_p, c_wchar, wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_uint), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class DESC1(ctypes.Structure):
        _fields_ = [("Description", c_wchar * 128), ("VendorId", c_uint), ("DeviceId", c_uint), ("SubSysId", c_uint),
                    ("Revision", c_uint), ("DedicatedVideoMemory", c_size_t), ("DedicatedSystemMemory", c_size_t),
                    ("SharedSystemMemory", c_size_t), ("LuidLow", wintypes.DWORD), ("LuidHigh", wintypes.LONG),
                    ("Flags", c_uint)]

    iid = GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory = c_void_p()
    hr = ctypes.windll.dxgi.CreateDXGIFactory1(byref(iid), byref(factory))
    if hr != 0 or not factory:
        return []

    def method(obj, index, restype, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])

    out: list[Adapter] = []
    try:
        enum = method(factory, 12, ctypes.c_long, c_uint, POINTER(c_void_p))       # IDXGIFactory1::EnumAdapters1
        i = 0
        while i < 16:
            ad = c_void_p()
            if enum(factory, i, byref(ad)) != 0:
                break
            d = DESC1()
            method(ad, 10, ctypes.c_long, POINTER(DESC1))(ad, byref(d))              # IDXGIAdapter1::GetDesc1
            out.append(Adapter(i, d.Description, d.VendorId, d.DedicatedVideoMemory / 1024 ** 3, bool(d.Flags & 2)))
            method(ad, 2, c_uint)(ad)                                                # Release
            i += 1
    finally:
        method(factory, 2, c_uint)(factory)
    return out


NVIDIA = 0x10DE


def best_adapter() -> Adapter | None:
    """The NVIDIA card if there is one, else the hardware adapter with the most video memory."""
    hw = [a for a in adapters() if not a.software]
    if not hw:
        return None
    nv = [a for a in hw if a.vendor == NVIDIA]
    return (nv or sorted(hw, key=lambda a: -a.memory_gb))[0]


# What the models ended up running on (set by analysis.runtime).
in_use = {"provider": "", "device": ""}


def describe_device() -> tuple[str, bool]:
    """('RTX 3060 Laptop GPU', True) or ('On the CPU (slow)', False)."""
    prov = in_use.get("provider") or ""
    if prov in ("DmlExecutionProvider", "CUDAExecutionProvider"):
        name = in_use.get("device") or "Graphics card"
        return name.replace("NVIDIA GeForce ", ""), True
    if prov == "CPUExecutionProvider":
        return "On the CPU (slow)", False
    return "", False


def nvidia_stats() -> dict | None:
    """{'util': 87, 'used': 3.1, 'total': 6.0} from nvidia-smi, or None."""
    from ..media import ff
    try:
        p = ff.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits"], timeout=4)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    try:
        u, used, tot = [float(x) for x in p.stdout.decode().strip().splitlines()[0].split(",")]
    except Exception:
        return None
    return {"util": int(u), "used": used / 1024, "total": tot / 1024}
