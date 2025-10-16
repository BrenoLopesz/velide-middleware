import logging
import os

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from workers.signature_verifier_worker import SignatureVerifierWorker

class SignatureVerifyService(QObject):
    """
    Manages the signature verification process in a background thread.
    """
    # Define signals that the UI can connect to
    verification_started = pyqtSignal()
    verification_progress = pyqtSignal(str, int, int)
    verification_finished = pyqtSignal(list)
    verification_error = pyqtSignal(str, object)
    
    def __init__(self, parent: QObject = None):
        super().__init__(parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.logger = logging.getLogger(__name__)

    def start_verification(self, directory: str, manifest_path: str, public_key_path: str):
        """
        Creates and starts a new verification worker.
        
        Args:
            directory: The path to the folder to verify.
            signature_file: The signature file to verify against.
            public_key_path: Path to the public key file for verification.
        """
        if not all([os.path.isdir(directory), os.path.isfile(manifest_path), os.path.isfile(public_key_path)]):
            err_msg = "Um ou mais caminhos são inválidos. Por favor, verifique os caminhos do diretório, da assinatura e do arquivo de chave."
            self.logger.warning(err_msg)
            # Emit error directly without starting worker for invalid startup conditions
            self.verification_error.emit(err_msg, "")
            return
            
        worker = SignatureVerifierWorker(directory, manifest_path, public_key_path)

        # Connect worker signals to the service's signals
        worker.signals.started.connect(self.verification_started)
        worker.signals.progress.connect(self.verification_progress)
        worker.signals.finished.connect(self.verification_finished)
        worker.signals.error.connect(self.verification_error)
        
        self.thread_pool.start(worker)
        self.logger.info(f"Worker de verificação para '{directory}' enviado para o pool de threads.")
