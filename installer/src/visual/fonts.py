from PyQt5.QtGui import QFont, QFontDatabase
from typing import TypedDict
import os
from utils.bundle_dir import BUNDLE_DIR 

fonts = None

class FontsDict(TypedDict):
    light: QFont
    regular: QFont
    bold: QFont

def load_fonts() -> FontsDict:
    global fonts
    # Load the custom font file
    light_font_id = QFontDatabase.addApplicationFont(os.path.join(BUNDLE_DIR, "resources", "fonts", "font_light.ttf"))
    regular_font_id = QFontDatabase.addApplicationFont(os.path.join(BUNDLE_DIR, "resources", "fonts", "font_regular.ttf"))
    bold_font_id = QFontDatabase.addApplicationFont(os.path.join(BUNDLE_DIR, "resources", "fonts", "font_bold.ttf"))
    # Get the font family from the loaded font
    light_font_family = QFontDatabase.applicationFontFamilies(light_font_id)[0]
    regular_font_family = QFontDatabase.applicationFontFamilies(regular_font_id)[0]
    bold_font_family = QFontDatabase.applicationFontFamilies(bold_font_id)[0]
    # Create a QFont object with the custom font
    loaded_fonts = {
        'light': QFont(light_font_family, 12),  # Adjust the font size as needed,
        'regular': QFont(regular_font_family, 12),
        'bold': QFont(bold_font_family, 12, weight=QFont.Bold),
        'regular_small': QFont(regular_font_family, 9)
    }

    fonts = loaded_fonts
    return loaded_fonts

def get_fonts():
    return fonts

