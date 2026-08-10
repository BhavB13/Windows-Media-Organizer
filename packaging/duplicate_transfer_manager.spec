# -*- mode: python ; coding: utf-8 -*-

import os
import re

block_cipher = None
project_root = os.path.abspath(os.path.join(SPECPATH, ".."))


def _read_version():
    """Read the single source of truth so the resource cannot drift."""
    source = os.path.join(project_root, "src", "duplicate_transfer_manager", "version.py")
    with open(source, encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', handle.read())
    return match.group(1) if match else "0.0.0"


def _build_version_resource():
    """Give the executable a Windows version resource.

    Without this the built exe has no ProductName, CompanyName, FileVersion, or
    description at all: the Properties dialog is blank, Task Manager and
    Programs and Features show no publisher, and an unsigned binary carrying no
    version information is treated far more harshly by SmartScreen and by
    antivirus heuristics.
    """

    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    version = _read_version()
    parts = [int(value) for value in re.findall(r"\d+", version)][:4]
    while len(parts) < 4:
        parts.append(0)
    numeric = tuple(parts)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [
                    StringStruct("CompanyName", "BhavB13"),
                    StringStruct("FileDescription", "Duplicate & Transfer Manager"),
                    StringStruct("FileVersion", version),
                    StringStruct("InternalName", "DuplicateTransferManager"),
                    StringStruct("LegalCopyright", "Copyright (c) BhavB13. MIT Licensed."),
                    StringStruct("OriginalFilename", "DuplicateTransferManager.exe"),
                    StringStruct("ProductName", "Duplicate & Transfer Manager"),
                    StringStruct("ProductVersion", version),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


version_resource = _build_version_resource() if os.name == "nt" else None

a = Analysis(
    [os.path.join(project_root, "main.py")],
    pathex=[os.path.join(project_root, "src"), project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "LICENSE"), "."),
        (os.path.join(project_root, "README.md"), "."),
        (os.path.join(project_root, "packaging", "android_platform_tools_manifest.json"), "packaging"),
        (os.path.join(project_root, "packaging", "update_public_key.json"), "packaging"),
        (os.path.join(project_root, "assets", "app.ico"), "assets"),
    ],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "send2trash",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DuplicateTransferManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "assets", "app.ico"),
    version=version_resource,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DuplicateTransferManager",
)
