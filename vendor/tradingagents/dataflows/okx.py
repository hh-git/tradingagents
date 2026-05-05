import io
import json
import os
import time
from datetime import datetime, timedelta
from typing import Annotated

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover - exercised by dependency-failure tests via monkeypatch
    requests = None

from .stockstats_utils import _clean_dataframe

OKX_BASE_URL = os.getenv("OKX_API_BASE_URL", "https://www.okx.com").rstrip("/")
OKX_TIMEOUT = int(os.getenv("OKX_API_TIMEOUT", "15"))
OKX_RETRIES = max(1, int(os.getenv("OKX_API_RETRIES", "3")))
OKX_RETRY_BACKOFF = float(os.getenv("OKX_API_RETRY_BACKOFF", "0.25"))
_OKX_REQUEST_EXCEPTIONS = (requests.RequestException, RuntimeError) if requests else (RuntimeError,)


def _unavailable(subject: str, exc: Exception) -> str:
    return (
        f"{subject} is temporarily unavailable from OKX ({exc}). "
        "Do not infer missing data; base the recommendation only on the remaining evidence."
    )


def _okx_missing_external_context_lines(inst_type: str) -> list[str]:
    lines = [
        "- External news/social availability: not provided by the OKX vendor; treat this section as missing external context, not as a catalyst feed.",
        "- Evidence guardrail: do not upgrade conviction from this proxy alone; require current OHLCV evidence and executable market-structure evidence.",
    ]
    if inst_type == "SPOT":
        lines.append("- Spot execution guardrail: require top-of-book liquidity/spread evidence before Buy or Overweight; otherwise prefer Hold/skip or a wait-for-data trigger.")
    return lines


def _okx_get(path: str, params: dict) -> dict:
    if requests is None:
        raise RuntimeError("The requests package is required for OKX REST data but is not installed")

    filtered_params = {key: value for key, value in params.items() if value not in (None, "")}
    last_exc: Exception | None = None

    for attempt in range(OKX_RETRIES):
        try:
            response = requests.get(
                f"{OKX_BASE_URL}{path}",
                params=filtered_params,
                timeout=OKX_TIMEOUT,
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {response.status_code}")
            response.raise_for_status()
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(f"Failed to decode OKX JSON response for {path}: {exc}") from exc

            if isinstance(payload, dict) and payload.get("code") not in (None, "0"):
                raise RuntimeError(f"OKX API error for {path}: {payload.get('code')} {payload.get('msg')}")
            return payload
        except _OKX_REQUEST_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < OKX_RETRIES - 1:
                time.sleep(OKX_RETRY_BACKOFF * (attempt + 1))

    raise RuntimeError(f"OKX request failed for {path} after {OKX_RETRIES} attempts: {last_exc}")


def _option_value(args: list[str], option: str, default=None):
    if option not in args:
        return default
    index = args.index(option)
    if index + 1 >= len(args):
        return default
    return args[index + 1]


def _run_okx_command(args: list[str]) -> dict:
    """Compatibility shim for the initial OKX integration command shape."""
    if len(args) >= 3 and args[:2] == ["market", "candles"]:
        return _okx_get(
            "/api/v5/market/candles",
            {
                "instId": args[2],
                "bar": _option_value(args, "--bar", "1D"),
                "limit": _option_value(args, "--limit", "100"),
            },
        )
    if len(args) >= 3 and args[:2] == ["market", "ticker"]:
        return _okx_get("/api/v5/market/ticker", {"instId": args[2]})
    if len(args) >= 3 and args[:2] == ["market", "books"]:
        return _okx_get(
            "/api/v5/market/books",
            {
                "instId": args[2],
                "sz": _option_value(args, "--sz", "5"),
            },
        )
    if len(args) >= 2 and args[:2] in (["market", "instruments"], ["public", "instruments"]):
        return _okx_get(
            "/api/v5/public/instruments",
            {
                "instType": _option_value(args, "--instType"),
                "instId": _option_value(args, "--instId"),
            },
        )
    if len(args) >= 2 and args[:2] == ["public", "funding-rate"]:
        return _okx_get("/api/v5/public/funding-rate", {"instId": _option_value(args, "--instId")})
    if len(args) >= 2 and args[:2] == ["public", "open-interest"]:
        return _okx_get(
            "/api/v5/public/open-interest",
            {
                "instType": _option_value(args, "--instType"),
                "instId": _option_value(args, "--instId"),
            },
        )
    raise RuntimeError(f"Unsupported OKX command adapter args: {' '.join(args)}")


def _extract_data(payload: dict):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _instrument_type(inst_id: str) -> str:
    inst_id = inst_id.strip().upper()
    if inst_id.endswith("-SWAP"):
        return "SWAP"
    parts = inst_id.split("-")
    if len(parts) >= 3 and parts[-1].isdigit() and len(parts[-1]) == 6:
        return "FUTURES"
    return "SPOT"


def _instrument_label(inst_id: str) -> str:
    inst_type = _instrument_type(inst_id)
    if inst_type == "SPOT":
        return f"OKX spot pair {inst_id}"
    if inst_type == "SWAP":
        return f"OKX perpetual swap {inst_id}"
    return f"OKX futures contract {inst_id}"


def _parse_ts_ms(value: str) -> datetime:
    return datetime.utcfromtimestamp(int(value) / 1000)


def _format_ts_ms(value: str) -> str:
    if not value:
        return ""
    return _parse_ts_ms(value).strftime("%Y-%m-%d %H:%M:%S UTC")


def _empty_ohlcv_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])


