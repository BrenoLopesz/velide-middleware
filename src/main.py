import sys
import os
from PyQt5.QtWidgets import QApplication
from services.deliveries_service import DeliveriesService
from services.strategies.cds_strategy import CdsStrategy
from services.auth_service import AuthService
from presenters.app_presenter import AppPresenter
from config import TargetSystem, config
from visual.main_view import MainView
from utils.log_handler import PackageFilter, QLogHandler
from utils.logger import setup_logging
from utils.bundle_dir import BUNDLE_DIR
from utils.fix_asyncio import apply_asyncio_fix
from utils.instance_lock import acquire_lock # Import the lock function
from visual.fonts import load_fonts

def loadCSS():
     # Load the CSS file
    with open(os.path.join(BUNDLE_DIR, 'resources', 'style.css'), 'r') as f:
        return f.read()

if __name__ == '__main__':
    try:
        # Apply the fix for asyncio on Python 3.8 and Windows 7
        apply_asyncio_fix()

        # Attempt to acquire the instance lock before starting the app
        acquire_lock()
        
        app = QApplication(sys.argv)
        app.setStyleSheet(loadCSS())
        load_fonts()

        view = MainView()

        # Set up all loggers
        log_handler = QLogHandler()
        src_filter = PackageFilter('src')
        log_handler.addFilter(src_filter)
        log_handler.new_log.connect(view.cds_screen.log_table.add_row)
        logger = setup_logging(log_handler)

        auth_service = AuthService(config.auth)
        delivery_service = DeliveriesService(config.api, config.target_system)

        if config.target_system == TargetSystem.CDS:
            cds_strategy = CdsStrategy(config.api, config.folder_to_watch)
            delivery_service.set_strategy(cds_strategy)

        presenter = AppPresenter(view=view, auth_service=auth_service, delivery_service=delivery_service)

        # 2. Tell the presenter to start the application
        presenter.run()

        auth_service.load_stored_token()

        # sqlite_manager = SQLiteManager()
        # sqlite_manager.connect()
        
        sys.exit(app.exec_())
    except Exception as e:
        print("--- APPLICATION FAILED TO START ---")
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc() # This gives even more detail
        input("Press Enter to exit...") # This will pause the window