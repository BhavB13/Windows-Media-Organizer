# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None
project_root = os.path.abspath(os.path.join(SPECPATH, ".."))

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
