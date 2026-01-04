from abc import ABC, abstractmethod


class TargetSystem(ABC):
    @property
    @abstractmethod
    def apply_changes(self) -> bool:
        """Whether this applies changes to the target system or not"""
        pass
