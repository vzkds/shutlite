"""Gera packaging/version_info.txt (aba Detalhes do shutlite.exe) a partir de server.__version__."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
version = re.search(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"', (ROOT / "server.py").read_text(encoding="utf-8"), re.M)
if not version:
    sys.exit("__version__ não encontrado em server.py")
nums = tuple(int(n) for n in version.groups()) + (0,)
text = ".".join(str(n) for n in nums[:3])

(Path(__file__).resolve().parent / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041604B0', [
      StringStruct('CompanyName', 'shutlite'),
      StringStruct('FileDescription', 'shutlite - desligue o PC pelo celular'),
      StringStruct('FileVersion', '{text}'),
      StringStruct('InternalName', 'shutlite'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'shutlite.exe'),
      StringStruct('ProductName', 'shutlite'),
      StringStruct('ProductVersion', '{text}')])]),
    VarFileInfo([VarStruct('Translation', [0x0416, 1200])])
  ]
)
""", encoding="utf-8")
print(f"version_info.txt: {text}")
