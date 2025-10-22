import argparse
import os
import subprocess
import sys
import traceback
from typing import Tuple

from PyQt5.QtWidgets import QApplication
from sqlalchemy import create_engine

from config import Settings, TargetSystem, config
from connectors.farmax.farmax_repository import FarmaxRepository
from connectors.farmax.farmax_setup import FarmaxSetup
from presenters.dashboard_presenter import DashboardPresenter
from presenters.deliverymen_mapping_presenter import DeliverymenMappingPresenter
from presenters.device_code_presenter import DeviceCodePresenter
from services.deliveries_service import DeliveriesService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from services.strategies.cds_strategy import CdsStrategy
from services.auth_service import AuthService
from presenters.app_presenter import AppPresenter
from services.strategies.farmax_strategy import FarmaxStrategy
from states.main_state_machine import MainStateMachine
from utils.sql_utils import get_farmax_engine_string
from visual.main_view import MainView
from utils.log_handler import PackageFilter, QLogHandler
from utils.logger import setup_logging
from utils.bundle_dir import BUNDLE_DIR
from utils.fix_asyncio import apply_asyncio_fix
from utils.instance_lock import acquire_lock
from visual.fonts import load_fonts

# --- Startup Helpers ---

def loadCSS() -> str:
    """Loads the main stylesheet."""
    css_path = os.path.join(BUNDLE_DIR, 'resources', 'style.css')
    with open(css_path, 'r') as f:
        return f.read()

def is_update_checked() -> bool:
    """Parses command-line arguments to check if update has been run."""
    parser = argparse.ArgumentParser(description="Middleware to connect various different softwares to Velide.")
    parser.add_argument(
        "--is-update-checked",
        action="store_true",
        help="If update wasn't checked, it will close and run the update auto-installer first instead."
    )
    args = parser.parse_args()
    return args.is_update_checked

def open_installer(installer_path: str):
    """Launches the installer executable in a detached process."""
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            
    subprocess.Popen(
        [installer_path], 
        creationflags=creation_flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL
    )

def handle_startup_error(e: Exception):
    """Displays a fatal error to the user before exiting."""
    print("--- APPLICATION FAILED TO START ---")
    print(f"ERROR: {e}")
    traceback.print_exc()
    input("Press Enter to exit...")

# --- Application Setup Functions ---

def perform_pre_boot_checks():
    """Handles tasks that must run before QApplication is initialized."""
    apply_asyncio_fix()
    acquire_lock()  # Ensures only one instance is running

def handle_updates() -> bool:
    """
    Checks if an update is required and runs the installer.
    Returns True if the application should exit, False otherwise.
    """
    if not is_update_checked() and getattr(sys, 'frozen', False):
        installer_path = os.path.join(BUNDLE_DIR, "installer", "main.exe")
        open_installer(installer_path)
        return True  # Signal to exit
    return False # Signal to continue

def create_application() -> QApplication:
    """Initializes and configures the QApplication."""
    app = QApplication(sys.argv)
    app.setStyleSheet(loadCSS())
    load_fonts()
    return app

def configure_logging(view: MainView):
    """Sets up logging and connects the handler to the main view."""
    log_handler = QLogHandler()
    filters = ['httpx', 'httpcore', 'urllib3', 'requests', 'pydantic', 'watchdog', 'PyYAML']
    src_filter = PackageFilter(filters)
    log_handler.addFilter(src_filter)
    log_handler.new_log.connect(view.dashboard_screen.log_table.add_row)
    setup_logging(log_handler)  # Assuming this configures the root logger

def create_strategy(app_config: Settings):
    """Factory function to create the correct delivery strategy."""
    if app_config.target_system == TargetSystem.CDS:
        return CdsStrategy(app_config.api, app_config.folder_to_watch)
    
    elif app_config.target_system == TargetSystem.FARMAX:
        engine = create_engine(
            get_farmax_engine_string(app_config.farmax),
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True
        )
        farmax_setup = FarmaxSetup(engine)
        farmax_repository = FarmaxRepository(engine)
        return FarmaxStrategy(
            farmax_config=app_config.farmax,
            # farmax_setup=farmax_setup, 
            farmax_repository=farmax_repository
        )
    
    # Handles missing strategy
    raise ValueError(f"Sistema alvo não suportado: {app_config.target_system}")

def build_services(app_config: Settings) -> Tuple[AuthService, DeliveriesService, DeliverymenRetrieverService]:
    """Creates and wires together all core application services."""
    auth_service = AuthService(app_config.auth)
    delivery_service = DeliveriesService(app_config.api, app_config.target_system)
    
    strategy = create_strategy(app_config)
    delivery_service.set_strategy(strategy)
    
    deliverymen_retriever_service = DeliverymenRetrieverService(app_config.api, app_config.target_system, strategy)

    return auth_service, delivery_service, deliverymen_retriever_service

# --- Main Execution ---

def main():
    """Main application entry point."""
    
    # Run checks before any GUI code
    perform_pre_boot_checks()
    
    # Handle updates and exit if necessary
    if handle_updates():
        sys.exit(0)
    
    # Create the Qt Application and main view
    app = create_application()
    view = MainView()
    
    # Set up logging
    configure_logging(view)
    
    # Build services (Dependency Injection)
    auth_service, delivery_service, deliverymen_retriever_service = build_services(config)
    
    machine = MainStateMachine()

    device_code_presenter = DeviceCodePresenter(auth_service, view)
    deliverymen_mapping_presenter = DeliverymenMappingPresenter(
        deliverymen_retriever_service, 
        auth_service,
        view.deliverymen_mapping_screen
    )
    dashboard_presenter = DashboardPresenter(view.dashboard_screen, delivery_service)

    # Initialize the presenter to connect view and services
    presenter = AppPresenter(
        view=view, 
        state_machine=machine,
        auth_service=auth_service,
        deliverymen_retriever_service=deliverymen_retriever_service,
        device_code_presenter=device_code_presenter
    )
    machine.initial_state.addTransition(
        view.initial_screen.on_request_device_flow_start,
        machine.device_flow_state
    )
    # Skip initial screen
    machine.initial_state.addTransition(
        device_code_presenter.authenticated,
        machine.check_mapping_state
    )
    machine.device_flow_state.addTransition(
        device_code_presenter.authenticated,
        machine.check_mapping_state
    )
    machine.check_mapping_state.addTransition(
        deliverymen_retriever_service.mapping_is_required,
        machine.gathering_deliverymen_state
    )
    machine.check_mapping_state.addTransition(
        deliverymen_retriever_service.mapping_not_required,
        machine.dashboard_state
    )
    machine.gathering_deliverymen_state.addTransition(
        deliverymen_retriever_service.deliverymen_received,
        machine.deliverymen_mapping_state
    )
    machine.deliverymen_mapping_state.addTransition(
        deliverymen_mapping_presenter.mapping_done,
        machine.dashboard_state
    )
    
    machine.device_flow_state.addTransition(
        auth_service.error,
        machine.error_state
    )
    machine.gathering_deliverymen_state.addTransition(
        deliverymen_retriever_service.error,
        machine.error_state
    )

    # Start the application logic
    presenter.run()
    auth_service.load_stored_token()
    
    # Run the application event loop
    sys.exit(app.exec_())


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        handle_startup_error(e)