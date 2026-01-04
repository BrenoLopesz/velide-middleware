# -*- coding: utf-8 -*-

"""
This module provides a QRunnable class to execute an Inno Setup installer in
a background thread, with signals for reporting progress and errors to the
main PyQt5 application thread.
"""

import logging
import os
import subprocess

from PyQt5.QtCore import QObject, QRunnable, pyqtSignal


class InstallerSignals(QObject):
    """
    Defines the signals available from the installer running in a QRunnable.

    Signals:
        error (str): Emitted when an error occurs.
        finished (object): Emitted when the installer process has finished
                           successfully.
    """

    error = pyqtSignal(str)
    finished = pyqtSignal()


class InstallerRunnableWorker(QRunnable):
    """
    QRunnable worker to execute an Inno Setup installer silently in a
    separate thread.
    """

    def __init__(self, installer_path: str):
        """
        Initializes the runnable.

        Args:
            installer_path (str): The absolute path to the Inno Setup
                                  installer executable.
        """
        super().__init__()
        if not isinstance(installer_path, str):
            raise TypeError("installer_path must be a string.")

        self.logger = logging.getLogger(__name__)
        self.installer_path = installer_path
        self.signals = InstallerSignals()

    def run(self):
        """
        Executes the installer. This method is called when the runnable
        is started by a QThreadPool.
        """
        try:
            # Step 1: Validate the installer path
            if not os.path.exists(self.installer_path):
                error_message = (
                    f"Erro: O arquivo de instalação não "
                    f"foi encontrado em '{self.installer_path}'."
                )
                self.logger.error(error_message)
                self.signals.error.emit(error_message)
                return

            if not os.path.isfile(self.installer_path):
                error_message = (
                    f"Erro: O caminho '{self.installer_path}' não é um arquivo válido."
                )
                self.logger.error(error_message)
                self.signals.error.emit(error_message)
                return

            # Step 2: Prepare the command for silent installation
            # /VERYSILENT: Hides the installation wizard and progress window.
            # /SP-: Disables the "This will install..." prompt at the beginning
            # of setup.
            # /NOCANCEL: Prevents the user from canceling during installation.
            # /UPDATE=1: Install the version auto-updater (this application)
            # new version in the "%TEMP%" folder, since wouldn't be possible
            # to update it while running.
            command = [
                self.installer_path,
                "/VERYSILENT",
                "/SP-",
                "/NOCANCEL",
                "/UPDATE=1",
            ]

            # Step 3: Execute the installer process
            # We use subprocess.run with check=True, which will raise
            # CalledProcessError for a non-zero exit code.
            process = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="latin-1",  # Inno Setup logs often use this encoding
            )

            print(f"Installer STDOUT:\n{process.stdout}")
            print(f"Installer STDERR:\n{process.stderr}")

        except subprocess.CalledProcessError as e:
            # This block catches errors where the installer runs but returns
            # a non-zero exit code (indicating failure).
            error_message = (
                f"Ocorreu um erro durante a instalação.\n\n"
                f"O instalador finalizou com o código de erro: {e.returncode}.\n\n"
                f"Saída Padrão (stdout):\n{e.stdout}\n\n"
                f"Saída de Erro (stderr):\n{e.stderr}"
            )
            self.logger.exception(error_message)
            self.signals.error.emit(error_message)
            return

        except FileNotFoundError:
            # This block catches the error if the installer executable itself
            # cannot be found by the system.
            error_message = (
                f"Erro de sistema: O executável do instalador "
                f"'{self.installer_path}' não pôde ser executado. "
                f"Verifique as permissões e o caminho."
            )
            self.logger.exception(error_message)
            self.signals.error.emit(error_message)
            return

        except Exception:
            # This is a generic catch-all for any other unexpected errors.
            error_message = (
                "Ocorreu um erro inesperado ao tentar executar o instalador."
            )
            self.logger.exception(error_message)
            self.signals.error.emit(error_message)
            return

        # If no exceptions were caught, the installation was successful.
        self.signals.finished.emit()
