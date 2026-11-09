"""ONNX Runtime sessions: graphics card (DirectML/CUDA) first, processor as fallback."""
from __future__ import annotations

import threading
from pathlib import Path

from ..core import log as _log

log = _log.get("ort")
_lock = threading.Lock()


def available_providers() -> list[str]:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


def make_session(path: Path, device: str = "auto"):
    """Create an InferenceSession. device: auto | gpu | cpu. Returns (session, provider_name)."""
    import onnxruntime as ort

    avail = ort.get_available_providers()
    order: list[str] = []
    if device in ("auto", "gpu"):
        for p in ("CUDAExecutionProvider", "DmlExecutionProvider"):
            if p in avail:
                order.append(p)
    order.append("CPUExecutionProvider")
    last: Exception | None = None
    from ..core import gpu, safemode
    if safemode.active():
        order = ["CPUExecutionProvider"]
    for prov in order:
        try:
            so = ort.SessionOptions()
            so.log_severity_level = 3
            opts: dict = {}
            dev = ""
            if prov == "DmlExecutionProvider":
                so.enable_mem_pattern = False
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                ad = gpu.best_adapter()
                if ad is not None:
                    opts = {"device_id": ad.index}
                    dev = ad.name
            with _lock:
                sess = ort.InferenceSession(str(path), sess_options=so,
                                            providers=[(prov, opts)] if opts else [prov])
            actual = (sess.get_providers() or [prov])[0]
            gpu.in_use.update(provider=actual, device=dev or ("Graphics card" if actual != "CPUExecutionProvider" else ""))
            log.info("%s runs on %s%s", path.name, actual, f" ({dev})" if dev else "")
            return sess, actual
        except Exception as e:  # pragma: no cover - depends on the machine
            last = e
            log.warning("could not load %s on %s: %s", path.name, prov, e)
    raise RuntimeError(f"Could not load {path.name}: {last}")


def pick_output(sess, *names: str) -> str:
    outs = [o.name for o in sess.get_outputs()]
    for n in names:
        if n in outs:
            return n
    return outs[0]
