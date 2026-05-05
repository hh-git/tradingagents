from copy import deepcopy
from typing import Dict, Optional

import tradingagents.default_config as default_config

# Use default config but allow it to be overridden
_config: Optional[Dict] = None

_OKX_DATA_VENDORS = {
    "core_stock_apis": "okx",
    "technical_indicators": "okx",
    "fundamental_data": "okx",
    "news_data": "okx",
}


def normalize_config(config: Optional[Dict] = None, base: Optional[Dict] = None) -> Dict:
    """Return a fully merged runtime config with asset-aware vendor defaults."""
    source = deepcopy(base or default_config.DEFAULT_CONFIG)
    overrides = deepcopy(config or {})

    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(source.get(key), dict):
            source[key].update(value)
        else:
            source[key] = value

    asset_universe = str(source.get("asset_universe", "equity")).strip().lower()
    if asset_universe == "okx":
        requested_vendors = overrides.get("data_vendors")
        default_vendors = default_config.DEFAULT_CONFIG.get("data_vendors", {})
        if requested_vendors is None or requested_vendors == default_vendors:
            source["data_vendors"] = deepcopy(_OKX_DATA_VENDORS)
        else:
            vendors = deepcopy(_OKX_DATA_VENDORS)
            vendors.update(requested_vendors)
            source["data_vendors"] = vendors

    return source


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        _config = normalize_config()


def set_config(config: Dict):
    """Update the configuration with custom values."""
    global _config
    _config = normalize_config(config, base=_config)


def get_config() -> Dict:
    """Get the current configuration."""
    if _config is None:
        initialize_config()
    return deepcopy(_config)


# Initialize with default config
initialize_config()
