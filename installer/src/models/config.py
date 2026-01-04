import logging
from typing import Type, TypeVar
from pathlib import Path
import yaml
from pydantic import BaseModel, Field

T = TypeVar("T", bound="InstallerConfig")

logger = logging.getLogger(__name__)


class InstallerConfig(BaseModel):
    owner: str = Field(
        ..., description="Dono de repositório no qual o projeto deve ser buscado."
    )
    repo: str = Field(..., description="Repositório para buscar as atualizações.")

    @classmethod
    def from_yaml(cls: Type[T], path: str) -> T:
        """
        Loads configuration from a YAML file.

        Args:
            path: The path to the YAML configuration file.

        Returns:
            An instance of the AppConfig class.
        """
        config_path = Path(path)
        if not config_path.is_file():
            raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")

        with open(config_path, "r", encoding="utf-8") as f:
            # Load YAML file into a Python dictionary
            config_data = yaml.safe_load(f)
            if not isinstance(config_data, dict):
                raise TypeError(
                    "Não foi possível converter o conteúdo do arquivo de configuração."
                )

            # Create an instance of the class (cls) using the loaded data
            return cls(**config_data)
