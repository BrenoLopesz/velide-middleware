import os
import sys
from cx_Freeze import Executable, setup

SETUP_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.abspath(os.path.join(SETUP_DIR, "..", "..", "..", "src"))
OUTPUT_DIR = os.path.abspath(
    os.path.join(SETUP_DIR, "..", "..", "..", "output", "middleware")
)
sys.path.insert(0, SRC_DIR)

# Construct paths relative to the setup.py directory
icon_path = os.path.join(SETUP_DIR, "..", "..", "resources", "velide_corner.ico")
main_script_path = os.path.join(SETUP_DIR, "..", "..", "..", "src", "main.py")
include_files_path = os.path.join(
    SETUP_DIR, "..", "..", "..", "resources"
)  # Assuming resources is at the root of the installer dir

main = Executable(
    script=main_script_path,
    # base=None,
    base="Win32GUI",
    icon=icon_path,
    target_name="main.exe",
)

setup(
    name="Velide Middleware",
    version="3.0",
    description="Middleware do Velide para realizar " \
    "integrações com ERPs e sistemas locais.",
    executables=[main],
    options={
        "build_exe": {
            "packages": [
                "asyncio",
                "cryptography",
                "PyQt5",
                "requests",
                "dotenv",
                "watchdog",
                "jwt",
                # "fdb",
                "httpx",
                # "pillow",
                "tqdm",
                "screeninfo",
                "sqlalchemy",
                "sqlalchemy_firebird",
                # "brotli",
                # "win32file",
                # "win32con",
                "msvcrt",
            ],
            "include_files": [
                (include_files_path, "resources")
                # ".env"
            ],
            "excludes": [
                "PyQt5.QtQml",
                "PyQt5.QtQuick",
                # You can also exclude this if you use 
                # 'requests' or 'httpx' for networking
                "PyQt5.QtNetwork", 
                "tkinter",  # Good practice to exclude if not used
            ],
            "build_exe": OUTPUT_DIR,
        },
    },
)
