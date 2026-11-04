"""Download FFmpeg (shared build), libmpv and fpcalc into ./bin for the Windows build."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
TMP = Path(__file__).resolve().parents[1] / "build" / "tmp"


def get(url: str, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nirvash-build"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {tok}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def release_assets(repo: str, tag: str = "latest") -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/releases/" + ("latest" if tag == "latest" else f"tags/{tag}")
    return json.loads(get(url))["assets"]


def ffmpeg() -> None:
    assets = release_assets("BtbN/FFmpeg-Builds", "latest")
    best = None
    for a in assets:
        m = re.fullmatch(r"ffmpeg-n(\d+)\.(\d+)-latest-win64-gpl-shared-\1\.\2\.zip", a["name"])
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            if best is None or v > best[0]:
                best = (v, a)
    if best is None:
        best = (None, next(a for a in assets if a["name"] == "ffmpeg-master-latest-win64-gpl-shared.zip"))
    a = best[1]
    print("FFmpeg:", a["name"])
    z = zipfile.ZipFile(io.BytesIO(get(a["browser_download_url"])))
    for n in z.namelist():
        if "/bin/" in n and not n.endswith("/") and not n.endswith("ffplay.exe"):
            (BIN / Path(n).name).write_bytes(z.read(n))
    lic = [n for n in z.namelist() if n.endswith("LICENSE.txt")]
    if lic:
        (BIN / "FFMPEG-LICENSE.txt").write_bytes(z.read(lic[0]))


def mpv() -> None:
    assets = release_assets("shinchiro/mpv-winbuild-cmake", "latest")
    a = next(a for a in assets if re.fullmatch(r"mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z", a["name"]))
    print("libmpv:", a["name"])
    TMP.mkdir(parents=True, exist_ok=True)
    arc = TMP / a["name"]
    arc.write_bytes(get(a["browser_download_url"]))
    out = TMP / "mpv"
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(["7z", "x", "-y", f"-o{out}", str(arc)], check=True, stdout=subprocess.DEVNULL)
    dll = next(out.rglob("libmpv-2.dll"))
    shutil.copy(dll, BIN / "libmpv-2.dll")


def fpcalc() -> None:
    url = "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-windows-x86_64.zip"
    print("fpcalc: 1.5.1")
    z = zipfile.ZipFile(io.BytesIO(get(url)))
    n = next(n for n in z.namelist() if n.endswith("fpcalc.exe"))
    (BIN / "fpcalc.exe").write_bytes(z.read(n))


if __name__ == "__main__":
    BIN.mkdir(exist_ok=True)
    ffmpeg()
    mpv()
    fpcalc()
    for p in sorted(BIN.iterdir()):
        print(f"  {p.name:40s} {p.stat().st_size / 1e6:8.1f} MB")
    sys.exit(0)
