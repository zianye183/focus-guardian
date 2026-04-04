"""
Central configuration loader for Focus Guardian.

All tunable parameters live in config.yaml at the project root.
Modules import from here instead of hardcoding values.
"""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config():
    """Load config.yaml and return as dict."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Loaded once at import time. Modules access via config.CONFIG.
CONFIG = load_config()
