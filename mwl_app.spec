# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

import os
import sys

# Directory where the script lives (repo root)
script_dir = os.path.dirname(os.path.abspath(__file__))

# logo.png naj bo zraven mwl_app.py v repozitoriju
datas = [
    ('logo.png', '.'),  # skopira logo.png v isti folder kot .exe
]

hiddenimports = [
    'pdfplumber',
    'pdfminer',
    'pdfminer.six',
    'pdfminer.high_level',
    'pdfminer.layout',
    'pdfminer.pdfparser',
    'pdfminer.pdfdocument',
    'pdfminer.pdfpage',
    'pdfminer.pdfinterp',
    'pdfminer.converter',
    'pdfminer.cmapdb',
]

a = Analysis(
    ['mwl_app.py'],
    pathex=[script_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='mwl_app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)