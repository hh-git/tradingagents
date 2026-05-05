from .y_finance import (
    get_YFin_data_online,
    get_stock_stats_indicators_window,
    get_fundamentals as get_yfinance_fundamentals,
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
)
from .yfinance_news import get_news_yfinance, get_global_news_yfinance
from .alpha_vantage import (
    get_stock as get_alpha_vantage_stock,
    get_indicator as get_alpha_vantage_indicator,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_income_statement as get_alpha_vantage_income_statement,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_global_news as get_alpha_vantage_global_news,
)
from .okx import (
    get_okx_data_online,
    get_okx_indicator_window,
    get_okx_fundamentals,
    get_okx_balance_sheet,
    get_okx_cashflow,
    get_okx_income_statement,
    get_okx_insider_transactions,
    get_okx_news,
    get_okx_global_news,
)
from .alpha_vantage_common import AlphaVantageRateLimitError
from .config import get_config
from .stockstats_utils import MissingDependencyError


def _resolve_vendor_impl(method: str, vendor: str):
    """Resolve the vendor implementation at call time so tests can patch module globals."""
    vendor_impl = VENDOR_METHODS[method][vendor]
    if isinstance(vendor_impl, str):
        return globals()[vendor_impl]
    return vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": ["get_stock_data"]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": ["get_indicators"]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    }
}

VENDOR_LIST = [
    "yfinance",
    "alpha_vantage",
    "okx",
]

VENDOR_METHODS = {
    "get_stock_data": {
        "alpha_vantage": "get_alpha_vantage_stock",
        "yfinance": "get_YFin_data_online",
        "okx": "get_okx_data_online",
    },
    "get_indicators": {
        "alpha_vantage": "get_alpha_vantage_indicator",
        "yfinance": "get_stock_stats_indicators_window",
        "okx": "get_okx_indicator_window",
    },
    "get_fundamentals": {
        "alpha_vantage": "get_alpha_vantage_fundamentals",
        "yfinance": "get_yfinance_fundamentals",
        "okx": "get_okx_fundamentals",
    },
    "get_balance_sheet": {
        "alpha_vantage": "get_alpha_vantage_balance_sheet",
        "yfinance": "get_yfinance_balance_sheet",
        "okx": "get_okx_balance_sheet",
    },
    "get_cashflow": {
        "alpha_vantage": "get_alpha_vantage_cashflow",
        "yfinance": "get_yfinance_cashflow",
        "okx": "get_okx_cashflow",
    },
    "get_income_statement": {
        "alpha_vantage": "get_alpha_vantage_income_statement",
        "yfinance": "get_yfinance_income_statement",
        "okx": "get_okx_income_statement",
    },
    "get_news": {
        "alpha_vantage": "get_alpha_vantage_news",
        "yfinance": "get_news_yfinance",
        "okx": "get_okx_news",
    },
    "get_global_news": {
        "yfinance": "get_global_news_yfinance",
        "alpha_vantage": "get_alpha_vantage_global_news",
        "okx": "get_okx_global_news",
    },
    "get_insider_transactions": {
        "alpha_vantage": "get_alpha_vantage_insider_transactions",
        "yfinance": "get_yfinance_insider_transactions",
        "okx": "get_okx_insider_transactions",
    },
}

def get_category_for_method(method: str) -> str:
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    config = get_config()
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())
    fallback_vendors = primary_vendors.copy()
    for vendor in all_available_vendors:
        if vendor not in fallback_vendors:
            fallback_vendors.append(vendor)

    for vendor in fallback_vendors:
        if vendor not in VENDOR_METHODS[method]:
            continue
        vendor_impl = _resolve_vendor_impl(method, vendor)
        try:
            return vendor_impl(*args, **kwargs)
        except (AlphaVantageRateLimitError, MissingDependencyError):
            continue

    raise RuntimeError(f"No available vendor for '{method}'")
