from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML structure in {path}")

    return data


def load_companies() -> list[dict[str, Any]]:
    data = load_yaml(CONFIG_DIR / "companies.yaml")
    return data.get("companies", []) or []


def load_keywords() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "keywords.yaml")


def load_settings() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "settings.yaml")
