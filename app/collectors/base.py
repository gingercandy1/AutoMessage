from abc import ABC, abstractmethod
from typing import Any


class BaseCollector(ABC):
    COLLECTOR_DICT: dict[str, Any] = {}
    NAME: str = ""

    def __init_subclass__(cls, **kwargs):
        if cls.NAME in cls.COLLECTOR_DICT.keys():
            cls.COLLECTOR_DICT[cls.NAME].append(cls())
        else:
            cls.COLLECTOR_DICT[cls.NAME] = [cls()]

    @abstractmethod
    async def fetch(self, *args, **kwargs) -> int:
        raise NotImplementedError

    @abstractmethod
    def save(self, records) -> None:
        raise NotImplementedError

    @classmethod
    def collect_dict(cls):
        return cls.COLLECTOR_DICT

    @abstractmethod
    def configs(self):
        return []

class BaseAnalyzer(ABC):
    ANALYZER_DICT: dict[str, Any] = {}
    NAME: str = ""

    def __init_subclass__(cls, **kwargs):
        if not cls.NAME in cls.ANALYZER_DICT.keys():
            cls.ANALYZER_DICT[cls.NAME] = cls()

    @abstractmethod
    async def build_context(self, *args, **kwargs) -> str:
        raise NotImplementedError

    @classmethod
    def analyzer_dict(cls):
        return cls.ANALYZER_DICT