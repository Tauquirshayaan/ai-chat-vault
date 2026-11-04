# -*- mode: python ; coding: utf-8 -*-
# Builds two executables:
#   AI_Chat_Vault.exe      the window (no console)
#   AI_Chat_Vault_CLI.exe  the text menu and command line
import os
import re

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(SPECPATH)
with open(os.path.join(ROOT, "chatvault", "__init__.py"), encoding="utf-8") as f:
    VERSION = re.search(r'__version__ = "([^"]+)"', f.read()).group(1)
NUMBERS = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])


def version_file(name, description):
    """Windows file properties (Details tab) generated from __version__."""
    path = os.path.join(workpath, f"version_{name}.txt")
    os.makedirs(workpath, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={NUMBERS}, prodvers={NUMBERS}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'AI Chat Vault contributors'),
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', 'AI Chat Vault'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")
    return path


try:
    THEME_FILES = collect_data_files("sv_ttk")
except Exception:
    THEME_FILES = []

ICON = os.path.join(ROOT, "assets", "icon.ico")

gui = Analysis(
    ["chat_vault.py"],
    datas=THEME_FILES,
    hiddenimports=["sv_ttk"] if THEME_FILES else [],
    excludes=["unittest", "pydoc"],
)
gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, gui.binaries, gui.datas, [],
    name="AI_Chat_Vault",
    console=False,
    icon=ICON,
    version=version_file("AI_Chat_Vault", "AI Chat Vault"),
    upx=False,
)

cli = Analysis(
    ["chat_vault_cli.py"],
    excludes=["tkinter", "_tkinter", "sv_ttk", "unittest", "pydoc"],
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, cli.binaries, cli.datas, [],
    name="AI_Chat_Vault_CLI",
    console=True,
    icon=ICON,
    version=version_file("AI_Chat_Vault_CLI", "AI Chat Vault command line"),
    upx=False,
)
