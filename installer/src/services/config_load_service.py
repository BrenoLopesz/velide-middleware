import logging
from PyQt5.QtCore import QObject, pyqtSignal
from pydantic import ValidationError
import yaml

from models.config import InstallerConfig


class ConfigLoadService(QObject):
    config_found = pyqtSignal(InstallerConfig)
    error = pyqtSignal(str)

    def __init__(self, path: str):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self._path = path

    def load_config(self):
        try:
            config = InstallerConfig.from_yaml(self._path)
            self.config_found.emit(config)
        except (FileNotFoundError, TypeError) as e:
            self.logger.exception()
            self.error.emit(e)
        except yaml.YAMLError:
            # Handle errors during YAML parsing
            msg = "Erro ao ler arquivo YAML."
            self.logger.exception(msg)
            self.emit.emit(msg)
        except ValidationError:
            # Handle errors during Pydantic validation
            msg = "Falha ao validar o arquivo de configuração."
            self.logger.exception(msg)
            self.emit.emit(msg)
        except Exception:
            msg = (
                "Ocorreu um erro inesperado durante a "
                "leitura do arquivo de configuração."
            )
            self.logger.exception(msg)
            self.emit.emit(msg)
