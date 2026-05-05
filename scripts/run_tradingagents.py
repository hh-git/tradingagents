#!/usr/bin/env python3
"""Run the standalone skill-local TradingAgentsGraph against OKX through Hermes.

The complete TradingAgents runtime is vendored under this skill directory, so
this wrapper must not import from or locate a separate local repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BACKEND_URL = "https://api.86gamestore.com/v1"
DEFAULT_DEEP_MODEL = "gpt-5.5"
DEFAULT_QUICK_MODEL = "gpt-5.4-mini"
USER_AGENT = "HermesAgent/0.12.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run TradingAgentsGraph with Hermes/OKX defaults.")
    parser.add_argument("--instrument", required=True, help="OKX instrument ID, e.g. BTC-USDT or BTC-USDT-SWAP.")
    parser.add_argument("--date", required=True, help="Analysis date passed to TradingAgentsGraph, YYYY-MM-DD.")
    parser.add_argument("--output-language", required=True, help="Output language, e.g. Chinese or English.")
    parser.add_argument("--deep-model", help=f"Deep-think model. Default probes then prefers {DEFAULT_DEEP_MODEL}.")
    parser.add_argument("--quick-model", help=f"Quick-think model. Default probes then prefers {DEFAULT_QUICK_MODEL}.")
    parser.add_argument("--backend-url", default=BACKEND_URL, help=f"Hermes-compatible base URL. Default: {BACKEND_URL}.")
    parser.add_argument("--max-debate-rounds", type=int, default=1)
    parser.add_argument("--max-risk-rounds", type=int, default=1)
    parser.add_argument("--analysts", default="market,social,news,fundamentals")
    parser.add_argument("--debug", action="store_true", help="Enable TradingAgentsGraph debug mode.")
    return parser.parse_args()


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def vendored_root() -> Path:
    return skill_root() / "vendor"


def ensure_vendored_code_on_syspath() -> Path:
    root = vendored_root()
    package_init = root / "tradingagents" / "__init__.py"
    if not package_init.exists():
        raise RuntimeError(
            f"Standalone TradingAgents runtime is missing: {package_init}. "
            "Reinstall the complete tradingagents skill payload, including vendor/."
        )
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def load_hermes_api_key() -> str | None:
    env_key = os.environ.get("HERMES_API_KEY")
    if env_key:
        return env_key

    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return None

    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("api_key:") or stripped.startswith("HERMES_API_KEY:"):
                key = stripped.split(":", 1)[1].strip().strip("'\"")
                if key:
                    os.environ["HERMES_API_KEY"] = key
                    return key
    except OSError as exc:
        print(f"warning: could not read {config_path}: {exc}", file=sys.stderr)

    return None


def probe_models(backend_url: str, api_key: str) -> list[str]:
    request = urllib.request.Request(
        backend_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"/models probe failed: HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"/models probe failed: {exc}") from exc

    models = payload.get("data", [])
    ids = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
    return sorted(set(ids))


def probe_models_with_retry(backend_url: str, api_key: str, attempts: int = 2) -> list[str]:
    last_error: RuntimeError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return probe_models(backend_url, api_key)
        except RuntimeError as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(f"warning: /models probe attempt {attempt} failed: {exc}", file=sys.stderr)
    assert last_error is not None
    raise last_error


def pick_model(requested: str | None, available: list[str], preferred: str) -> str:
    if requested:
        if available and requested not in available:
            print(f"warning: requested model {requested!r} was not returned by /models", file=sys.stderr)
        return requested
    if preferred in available:
        return preferred
    if available:
        return available[0]
    return preferred


def compact(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 1200 else value[:1200] + "...<truncated>"
    if isinstance(value, dict):
        return {key: compact(val) for key, val in value.items()}
    if isinstance(value, list):
        return [compact(item) for item in value[:20]]
    return value


def extract_final_decision_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("final_trade_decision", "current_response", "judge_decision"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def summarize_decision(decision_text: str) -> dict[str, str]:
    summary = {"rating": "", "price_target": "", "stop_loss": "", "max_position_size": "", "time_horizon": ""}
    patterns = {
        "rating": r"\*\*Rating\*\*:\s*(.+)",
        "price_target": r"\*\*Price Target\*\*:\s*(.+)",
        "stop_loss": r"\*\*Stop Loss\*\*:\s*(.+)",
        "max_position_size": r"\*\*Max Position Size\*\*:\s*(.+)",
        "time_horizon": r"\*\*Time Horizon\*\*:\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, decision_text)
        if match:
            summary[key] = match.group(1).strip()
    return summary


def main() -> int:
    args = parse_args()
    runtime_root = ensure_vendored_code_on_syspath()

    api_key = load_hermes_api_key()
    if not api_key:
        print("error: HERMES_API_KEY is unset and ~/.hermes/config.yaml did not provide one", file=sys.stderr)
        return 2

    available_models = probe_models_with_retry(args.backend_url, api_key)
    deep_model = pick_model(args.deep_model, available_models, DEFAULT_DEEP_MODEL)
    quick_model = pick_model(args.quick_model, available_models, DEFAULT_QUICK_MODEL)

    from tradingagents.dataflows.config import get_config, set_config
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    config["asset_universe"] = "okx"
    config["llm_provider"] = "hermes"
    config["backend_url"] = args.backend_url
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = quick_model
    config["max_debate_rounds"] = args.max_debate_rounds
    config["max_risk_discuss_rounds"] = args.max_risk_rounds
    config["output_language"] = args.output_language

    # Ensure dataflow tools see the OKX asset universe before the graph starts.
    # Some module-level helpers initialize their global config at import time;
    # setting it here prevents transient fallback to yfinance for OKX instIds.
    set_config(config)

    print("runtime_config:")
    print(json.dumps({
        "skill_root": str(skill_root()),
        "runtime_root": str(runtime_root),
        "standalone": True,
        "asset_universe": config["asset_universe"],
        "llm_provider": config["llm_provider"],
        "backend_url": config["backend_url"],
        "deep_think_llm": config["deep_think_llm"],
        "quick_think_llm": config["quick_think_llm"],
        "max_debate_rounds": config["max_debate_rounds"],
        "max_risk_discuss_rounds": config["max_risk_discuss_rounds"],
        "output_language": config["output_language"],
        "models_seen": available_models,
    }, ensure_ascii=False, indent=2))

    print("data_vendors_before_graph:")
    print(json.dumps(get_config().get("data_vendors", {}), ensure_ascii=False, indent=2, default=str))

    analysts = [item.strip() for item in args.analysts.split(",") if item.strip()]
    graph = TradingAgentsGraph(selected_analysts=analysts, debug=args.debug, config=config)
    state, decision = graph.propagate(args.instrument, args.date)

    print("data_vendors_after_graph:")
    print(json.dumps(get_config().get("data_vendors", {}), ensure_ascii=False, indent=2, default=str))
    print("state_keys:")
    print(json.dumps(sorted(state.keys()), ensure_ascii=False, indent=2))
    print("final_trade_decision:")
    print(state.get("final_trade_decision") or decision)
    decision_text = extract_final_decision_text(state.get("final_trade_decision") or decision)
    print("decision_summary_json:")
    print(json.dumps(summarize_decision(decision_text), ensure_ascii=False, indent=2))
    print("selected_state:")
    selected = {
        key: compact(state.get(key))
        for key in (
            "company_of_interest",
            "trade_date",
            "market_report",
            "sentiment_report",
            "news_report",
            "fundamentals_report",
            "investment_debate_state",
            "risk_debate_state",
            "final_trade_decision",
        )
        if key in state
    }
    print(json.dumps(selected, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