def _safe_float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_candles(rows: list[list[str]]) -> pd.DataFrame:
    if not rows:
        return _empty_ohlcv_frame()
    normalized = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts, o, h, l, c = row[:5]
        vol = row[5] if len(row) > 5 else "0"
        parsed = {
            "Open": _safe_float(o),
            "High": _safe_float(h),
            "Low": _safe_float(l),
            "Close": _safe_float(c),
            "Volume": _safe_float(vol, 0.0),
        }
        if any(parsed[key] is None for key in ("Open", "High", "Low", "Close", "Volume")):
            continue
        try:
            date = _parse_ts_ms(ts)
        except (TypeError, ValueError, OverflowError):
            continue
        normalized.append(
            {
                "Date": date,
                **parsed,
            }
        )
    if not normalized:
        return _empty_ohlcv_frame()
    df = pd.DataFrame(normalized)
    df = df.sort_values("Date").reset_index(drop=True)
    return _clean_dataframe(df)


def _first_data_item(payload: dict) -> dict:
    data = _extract_data(payload)
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def _derivative_market_lines(ticker: str, inst_type: str) -> list[str]:
    if inst_type not in ("SWAP", "FUTURES"):
        return []

    lines = ["", "### Derivatives Market Structure"]
    try:
        payload = _run_okx_command(["public", "open-interest", "--instType", inst_type, "--instId", ticker])
        item = _first_data_item(payload)
        if item:
            lines.extend([
                f"- openInterest: {item.get('oi')}",
                f"- openInterestCcy: {item.get('oiCcy')}",
                f"- openInterestUsd: {item.get('oiUsd')}",
                f"- openInterestTs: {_format_ts_ms(item.get('ts'))}",
            ])
    except Exception as exc:
        lines.append(f"- openInterest: unavailable ({exc})")

    if inst_type == "SWAP":
        try:
            payload = _run_okx_command(["public", "funding-rate", "--instId", ticker])
            item = _first_data_item(payload)
            if item:
                lines.extend([
                    f"- fundingRate: {item.get('fundingRate')}",
                    f"- nextFundingRate: {item.get('nextFundingRate')}",
                    f"- fundingTime: {_format_ts_ms(item.get('fundingTime'))}",
                    f"- nextFundingTime: {_format_ts_ms(item.get('nextFundingTime'))}",
                ])
        except Exception as exc:
            lines.append(f"- fundingRate: unavailable ({exc})")

    return lines


