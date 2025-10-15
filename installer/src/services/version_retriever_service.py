import logging
import os
from PyQt5.QtCore import QObject, pyqtSignal
from packaging.version import parse, InvalidVersion

class VersionRetrieveService(QObject):
    """
    A service class that finds and validates a version file in a given directory.

    It inherits from QObject to leverage Qt's signal and slot mechanism.
    This service operates synchronously.

    Signals:
         version_found (pyqtSignal): Emitted when a valid version is found.
                                   Payload is a comparable packaging.version.Version object.
        error_occurred (pyqtSignal): Emitted when any error occurs.
                                    Payload is a user-friendly error string in Portuguese.
    """
    # Signal to emit a generic Python object, which will be our Version object.
    version_found = pyqtSignal(object)

    # Signal to emit when any error occurs during the process.
    error_occurred = pyqtSignal(str)

    def __init__(self, folder_path: str, parent=None):
        """Initializes the VersionRetriever service."""
        super().__init__(parent)
        self.version_filename = "version.txt"
        self.folder_path = folder_path
        # The regex pattern is no longer needed.

    def get_current_version(self):
        """
        Searches for, reads, and validates the version from a file in the specified folder.

        This is the main public method of the service. It will emit either
        the `version_found` or `error_occurred` signal based on the outcome.

        Args:
            folder_path: The absolute or relative path to the directory
                         where 'version.txt' should be located.
        """
        try:
            # Check if the provided path is actually a directory.
            if not os.path.isdir(self.folder_path):
                raise NotADirectoryError(f"O caminho fornecido não é um diretório válido: {self.folder_path}")

            version_file_path = os.path.join(self.folder_path, self.version_filename)

            # Read the content of the version file.
            with open(version_file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()

            # Validate the content by trying to parse it as a valid version.
            try:
                # Parse the content and store the resulting object.
                version_obj = parse(content)
            except InvalidVersion:
                raise ValueError(f"O conteúdo '{content}' não corresponde a um formato de versão válido (SemVer).")

            # If successful, emit the parsed Version object, not the string.
            logging.info(f"Versão encontrada e validada: {version_obj}")
            self.version_found.emit(version_obj)
        
        except FileNotFoundError:
            # Handle the case where the version.txt file does not exist.
            error_msg = f"Erro: O arquivo de versão '{self.version_filename}' não foi encontrado no diretório '{self.folder_path}'."
            logging.exception(error_msg)
            self.error_occurred.emit(error_msg)

        except PermissionError:
            # Handle the case where the script doesn't have permissions to read the file.
            error_msg = f"Erro: Sem permissão para ler o arquivo de versão em '{self.folder_path}'."
            logging.exception(error_msg)
            self.error_occurred.emit(error_msg)

        except NotADirectoryError as e:
            # Handle the case where the provided path is not a directory.
            error_msg = str(e)
            logging.exception(error_msg)
            self.error_occurred.emit(error_msg)
            
        except ValueError as e:
            # Handle our custom validation error for invalid SemVer format.
            error_msg = f"Erro de formato: {e}"
            logging.exception(error_msg)
            self.error_occurred.emit(error_msg)

        except Exception as e:
            # A catch-all for any other unexpected errors.
            # logging.exception provides more detail, including a stack trace.
            error_msg = f"Ocorreu um erro inesperado ao processar a versão: {e}"
            logging.exception(error_msg) # Use .exception to include traceback in logs
            self.error_occurred.emit(error_msg)
