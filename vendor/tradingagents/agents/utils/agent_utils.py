from langchain_core.messages import HumanMessage, RemoveMessage
from tradingagents.dataflows.config import get_config

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    asset_universe = get_config().get("asset_universe", "equity").strip().lower()
    if asset_universe == "okx":
        okx_parts = ticker.strip().upper().split("-")
        spot_note = (
            " This is a spot rotation candidate: evaluate it as unlevered spot exposure, "
            "prioritize liquidity, 24h volume, spread, trend quality, ATR-based risk, "
            "and relative opportunity cost versus other rotation candidates."
            if len(okx_parts) == 2
            else ""
        )
        return (
            f"The instrument to analyze is `{ticker}`. "
            "This is an OKX market symbol, so use this exact instrument id in every tool call, report, and recommendation. "
            "Preserve the full OKX contract identifier exactly as provided (examples: `BTC-USDT` for spot, `BTC-USDT-SWAP` for perpetual swaps, `BTC-USDT-240628` for dated futures). "
            "Do not rewrite it into an equity ticker, company name, or another exchange format."
            f"{spot_note}"
        )
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