def _spot_market_lines(ticker: str, inst_type: str) -> list[str]:
    if inst_type != "SPOT":
        return []

    lines = ["", "### Spot Rotation Market Structure"]
    try:
        item = _first_data_item(_run_okx_command(["market", "ticker", ticker]))
        if item:
            lines.extend([
                f"- last: {item.get('last')}",
                f"- 24h high: {item.get('high24h')}",
                f"- 24h low: {item.get('low24h')}",
                f"- 24h volume base: {item.get('vol24h')}",
                f"- 24h volume quote: {item.get('volCcy24h')}",
                f"- snapshotTs: {_format_ts_ms(item.get('ts'))}",
            ])
        else:
            lines.append("- ticker: unavailable (no ticker snapshot returned)")
    except Exception as exc:
        lines.append(f"- ticker: unavailable ({exc})")

    try:
        book = _first_data_item(_run_okx_command(["market", "books", ticker, "--sz", "5"]))
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if bids and asks:
            bid = _safe_float(bids[0][0])
            ask = _safe_float(asks[0][0])
            bid_size = _safe_float(bids[0][1], 0.0) if len(bids[0]) > 1 else 0.0
            ask_size = _safe_float(asks[0][1], 0.0) if len(asks[0]) > 1 else 0.0
            if bid is None or ask is None:
                lines.append("- orderBook: unavailable (malformed top-of-book prices)")
            else:
                mid = (bid + ask) / 2
                spread_pct = ((ask - bid) / mid * 100) if mid else 0
                lines.extend([
                    f"- bestBid: {bid}",
                    f"- bestAsk: {ask}",
                    f"- bestBidSize: {bid_size}",
                    f"- bestAskSize: {ask_size}",
                    f"- quotedSpreadPct: {round(spread_pct, 4)}",
                    f"- bookTs: {_format_ts_ms(book.get('ts'))}",
                ])
        else:
            lines.append("- orderBook: unavailable (no bid/ask depth returned)")
    except Exception as exc:
        lines.append(f"- orderBook: unavailable ({exc})")

    lines.append("- Spot workflow note: size entries from available liquidity, quoted spread, and volatility; do not use funding or open interest as spot evidence.")
    lines.append("- Spot recommendation guardrail: if current OHLCV, ticker snapshot, or top-of-book spread/liquidity is unavailable, lower conviction and prefer Hold/skip or wait for a defined data refresh trigger.")
    return lines


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _format_indicator_series(name: str, start: datetime, end: datetime, values: list[tuple[str, str]], description: str) -> str:
    body = "".join(f"{date}: {value}\n" for date, value in values)
    return f"## {name} values from {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}:\n\n{body}\n\n{description}"


def get_okx_candles(
    inst_id: Annotated[str, "OKX instrument id such as BTC-USDT or BTC-USDT-SWAP"],
    bar: Annotated[str, "OKX candle bar, e.g. 1H, 4H, 1D"] = "1D",
    limit: Annotated[int, "Number of rows to fetch"] = 100,
) -> pd.DataFrame:
    inst_id = inst_id.strip().upper()
    payload = _run_okx_command(["market", "candles", inst_id, "--bar", bar, "--limit", str(limit)])
    rows = _extract_data(payload)
    return _normalize_candles(rows)


