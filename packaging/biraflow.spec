# PyInstaller spec: a folder build with two executables sharing one runtime.
#   biraflow.exe      the app (no console window)
#   biraflow-cli.exe  the same app with a console, for `biraflow-cli --check` / `--transcribe`
# Build with scripts\build-installer.ps1, which also signs and packages it.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "biraflow", "assets", "app.ico")
VERSION_FILE = os.path.join(ROOT, "build", "version_info.txt")

datas = collect_data_files("sv_ttk") + [
    (os.path.join(ROOT, "biraflow", "assets"), os.path.join("biraflow", "assets")),
    (os.path.join(ROOT, "README.md"), "."),
]
# comtypes.gen: the UI Automation wrappers, generated once here instead of at every start
hiddenimports = (collect_submodules("biraflow") + collect_submodules("comtypes.gen")
                 + ["pystray._win32", "comtypes.client", "comtypes.stream", "vosk", "vosk.vosk_cffi", "_cffi_backend",
                    "soundfile", "tkinterdnd2"])
# voice activation: libvosk.dll and its MinGW runtime, loaded from the vosk package folder
binaries = [(path, "vosk") for path, _ in collect_dynamic_libs("vosk")]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest", "matplotlib", "scipy", "IPython", "pandas", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, icon=ICON, version=VERSION_FILE, upx=False)
app = EXE(pyz, a.scripts, [], name="biraflow", console=False, **common)
cli = EXE(pyz, a.scripts, [], name="biraflow-cli", console=True, **common)

COLLECT(app, cli, a.binaries, a.datas, name="BiraFlow", upx=False)
