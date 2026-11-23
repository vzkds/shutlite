# Build do shutlite.exe (um arquivo só, com console).
#
#     python -m pip install pyinstaller
#     pyinstaller packaging/shutlite.spec
#
# Sai em dist/shutlite.exe. O workflow de release (.github/workflows/release.yml) faz o mesmo.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "web", "index.html"), "web"),
        (os.path.join(ROOT, "web", "qrcode.min.js"), "web"),
    ],
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="shutlite",
    console=True,  # mostra endereços e erros; o início automático abre minimizado
    icon=os.path.join(ROOT, "Brand", "shutlite.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    upx=False,  # UPX aumenta falso positivo de antivírus
)
