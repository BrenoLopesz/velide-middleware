import sys
from pathlib import Path
from cx_Freeze import Executable, setup

# --- 1. PATH CONFIGURATION ---
# File location: installer/src/cli/setup_installer.py
CURRENT_DIR = Path(__file__).resolve().parent

INSTALLER_SRC_DIR = CURRENT_DIR.parent
INSTALLER_ROOT_DIR = CURRENT_DIR.parent.parent
OUTPUT_DIR = INSTALLER_ROOT_DIR.parent / "output" / "installer"
RESOURCES_DIR = INSTALLER_ROOT_DIR / "resources"

# Add installer source to python path so it can find its own modules
sys.path.insert(0, str(INSTALLER_SRC_DIR))

# --- 2. FILE DEFINITIONS ---
# The script that runs the update logic
main_installer_script = INSTALLER_SRC_DIR / "main.py"

icon_path = RESOURCES_DIR / "velide_corner.ico"

# Verify paths to catch errors early
if not main_installer_script.exists():
    # Fallback: maybe the user meant the file in the current dir?
    if (CURRENT_DIR / "main.py").exists():
         main_installer_script = CURRENT_DIR / "main.py"
    else:
         print(f"WARNING: Script not found at {main_installer_script}")

# --- 3. EXECUTABLE DEFINITION ---
main_installer = Executable(
    script=str(main_installer_script),
    base="Win32GUI",
    icon=str(icon_path) if icon_path.exists() else None,
    target_name="main.exe",
    copyright="Velide Soluções"
)

build_exe_options = {
    "packages": [
        "asyncio",
        "cryptography",
        "PyQt5",
        "requests",
        "dotenv",
        "watchdog",
        "jwt",
        "httpx",
        "tqdm",
        "screeninfo",
        # "msvcrt",  <-- REMOVED: Do not include this in packages
    ],
    "include_files": [
        # Copies 'installer/resources' -> 'output/installer/resources'
        (str(RESOURCES_DIR), "resources") 
    ],
    "excludes": [
        "PyQt5.QtQml", 
        "PyQt5.QtQuick", 
        "PyQt5.QtNetwork",
        "tkinter", 
        "unittest", 
        "xml", 
        "pydoc"
    ],
    "build_exe": str(OUTPUT_DIR),
    
    # --- FIX FOR C++ RUNTIME ---
    "include_msvcr": True, 
}

setup(
    name="Velide Update Installer",
    version="4.1.0",
    description="Instalador de atualizações do Middleware do Velide.",
    options={"build_exe": build_exe_options},
    executables=[main_installer],
)