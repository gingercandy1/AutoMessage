import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional


DEFAULT_FMT   = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DEFAULT_DATE  = "%Y-%m-%d %H:%M:%S"
DEFAULT_LEVEL = logging.INFO


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time":   self.formatTime(record, DEFAULT_DATE),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
            "file":   record.filename,
            "line":   record.lineno,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class AppLogger:
    _initialized: bool = False
    _root_level:  int  = DEFAULT_LEVEL
    _use_json:    bool = False

    @classmethod
    def setup(
        cls,
        level: int        = DEFAULT_LEVEL,
        log_dir: Optional[str] = None,
        json_output: bool = False,
        backup_days: int  = 7,
    ) -> None:
        if cls._initialized:
            return

        cls._root_level = level
        cls._use_json   = json_output

        formatter = JsonFormatter() if json_output else logging.Formatter(DEFAULT_FMT, DEFAULT_DATE)
        root = logging.getLogger()
        root.setLevel(level)

        if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
                   for h in root.handlers):
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            root.addHandler(console)

        if log_dir:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            file_handler = TimedRotatingFileHandler(
                filename    = log_path / "app.log",
                when        = "midnight",
                interval    = 1,
                backupCount = backup_days,
                encoding    = "utf-8",
            )
            file_handler.suffix = "%Y-%m-%d.log"
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

        for noisy_lib in ("httpx", "httpcore", "aiogram", "urllib3", "aiohttp"):
            logging.getLogger(noisy_lib).setLevel(logging.WARNING)
        cls._initialized = True

    @classmethod
    def get(cls, name: str, level: Optional[int] = None) -> logging.Logger:
        if not cls._initialized:
            cls.setup()

        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.propagate = True

        if level is not None:
            logger.setLevel(level)
        return logger

    @classmethod
    def set_level(cls, level: int, name: Optional[str] = None) -> None:
        target = logging.getLogger(name) if name else logging.getLogger()
        target.setLevel(level)

    @classmethod
    def enable_debug(cls) -> None:
        cls.set_level(logging.DEBUG)

    @classmethod
    def disable_debug(cls) -> None:
        cls.set_level(logging.INFO)


_default_logger = logging.getLogger("app")


def _ensure() -> logging.Logger:
    if not AppLogger._initialized:
        AppLogger.setup(
            level=logging.DEBUG,    # 开发时用 DEBUG，生产用 INFO
            log_dir="logs",         # 不传则只输出控制台
            json_output=False,      # 生产环境接日志平台时改 True
            backup_days=7           # 日志文件保留 7 天
        )
    return _default_logger


def debug(msg: str, *args, **kwargs):   _ensure().debug(msg, *args, **kwargs)
def info(msg: str, *args, **kwargs):    _ensure().info(msg, *args, **kwargs)
def warning(msg: str, *args, **kwargs): _ensure().warning(msg, *args, **kwargs)
def error(msg: str, *args, **kwargs):   _ensure().error(msg, *args, **kwargs)
def critical(msg: str, *args, **kwargs):_ensure().critical(msg, *args, **kwargs)

def exception(msg: str, *args, **kwargs):
    _ensure().exception(msg, *args, **kwargs)

_ensure()