def get_okx_data_online(
    symbol: Annotated[str, "OKX instrument id such as BTC-USDT or BTC-USDT-SWAP"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):
    symbol = symbol.strip().upper()
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        return f"Invalid date for OKX data request on {symbol}: {exc}"
    lookback_days = max(5, (end_dt - start_dt).days + 10)
    bar = "1D"
    if (end_dt - start_dt).days <= 3:
        bar = "1H"
        lookback_days = max(48, ((end_dt - start_dt).days + 2) * 24)

    try:
        df = get_okx_candles(symbol, bar=bar, limit=min(lookback_days, 300))
    except Exception as exc:
        return _unavailable(f"OKX OHLCV data for {symbol}", exc)
    if df.empty:
        return f"No OKX data found for symbol '{symbol}' between {start_date} and {end_date}"

    filtered = df[(df["Date"] >= pd.Timestamp(start_dt)) & (df["Date"] <= pd.Timestamp(end_dt) + pd.Timedelta(days=1))]
    if filtered.empty:
        filtered = df.tail(min(len(df), 30))

    filtered = filtered.copy()
    filtered["Date"] = filtered["Date"].dt.strftime("%Y-%m-%d %H:%M:%S")

    csv_buffer = io.StringIO()
    filtered.to_csv(csv_buffer, index=False)
    header = f"# { _instrument_label(symbol) } data from {start_date} to {end_date}\n"
    header += f"# Total records: {len(filtered)}\n"
    header += f"# Data retrieved on: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
    return header + csv_buffer.getvalue()


def get_okx_indicator_window(
    symbol: Annotated[str, "OKX instrument id"],
    indicator: Annotated[str, "technical indicator name"],
    curr_date: Annotated[str, "current trading date YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    symbol = symbol.strip().upper()
    try:
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    except ValueError as exc:
        return f"Invalid date for OKX indicator request on {symbol}: {exc}"
    start_dt = curr_dt - timedelta(days=look_back_days)
    bar = "4H" if look_back_days <= 45 else "1D"
    limit = max(60, min(300, look_back_days * (6 if bar == "4H" else 2)))
    try:
        df = get_okx_candles(symbol, bar=bar, limit=limit)
    except Exception as exc:
        return _unavailable(f"OKX {indicator} indicator data for {symbol}", exc)
    if df.empty:
        return f"No OKX OHLCV data available for {symbol}"

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    volume = df["Volume"].astype(float)

    indicator = indicator.lower().strip()
    descriptions = {
        "rsi": "RSI: momentum oscillator; values above 70 can indicate overbought conditions and below 30 oversold conditions.",
        "macd": "MACD: trend-following momentum indicator based on EMA spreads; useful for momentum shifts and crossovers.",
        "macds": "MACD Signal: smoothed MACD line; compare against MACD for crossover signals.",
        "macdh": "MACD Histogram: distance between MACD and signal line; useful for momentum acceleration/deceleration.",
        "close_10_ema": "10 EMA: responsive short-term trend measure.",
        "close_50_sma": "50 SMA: medium-term trend benchmark.",
        "close_200_sma": "200 SMA: long-term trend benchmark.",
        "boll": "Bollinger middle band: 20-period simple moving average.",
        "boll_ub": "Bollinger upper band: middle band + 2 standard deviations.",
        "boll_lb": "Bollinger lower band: middle band - 2 standard deviations.",
        "atr": "ATR: average true range; a measure of volatility.",
        "vwma": "VWMA: volume-weighted moving average.",
    }

    if indicator == "rsi":
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        # A monotonic uptrend should yield RSI near 100 rather than all-NaN.
        # Treat zero average loss as an infinite RS unless gains are also zero.
        rs = gain / loss.replace(0, pd.NA)
        series = 100 - (100 / (1 + rs))
        series = series.mask((loss == 0) & (gain > 0), 100.0)
        series = series.mask((loss == 0) & (gain == 0), 50.0)
    elif indicator == "macd":
        series = _ema(close, 12) - _ema(close, 26)
    elif indicator == "macds":
        macd = _ema(close, 12) - _ema(close, 26)
        series = _ema(macd, 9)
    elif indicator == "macdh":
        macd = _ema(close, 12) - _ema(close, 26)
        signal = _ema(macd, 9)
        series = macd - signal
    elif indicator == "close_10_ema":
        series = _ema(close, 10)
    elif indicator == "close_50_sma":
        series = close.rolling(50).mean()
    elif indicator == "close_200_sma":
        series = close.rolling(200).mean()
    elif indicator == "boll":
        series = close.rolling(20).mean()
    elif indicator == "boll_ub":
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        series = mid + 2 * std
    elif indicator == "boll_lb":
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        series = mid - 2 * std
    elif indicator == "atr":
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        series = tr.rolling(14).mean()
    elif indicator == "vwma":
        series = (close * volume).rolling(20).sum() / volume.rolling(20).sum()
    else:
        return (
            f"Indicator {indicator} is not supported for OKX vendor. "
            f"Supported indicators: {sorted(descriptions.keys())}. "
            "Select one of the supported OKX indicators instead of inferring this signal."
        )

    values = []
    for date, value in zip(df["Date"], series):
        if pd.isna(value):
            continue
        if start_dt <= pd.Timestamp(date).to_pydatetime() <= curr_dt + timedelta(days=1):
            values.append((pd.Timestamp(date).strftime("%Y-%m-%d"), str(round(float(value), 6))))

    if not values:
        return f"No computed {indicator} values available for {symbol} in the requested window"

    return _format_indicator_series(indicator, start_dt, curr_dt, values, descriptions[indicator])


def get_okx_fundamentals(
    ticker: Annotated[str, "OKX instrument id"],
    curr_date: Annotated[str, "current date"] = None,
):
    ticker = ticker.strip().upper()
    inst_type = _instrument_type(ticker)
    try:
        payload = _run_okx_command(["public", "instruments", "--instType", inst_type, "--instId", ticker])
    except Exception as exc:
        if inst_type == "SPOT":
            lines = [
                f"## OKX Instrument Profile: {ticker}",
                f"- instrument metadata: temporarily unavailable from OKX ({exc})",
                "- Metadata gap handling: do not infer missing tick size, lot size, minimum size, or listing state.",
            ]
            lines.extend(_spot_market_lines(ticker, inst_type))
            lines.append("- Note: Treat this as a lower-confidence spot rotation input until instrument metadata is available.")
            return "\n".join(lines)
        return _unavailable(f"OKX instrument metadata for {ticker}", exc)
    data = _extract_data(payload)
    if not data:
        return f"No instrument metadata found for {ticker}"
    item = data[0]
    keys = [
        "instId", "instType", "baseCcy", "quoteCcy", "settleCcy", "state",
        "tickSz", "lotSz", "minSz", "lever", "ctVal", "ctMult", "ctType",
        "instFamily", "uly", "listTime", "expTime"
    ]
    lines = [f"## OKX Instrument Profile: {ticker}"]
    for key in keys:
        value = item.get(key)
        if value in (None, ""):
            continue
        if key in ("listTime", "expTime"):
            value = _format_ts_ms(value)
        lines.append(f"- {key}: {value}")
    lines.extend(_spot_market_lines(ticker, inst_type))
    lines.extend(_derivative_market_lines(ticker, inst_type))
    lines.append("- Note: For crypto spot and derivatives, exchange metadata replaces equity-style corporate fundamentals.")
    return "\n".join(lines)


def get_okx_balance_sheet(ticker: str, freq: str = None, curr_date: str = None):
    return f"Balance-sheet style corporate filings are not available for OKX crypto instrument {ticker}. Use get_okx_fundamentals for exchange metadata and derivatives positioning tools for leverage context."


def get_okx_cashflow(ticker: str, freq: str = None, curr_date: str = None):
    return f"Cashflow statements are not applicable to OKX crypto instrument {ticker}. Use market structure, funding, open interest, and liquidity data instead."


def get_okx_income_statement(ticker: str, freq: str = None, curr_date: str = None):
    return f"Income statements are not applicable to OKX crypto instrument {ticker}. Use exchange metadata and price/volume/funding context instead."


def get_okx_news(ticker: str, start_date: str, end_date: str) -> str:
    ticker = ticker.strip().upper()
    inst_type = _instrument_type(ticker)
    try:
        payload = _run_okx_command(["market", "ticker", ticker])
        data = _extract_data(payload)
        item = data[0] if data else {}
        lines = [f"## OKX Market Snapshot News Proxy for {ticker}"]
        if item:
            lines.extend([
                f"- last: {item.get('last')}",
                f"- 24h high: {item.get('high24h')}",
                f"- 24h low: {item.get('low24h')}",
                f"- 24h vol base: {item.get('vol24h')}",
                f"- 24h vol quote: {item.get('volCcy24h')}",
                f"- ts: {item.get('ts')}",
            ])
        else:
            lines.append("- ticker snapshot: unavailable (no OKX ticker data returned)")
        lines.extend(_okx_missing_external_context_lines(inst_type))
        return "\n".join(lines)
    except Exception as exc:
        return "\n".join([
            _unavailable(f"OKX market snapshot news proxy for {ticker}", exc),
            *_okx_missing_external_context_lines(inst_type),
        ])


def get_okx_global_news(curr_date: str, look_back_days: int = 7, limit: int = 10) -> str:
    lines = [
        f"## OKX Global Market Context as of {curr_date}",
        "",
        "- External macro news/social availability: not provided by the OKX vendor.",
        "- Treat macro/news/social context as missing unless another configured vendor supplies it.",
        "- Evidence guardrail: for OKX-focused analysis, rely on ticker, candles, order book, funding, and open-interest tools; do not issue Buy or Overweight from absent external context.",
    ]
    return "\n".join(lines)


def get_okx_insider_transactions(symbol: str) -> str:
    return f"Insider transaction data is not applicable to OKX crypto instrument {symbol}."
