# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for CiscoIOSBox.

Build with:
    pyinstaller ciscoiosbox.spec

Produces a single-file executable in dist/. See README.md for platform notes.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
project_root = Path(SPECPATH)

# ─── Data files ───────────────────────────────────────────────────────────────
# ntc-templates ships its TextFSM templates as package data. PyInstaller does
# not detect them (they are opened at runtime by path), so without this the
# frozen build silently falls back to the regex parsers.
datas = []
try:
    datas += collect_data_files("ntc_templates")
except Exception:                                    # noqa: BLE001
    print("WARNING: ntc-templates not found; the .exe will use regex parsers only.")

# ─── Hidden imports ───────────────────────────────────────────────────────────
# Modules resolved dynamically at runtime, which static analysis cannot see.
hiddenimports = [
    # keyring picks a backend at runtime by entry point.
    "keyring.backends.SecretService",
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.chainer",
    "keyring.backends.fail",
    # pyserial's platform-specific implementations.
    "serial.tools.list_ports",
    "serial.serialposix",
    "serial.serialwin32",
    "serial.serialcli",
    # netmiko loads device drivers from a dispatcher table.
    "netmiko.cisco",
    "netmiko.cisco.cisco_ios",
    # pyqtgraph resolves its Qt binding dynamically.
    "pyqtgraph.graphicsItems",
]

# netmiko's ConnectHandler maps device_type strings to classes at runtime, so
# every driver module must be bundled explicitly.
try:
    hiddenimports += collect_submodules("netmiko")
except Exception:                                    # noqa: BLE001
    pass

# pysnmp is optional; include it only when present.
try:
    import pysnmp                                    # noqa: F401
    hiddenimports += collect_submodules("pysnmp")
    hiddenimports += collect_submodules("pyasn1")
    datas += collect_data_files("pysnmp")
except ImportError:
    print("NOTE: pysnmp not installed; the build will use CLI polling only.")

# ─── Exclusions ───────────────────────────────────────────────────────────────
# Qt modules this application never imports. Excluding them roughly halves the
# bundle size, which matters for a tool people copy onto jump hosts.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngine",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuick3D",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    # Scientific stack pulled in transitively but unused.
    "matplotlib", "scipy", "pandas", "IPython", "tkinter", "PyQt5", "PyQt6",
]

a = Analysis(
    ["run.py"],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CiscoIOSBox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX often trips antivirus heuristics on network tools, and the saving is
    # modest. Leave it off.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # windowed app: no console on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                      # set to "resources/icon.ico" when you add one
)

# macOS: also emit a proper .app bundle so it behaves like a native application.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="CiscoIOSBox.app",
        icon=None,
        bundle_identifier="com.ciscoiosbox.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleShortVersionString": "0.1.0",
        },
    )
