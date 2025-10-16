# -*- coding: utf-8 -*-
"""
A service and QRunnable worker for verifying file signatures in a directory.

This module adapts the logic from the manifest verification CLI script to run
asynchronously in a PyQt application, preventing the GUI from freezing during
I/O-intensive operations and providing progress feedback.
"""
import logging
import os
import json
import time
import traceback
import base64
import hashlib

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

from utils.cryptography import get_file_hash, load_public_key

class SignatureVerifierSignals(QObject):
    """
    Defines the signals available for the signature verification worker.
    """
    # Signal emitted when verification starts
    started = pyqtSignal()

    # Signal emitted with (current_file_path, processed_count, total_to_process)
    progress = pyqtSignal(str, int, int)

    # Signal emitted upon completion, sending a list of inconsistency messages.
    # An empty list signifies success.
    finished = pyqtSignal(list)

    # Signal emitted on critical error, sending (friendly_message, traceback_string)
    error = pyqtSignal(str, object)


class SignatureVerifierWorker(QRunnable):
    """
    Worker thread for verifying all files in a directory against a signed manifest
    without blocking the GUI.
    """
    PROGRESS_THROTTLE_INTERVAL = 0.1  # seconds

    def __init__(self, directory: str, manifest_path: str, public_key_path: str):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.signals = SignatureVerifierSignals()

        self.directory = directory
        self.manifest_path = manifest_path
        self.public_key_path = public_key_path
        self.is_cancelled = False

    @pyqtSlot()
    def run(self):
        """
        Main logic for the verification worker.
        """
        self.logger.info("Worker de verificação de manifesto iniciado.")
        self.signals.started.emit()
        last_progress_time = 0

        try:
            # Step 1: Load public key
            self.logger.info(f"Carregando chave pública de {self.public_key_path}...")
            public_key = load_public_key(self.public_key_path)
            if not public_key:
                raise FileNotFoundError(f"Não foi possível carregar a chave pública de {self.public_key_path}")

            # Step 2: Verify the manifest file itself (CRITICAL STEP)
            signature_path = self.manifest_path.rsplit('.', 1)[0] + ".sig"
            self.logger.info(f"Verificando a assinatura do manifesto '{os.path.basename(self.manifest_path)}'...")

            with open(self.manifest_path, "rb") as f:
                manifest_bytes = f.read()
            with open(signature_path, 'r') as f:
                signature = base64.b64decode(f.read())

            manifest_hash = hashlib.sha256(manifest_bytes).digest()

            public_key.verify(
                signature,
                manifest_hash,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            self.logger.info("Assinatura do manifesto é válida. O manifesto é confiável.")

            # Step 3: Load trusted data from the verified manifest
            manifest_data = json.loads(manifest_bytes)
            manifest_hashes = {item['hash'] for item in manifest_data.get('files', [])}
            
            inconsistencies = []

            # Step 4: Scan local directory and verify files against manifest hashes
            self.logger.info("Verificando arquivos locais com base no manifesto confiável...")
            
            local_files_to_check = []
            ignore_files = {os.path.basename(self.manifest_path), os.path.basename(signature_path)}
            for root, _, files in os.walk(self.directory):
                for name in files:
                    if name not in ignore_files:
                        local_files_to_check.append(os.path.join(root, name))
            
            total_files = len(local_files_to_check)
            for i, file_path in enumerate(local_files_to_check):
                if self.is_cancelled:
                    return

                # Throttle progress signal
                current_time = time.time()
                if current_time - last_progress_time > self.PROGRESS_THROTTLE_INTERVAL:
                    self.signals.progress.emit(os.path.basename(file_path), i + 1, total_files)
                    last_progress_time = current_time

                try:
                    current_hash = get_file_hash(file_path)
                    if current_hash not in manifest_hashes:
                        relative_path = os.path.relpath(file_path, self.directory)
                        inconsistencies.append(f"'{relative_path}': NÃO CONFIÁVEL (Arquivo não listado no manifesto)")
                except Exception as e:
                    relative_path = os.path.relpath(file_path, self.directory)
                    inconsistencies.append(f"'{relative_path}': FALHA (Erro ao processar: {e})")

            self.logger.info("Processo de verificação concluído.")
            self.signals.finished.emit(inconsistencies)

        except InvalidSignature:
            msg = "MANIFESTO ADULTERADO: A assinatura é inválida."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        except (FileNotFoundError, IOError) as e:
            msg = f"Arquivo necessário não encontrado: {e.filename}"
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        except json.JSONDecodeError:
            msg = f"Erro ao decodificar JSON do manifesto '{os.path.basename(self.manifest_path)}'."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        except Exception:
            msg = "Ocorreu um erro inesperado durante a verificação."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

    def cancel(self):
        """Signals the worker that verification should be cancelled."""
        self.is_cancelled = True
