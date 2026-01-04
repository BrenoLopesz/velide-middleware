import logging
import os
import sys
from PyQt5.QtWidgets import QApplication
from services.installer_service import InstallerService
from services.signature_verify_service import SignatureVerifyService
from services.update_checker_service import UpdateCheckerService
from services.update_downloader_service import UpdateDownloaderService
from services.batch_executor_service import BatchExecutorService
from services.version_retriever_service import VersionRetrieveService
from services.config_load_service import ConfigLoadService
from app_presenter import AppPresenter
from main_view import MainView
from utils.bundle_dir import BUNDLE_DIR
from visual.fonts import load_fonts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def load_css():
    # Load the CSS file
    with open(os.path.join(BUNDLE_DIR, "resources", "style.css"), "r") as f:
        return f.read()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(load_css())
    load_fonts()

    resources_path = os.path.join(BUNDLE_DIR, "resources")
    config_path = os.path.join(resources_path, "config.yml")
    config_load_service = ConfigLoadService(config_path)

    version_retriever_service = VersionRetrieveService(resources_path)

    update_checker_service = UpdateCheckerService()
    update_downloader_service = UpdateDownloaderService()
    update_installer_service = InstallerService()
    signature_verify_service = SignatureVerifyService()

    apply_update_path = os.path.join(resources_path, "apply_update.bat")
    batch_executor_service = BatchExecutorService(apply_update_path)

    main_view = MainView()
    app_presenter = AppPresenter(
        view=main_view,
        config_load_service=config_load_service,
        version_retrieve_service=version_retriever_service,
        update_checker_service=update_checker_service,
        downloader_service=update_downloader_service,
        signature_verify_service=signature_verify_service,
        installer_service=update_installer_service,
        batch_executor_service=batch_executor_service,
    )
    app_presenter.run()

    sys.exit(app.exec_())
