"""Explicit application assets: never freeze a workstation DB/config/test export."""
from pathlib import Path


UI_RESOURCES = (
    "error.png", "loading.png", "style.css", "velide.png", "velide_white.png",
    "velide_36.png", "velide.ico", "velide_corner.ico",
    "fonts/font_regular.ttf", "fonts/font_light.ttf", "fonts/font_bold.ttf",
)
MAIN_RESOURCES = ("config_example.yml",) + UI_RESOURCES
UPDATER_RESOURCES = (
    "config.yml", "private_public.pem", "apply_update.bat", "installer.iss",
    "files_and_run.iss", "logic.pas", "settings.iss",
) + UI_RESOURCES


def resource_files(directory, updater=False):
    root = Path(directory)
    assets = UPDATER_RESOURCES if updater else MAIN_RESOURCES
    result = []
    for name in assets:
        source = root / name
        if not source.is_file():
            raise FileNotFoundError("Required release asset is missing: " + str(source))
        result.append((str(source), "resources/" + name))
    return result
