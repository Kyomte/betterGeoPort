# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the GeoPort rebuild.
from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = [('templates', 'templates'), ('static', 'static')]
binaries = []
hiddenimports = []

# pymobiledevice3 and the parts of its tree that ship data files / native libs
# or use dynamic imports PyInstaller's static analysis can miss.
COLLECT_PKGS = [
    'pymobiledevice3', 'developer_disk_image', 'ipsw_parser', 'zeroconf',
    'ifaddr', 'construct', 'pyimg4', 'apple_compress', 'qh3', 'pytun_pmd3',
    'sslpsk_pmd3', 'opack', 'bpylist2', 'pykdebugparser', 'remotezip',
]
for pkg in COLLECT_PKGS:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Some libs read their own version via importlib.metadata at runtime — bundle
# the .dist-info for those or they raise PackageNotFoundError when frozen.
for pkg in ['pymobiledevice3', 'zeroconf', 'construct', 'readchar', 'inquirer3',
            'blessed', 'wcwidth', 'prompt_toolkit', 'pygments', 'rich',
            'ipsw_parser', 'developer_disk_image']:
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'PyQt5', 'PyQt6', 'PySide6', 'tkinter', 'wx'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='GeoPort',
    console=True,
    target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name='GeoPort')
