from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


def setup_logging(settings: dict[str, Any]) -> logging.Logger:
    log_settings = settings.get("logging", {})
    level_name = str(log_settings.get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file_path = log_settings.get("log_file_path", "data/logs/scraper.log")
    log_path = Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("job_alert_bot")
    if logger.handlers:
        logger.setLevel(level)
        return logger

    logger.setLevel(level)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
