import logging
import os
import subprocess
import sys
from typing import Optional, Union
from packaging.version import Version, parse
from PyQt5.QtCore import QObject, QStateMachine, QState, QFinalState, pyqtSignal
from PyQt5.QtWidgets import QApplication
from services.batch_executor_service import BatchExecutorService
from services.installer_service import InstallerService
from services.version_retriever_service import VersionRetrieveService
from services.update_downloader_service import UpdateDownloaderService
from services.signature_verify_service import SignatureVerifyService
from services.update_checker_service import UpdateCheckerService
from models.config import InstallerConfig
from services.config_load_service import ConfigLoadService
from main_view import MainView
from utils.bundle_dir import BUNDLE_DIR

class AppPresenter(QObject):
    config_ready = pyqtSignal()
    version_ready = pyqtSignal()

    def __init__(
            self, 
            view: MainView, 
            config_load_service: ConfigLoadService, 
            version_retrieve_service: VersionRetrieveService,
            update_checker_service: UpdateCheckerService,
            downloader_service: UpdateDownloaderService,
            signature_verify_service: SignatureVerifyService,
            installer_service: InstallerService,
            batch_executor_service: BatchExecutorService
    ):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._view = view

        self._config_load_service = config_load_service
        self._version_retrieve_service = version_retrieve_service
        self._batch_executor_service = batch_executor_service
        self._update_checker_service = update_checker_service
        self._downloader_service = downloader_service
        self._signature_verify_service = signature_verify_service
        self._installer_service = installer_service

        self._machine = QStateMachine(self)

        # Will be found later on
        self._config: Optional[InstallerConfig] = None
        self._current_version: Optional[object] = None

        self._new_version: Optional[str] = None
        self._current_progress = 0

        self._destination_folder = os.path.join(BUNDLE_DIR, "..", "output")  
        self._installer_path = os.path.join(self._destination_folder, "installer", "velide_installer.exe")
        self._manifest_path = os.path.join(self._destination_folder, "manifest.json")
        self._signature_path = os.path.join(self._destination_folder, "manifest.sig")
        self._main_exe_path = os.path.join(BUNDLE_DIR, "..", "main.exe")

        self._create_states()
        self._connect_actions()
        self._build_state_machine()

    def run(self):
        self._machine.start()
        self._view.show()
        self._config_load_service.load_config()
        self._version_retrieve_service.get_current_version()

    def _check_for_update(self):
        # If we get here, we have everything we need to proceed.
        self._update_checker_service.check_for_update(self._config, self._current_version)

    def _on_config_found(self, config: InstallerConfig):
        """Slot for when the configuration is successfully loaded."""
        self._config = config # This will trigger the setter and emit config_loaded
        self.config_ready.emit()

    def _on_version_found(self, version: Version):
        """Slot for when the local version is successfully retrieved."""
        self._current_version = version # This will trigger the setter and emit version_loaded
        self.version_ready.emit()

    def _on_fail_to_retrieve_version(self, error: str):
        """Slot for when retrieving the local version fails."""
        self.logger.warning(
            f"Não foi possível obter a versão atual. "
            "Considerando isso como uma instalação corrompida. Uma atualização será realizada para tentar corrigir."
        )
        self._current_version = parse("0.0.0") # Still treat it as a valid "found" version
        self.version_ready.emit()

    def _on_new_version_found(self, installer_url: str, manifest_url: str, signature_url: str, new_version: str):
        self._view.update_screen.set_status_text("Baixando Atualização...")
        self._view.update_screen.set_version_text(f"v{new_version}")
        self._new_version = new_version
        
        # Trigger the download
        self._downloader_service.start_download([
            (installer_url, self._installer_path, True), # Report progress for installer
            (manifest_url, self._manifest_path, False), 
            (signature_url, self._signature_path, False)
        ])

    def _on_no_update_found(self):
        # TODO: Start app
        self._view.update_screen.set_status_text("Você possui a última versão.")
        # You might want a transition to the finished state here
        # This signal will be used in the state machine later

    def _on_download_progress(self, bytes_received: int, bytes_total: int):
        old_progress = self._current_progress
        new_progress = bytes_received / bytes_total
        self._current_progress = new_progress

        if round(old_progress * 100) != round(new_progress * 100):
            self._view.update_screen.set_version_text(f"v{self._new_version} ({round(new_progress * 100)}%)")

    def _on_download_finished(self):
        self._view.update_screen.set_status_text("Download completo. Verificando...")
        self._view.update_screen.set_version_text(f"v{self._new_version}")

        installer_dir = os.path.dirname(self._installer_path)
        public_key_path = os.path.join(BUNDLE_DIR, "resources", "private_public.pem")

        # Trigger the verification
        self._signature_verify_service.start_verification(
            directory=installer_dir, 
            manifest_path=self._manifest_path, 
            public_key_path=public_key_path
        )

    def _on_verification_finished(self, results: list):
        # Check if verification was successful (logic depends on your verifier worker)
        is_valid = all(item['valid'] for item in results)
        if is_valid:
            self._view.update_screen.set_status_text("Instalando...")

            self._installer_service.start_installation(self._installer_path)
        else:
            self.display_error_screen("Falha ao verificar assinatura!", "Um ou mais arquivos não estão devidamente assinados.")
    
    def _on_installation_finished(self):
        self._batch_executor_service.execute()

    def quit_application(self):
        # Opens main application if there's no update.
        if self._new_version is None:
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            
            subprocess.Popen(
                [self._main_exe_path, "--is-upgrade-checked"], 
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL
            )
            
        QApplication.instance().quit()

    def _create_states(self):
        # State for when we are gathering initial data
        self.gathering_data_state = QState(QState.ParallelStates) # This is the key!

        # Sub-state for config loading
        self.config_process_state = QState(self.gathering_data_state)
        self.waiting_for_config_state = QState(self.config_process_state)
        self.config_finished_state = QFinalState(self.config_process_state)
        self.config_process_state.setInitialState(self.waiting_for_config_state)

        # Sub-state for version loading
        self.version_process_state = QState(self.gathering_data_state)
        self.waiting_for_version_state = QState(self.version_process_state)
        self.version_finished_state = QFinalState(self.version_process_state)
        self.version_process_state.setInitialState(self.waiting_for_version_state)

        self.update_chain_state = QState()

        self.checking_for_update_state = QState(self.update_chain_state)
        self.downloading_state = QState(self.update_chain_state)
        self.verifying_state = QState(self.update_chain_state)
        self.installing_state = QState(self.update_chain_state)

        self.update_done_state = QState()
        self.finished_state = QFinalState()
        self.error_state = QFinalState()

    def _connect_actions(self):
        # --- Config Service ---
        self._config_load_service.error.connect(self.display_error_screen)
        self._config_load_service.config_found.connect(self._on_config_found)

        # --- Version Service ---
        self._version_retrieve_service.error_occurred.connect(self._on_fail_to_retrieve_version) # Custom error handling
        self._version_retrieve_service.version_found.connect(self._on_version_found)

        # --- Update Checker Service ---
        self._update_checker_service.error.connect(self.display_error_screen)
        self._update_checker_service.update_found.connect(self._on_new_version_found)
        self._update_checker_service.no_update_found.connect(self._on_no_update_found) 

        # --- Downloader Service ---
        self._downloader_service.error.connect(self.display_error_screen)
        self._downloader_service.progress.connect(self._on_download_progress)
        self._downloader_service.finished.connect(self._on_download_finished)

        # --- Verifier Service ---
        self._signature_verify_service.verification_error.connect(self.display_error_screen)
        self._signature_verify_service.verification_finished.connect(self._on_verification_finished)

        # --- Installer Service ---
        self._installer_service.error.connect(self.display_error_screen) 
        self._installer_service.finished.connect(self._on_installation_finished)

        # Close application on finished
        self._view.finished_screen.quit_app.connect(self.quit_application)

        # --- UI Screen Switching ---
        self.update_chain_state.entered.connect(lambda: self._view.show_screen_by_index(0))
        self.finished_state.entered.connect(lambda: self._view.show_screen_by_index(1))
        self.error_state.entered.connect(lambda: self._view.show_screen_by_index(2))

        # Connect the "entered" signal of the next state to the action
        self.checking_for_update_state.entered.connect(self._check_for_update)

        self.finished_state.entered.connect(self._view.finished_screen.wait_to_quit)

    def _build_state_machine(self):
        """Adds states and transitions to the state machine."""
        states = [
            self.gathering_data_state, self.update_chain_state, self.update_done_state, self.finished_state, self.error_state
        ]
        for state in states:
            self._machine.addState(state)

        self.update_chain_state.setInitialState(self.checking_for_update_state)
        self._machine.setInitialState(self.gathering_data_state)

        # --- Transitions for gathering data ---
        # When the presenter says the config is ready, the config process is done.
        self.waiting_for_config_state.addTransition(self.config_ready, self.config_finished_state)

        # When the presenter says the version is ready, the version process is done.
        self.waiting_for_version_state.addTransition(self.version_ready, self.version_finished_state)

        self.gathering_data_state.finished.connect(lambda:
                                                   print("fineshede")
                                                   )
        # --- Main Transitions ---
        # Gathering Data -> Checking for Updates
        self.gathering_data_state.addTransition(self.update_chain_state)

        # --- Transitions within the Update Chain ---
        # Checking -> Downloading (if update found)
        self.checking_for_update_state.addTransition(self._update_checker_service.update_found, self.downloading_state)
        
        # Checking -> Finished (if no update found)
        self.checking_for_update_state.addTransition(self._update_checker_service.no_update_found, self.finished_state)
        
        # Downloading -> Verifying
        self.downloading_state.addTransition(self._downloader_service.finished, self.verifying_state)
        
        # Verifying -> Installing
        self.verifying_state.addTransition(self._signature_verify_service.verification_finished, self.installing_state)
        
        # Installing -> Update Done
        self.installing_state.addTransition(self._installer_service.finished, self.update_done_state)

        # Update Done -> Finished
        self.update_done_state.addTransition(self._batch_executor_service.launched, self.finished_state)

        # --- Error Transitions (Any state can go to error) ---
        self.gathering_data_state.addTransition(self._config_load_service.error, self.error_state)
        self.checking_for_update_state.addTransition(self._update_checker_service.error, self.error_state)
        self.downloading_state.addTransition(self._downloader_service.error, self.error_state)
        self.verifying_state.addTransition(self._signature_verify_service.verification_error, self.error_state)
        self.installing_state.addTransition(self._installer_service.error, self.error_state)
        self.update_done_state.addTransition(self._batch_executor_service.error, self.error_state)

    def display_error_screen(self, error_message: str, stacktrace: Optional[str]):
        self._view.error_screen.set_error_description(error_message)
