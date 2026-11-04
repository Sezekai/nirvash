# PyInstaller spec: builds dist/App/Nirvash.exe (one folder, so it starts fast).
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent

datas = [(str(ROOT / "assets"), "assets")]
binaries = [(str(p), "bin") for p in (ROOT / "bin").glob("*") if p.is_file()]
hidden = ["mpv"]
for pkg in ("onnxruntime", "tokenizers", "shazamio", "shazamio_core", "aiohttp_retry", "pydantic"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
datas += collect_data_files("certifi")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
              "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Nirvash",
    console=False,
    icon=str(ROOT / "assets" / "nirvash.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="App", upx=False)
