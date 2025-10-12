import logging
import os
import sys
from PyQt5.QtWidgets import QApplication
from services.update_checker_service import UpdateCheckerService
from services.config_load_service import ConfigLoadService
from models.config import InstallerConfig
from app_presenter import AppPresenter
from main_view import MainView
from utils.bundle_dir import BUNDLE_DIR
from visual.fonts import load_fonts

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

def load_css():
     # Load the CSS file
    with open(os.path.join(BUNDLE_DIR, 'resources', 'style.css'), 'r') as f:
        return f.read()
    
    
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(load_css())
    load_fonts()

    config_path = os.path.join(BUNDLE_DIR, 'resources', 'config.yml')
    config_load_service = ConfigLoadService(config_path)

    main_view = MainView()
    app_presenter = AppPresenter(main_view, config_load_service)
    app_presenter.run()


    sys.exit(app.exec_())