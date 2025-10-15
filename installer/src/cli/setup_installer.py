import os
import sys
from cx_Freeze import Executable, setup

SETUP_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.abspath(os.path.join(SETUP_DIR, ".."))
OUTPUT_DIR = os.path.abspath(os.path.join(SETUP_DIR, "..", "..", "..", "output", "installer"))
sys.path.insert(0, SRC_DIR)

# Construct paths relative to the setup.py directory
main_installer_script_path = os.path.join(SETUP_DIR, "..", "main.py")
icon_path = os.path.join(SETUP_DIR, "..", "..", "resources", "velide_corner.ico")
installer_include_files_path = os.path.join(SETUP_DIR, "..", "..", "resources") # Assuming resources is at the root of the installer dir

main_installer = Executable(
    script=main_installer_script_path,
    # base=None,
    base="Win32GUI",
    icon=icon_path,
    target_name="main.exe"
)

setup(
    name="Velide Update Installer",
    version="2.0",
    description="Instalador de atualizações do Middleware do Velide.",
    executables=[main_installer],
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
                # "brotli",
                # "win32file",
                # "win32con",
                "msvcrt"
            ],
             "include_files": [
                (installer_include_files_path, "resources")
                # ".env"
            ],
            "excludes": [
                "PyQt5.QtQml",
                "PyQt5.QtQuick",
                "PyQt5.QtNetwork", # You can also exclude this if you use 'requests' or 'httpx' for networking
                "tkinter" # Good practice to exclude if not used
            ],
            "build_exe": OUTPUT_DIR
        },
        
    }
)