# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（路径相对本文件，可在任意机器上重建）。"""
import glob
import os

HERE = os.path.abspath(SPECPATH)
WEB = os.path.join(HERE, "..", "web-radio")
ICON = os.path.join(HERE, "icon.ico")

# 可选：独立 venv（存在则用其中的 ffmpeg / vosk 模型）
VENV_CANDIDATES = [
    os.path.join(HERE, ".venv-build"),
    os.path.join(HERE, "venv"),
]
VENV = next((v for v in VENV_CANDIDATES if os.path.isdir(v)), None)

# ffmpeg 二进制（imageio_ffmpeg 自带）
ffmpeg_exe = "ffmpeg"
if VENV:
    hits = glob.glob(os.path.join(VENV, "Lib", "site-packages", "imageio_ffmpeg",
                                  "binaries", "ffmpeg*.exe"))
    if hits:
        ffmpeg_exe = hits[0]

# Vosk 英文模型（打包进 exe，离线可用）：优先本目录 models/
models_root = os.path.join(HERE, "models")
has_model = os.path.isdir(models_root) and any(
    d.startswith("vosk-model") for d in os.listdir(models_root))

# Vosk 通过 cffi 从自身包目录 dlopen libvosk.dll（及 MinGW 依赖 dll）
vosk_pkg = None
if VENV:
    p = os.path.join(VENV, "Lib", "site-packages", "vosk")
    if os.path.isdir(p):
        vosk_pkg = p
vosk_dlls = ["libvosk.dll", "libgcc_s_seh-1.dll",
             "libstdc++-6.dll", "libwinpthread-1.dll"]

binaries = [(ffmpeg_exe, ".")] if ffmpeg_exe != "ffmpeg" else []
datas = [(WEB, "web-radio")]
if has_model:
    datas.append((models_root, "models"))
if vosk_pkg:
    for dll in vosk_dlls:
        p = os.path.join(vosk_pkg, dll)
        if os.path.isfile(p):
            datas.append((p, "vosk"))

a = Analysis(
    [os.path.join(HERE, "app.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "asr", "vosk", "vosk.transcriber", "websockets", "imageio_ffmpeg",
        "webview", "webview.platforms", "webview.platforms.edgechromium",
        "webview.platforms.cef", "webview.platforms.winforms",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="网络收音机",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON] if os.path.isfile(ICON) else None,
)
