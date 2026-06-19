# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for BitFerry (macOS .app bundle)
# Build: pyinstaller bitferry.spec

import sys

block_cipher = None

a = Analysis(
    ['bitferry.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.sip',
        # 在线更新 HTTPS 校验用的 CA 证书库
        'certifi',
        # macOS: Dock 图标点击激活窗口 + 屏幕录制权限检查
        'AppKit',
        'Quartz',
        'objc',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter',
        'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL', 'cv2',
    ],
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
    name='BitFerry',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='bitferry.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=False,
    upx_exclude=[],
    name='BitFerry',
)

app = BUNDLE(
    coll,
    name='BitFerry.app',
    icon='bitferry.icns',
    bundle_identifier='com.bitferry.app',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'CFBundleDisplayName': 'BitFerry',
        'CFBundleShortVersionString': '1.1.5',
        'CFBundleVersion': '1.1.5',
        'NSLocalNetworkUsageDescription': 'BitFerry 需要访问局域网以发现并连接设备。',
        'NSScreenCaptureUsageDescription': 'BitFerry 需要屏幕录制权限以进行区域截图。',
        'NSBluetoothAlwaysUsageDescription': '',
        'LSApplicationCategoryType': 'public.app-category.utilities',
    },
)
