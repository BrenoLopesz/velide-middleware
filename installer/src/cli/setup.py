import sys
from pathlib import Path
from cx_Freeze import Executable, setup

# --- 1. ROBUST PATH SETUP ---
# Determine the absolute path to the folder containing THIS file
CURRENT_DIR = Path(__file__).resolve().parent

# Calculate root relative to installer/src/cli/setup.py
# Adjust .parent count based on your actual folder depth
ROOT_DIR = CURRENT_DIR.parent.parent.parent  # Go up 3 levels to velide-middleware
SRC_DIR = ROOT_DIR / "src"
OUTPUT_DIR = ROOT_DIR / "output" / "middleware"
RESOURCES_DIR = ROOT_DIR / "resources"

# Add src to python path so cx_Freeze can find imports
sys.path.insert(0, str(SRC_DIR))

# --- 2. FILE DEFINITIONS ---
main_script = SRC_DIR / "main.py"
icon_path = RESOURCES_DIR / "velide_corner.ico"

# Validate paths (Helpful for debugging)
if not main_script.exists():
    raise FileNotFoundError(f"Main script not found at: {main_script}")

# --- 3. DEPENDENCY CONFIGURATION ---
build_exe_options = {
    "packages": [
        "asyncio", 
        "cryptography", 
        "PyQt5", 
        "requests", 
        "dotenv",
        "watchdog", 
        "jwt", 
        "fdb", 
        "httpx", 
        "tqdm", 
        "screeninfo",
        "sqlalchemy", 
        "sqlalchemy_firebird",
        # "msvcrt" REMOVED: Never include msvcrt in 'packages'
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
    "include_files": [
        (str(RESOURCES_DIR), "resources"),
    ],
    "build_exe": str(OUTPUT_DIR),
    
    # --- FIX FOR [WinError 123] ---
    # We manually force the correct Qt bin path logic if the hook fails
    "include_msvcr": True, 
}

# --- 4. EXECUTABLE DEFINITION ---
target = Executable(
    script=str(main_script),
    base="Win32GUI",
    icon=str(icon_path) if icon_path.exists() else None,
    target_name="main.exe",
    copyright="Velide"
)

setup(
    name="Velide Middleware",
    version="4.1.0",
    description="Middleware do Velide para realizar " \
    "integrações com ERPs e sistemas locais.",
    options={"build_exe": build_exe_options},
    executables=[target],
)