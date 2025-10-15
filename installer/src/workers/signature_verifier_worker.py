# -*- coding: utf-8 -*-
"""
A service and QRunnable worker for verifying file signatures in a directory.

This module adapts the logic from the verification CLI script to run asynchronously
in a PyQt application, preventing the GUI from freezing during I/O-intensive
operations and providing progress feedback.
"""
import logging
import os
import json
import time
import traceback
import base64

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal, pyqtSlot
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature

from cli.verify_update import get_file_hash, load_public_key

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
    Worker thread for verifying all files in a directory against a signature file
    without blocking the GUI.
    """
    # Throttle progress updates to a maximum of once every 100ms (10 updates per second)
    PROGRESS_THROTTLE_INTERVAL = 0.1  # seconds
    def __init__(self, directory: str, signature_file: str, public_key_path: str):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.signals = SignatureVerifierSignals()

        self.directory = directory
        self.signature_file = signature_file
        self.public_key_path = public_key_path
        self.is_cancelled = False # Placeholder for future cancellation logic

    @pyqtSlot()
    def run(self):
        """
        Main logic for the verification worker.
        """
        self.logger.info("Worker de verificação de assinatura iniciado.")
        self.signals.started.emit()

        last_progress_time = 0

        try:
            # Step 1: Load public key
            self.logger.info(f"Carregando chave pública de {self.public_key_path}...")
            public_key = load_public_key(self.public_key_path)

            # Step 2: Load signatures from file
            self.logger.info(f"Carregando assinaturas de {self.signature_file}...")
            with open(self.signature_file, 'r') as f:
                signatures = json.load(f)
            
            inconsistencies = []
            
            # Step 3: Verify each file listed in the signature file
            signed_files_on_disk = set()
            files_to_verify = list(signatures.keys())
            total_files = len(files_to_verify)

            for i, relative_path in enumerate(files_to_verify):
                if self.is_cancelled:
                    return

                # Throttle progress
                current_time = time.time()
                if current_time - last_progress_time > self.PROGRESS_THROTTLE_INTERVAL:
                    self.signals.progress.emit(relative_path, i + 1, total_files)
                    last_progress_time = current_time
                    
                file_path = os.path.join(self.directory, relative_path)
                signed_files_on_disk.add(relative_path)

                if not os.path.exists(file_path):
                    inconsistencies.append(f"'{relative_path}': FALHA (Arquivo ausente)")
                    continue
                
                try:
                    current_hash = get_file_hash(file_path)
                    signature = base64.b64decode(signatures[relative_path])

                    public_key.verify(
                        signature,
                        current_hash,
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.MAX_LENGTH
                        ),
                        hashes.SHA256()
                    )
                except InvalidSignature:
                    inconsistencies.append(f"'{relative_path}': FALHA (Incompatibilidade de assinatura - arquivo adulterado)")
                except Exception as e:
                    inconsistencies.append(f"'{relative_path}': FALHA (Erro: {e})")

            # Step 4: Check for any unsigned files in the directory
            all_files_on_disk = set()
            for root, _, files in os.walk(self.directory):
                for name in files:
                    full_path = os.path.join(root, name)
                    relative_path = os.path.relpath(full_path, self.directory)
                    all_files_on_disk.add(relative_path)
            
            unsigned_files = all_files_on_disk - signed_files_on_disk
            for unsigned_file in unsigned_files:
                inconsistencies.append(f"'{unsigned_file}': FALHA (Arquivo não assinado)")

            self.logger.info("Processo de verificação concluído.")
            self.signals.finished.emit(inconsistencies)

        except (FileNotFoundError, IOError) as e:
            msg = f"Arquivo não encontrado: {e.filename}"
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        except json.JSONDecodeError:
            msg = f"Erro ao decodificar JSON de {self.signature_file}."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())
        except Exception:
            msg = "Ocorreu um erro inesperado durante a verificação."
            self.logger.error(msg, exc_info=True)
            self.signals.error.emit(msg, traceback.format_exc())

    def cancel(self):
        """Signals the worker that verification should be cancelled."""
        self.is_cancelled = True
