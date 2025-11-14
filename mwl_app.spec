# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

# Dodatne datoteke, ki jih želimo zraven .exe
datas = [
    ('logo.png', '.'),  # kopira logo.png v isti folder kot exe
]

# Moduli, ki jih PyInstaller mogoče sam ne najde
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
    ['mwl_app.py'],   # skripta mora biti v rootu repozitorija
    pathex=['.'],     # trenutni direktorij (GitHub runner checkout)
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
