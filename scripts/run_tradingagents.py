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
ENV_REF_RE = re.compile(r"\${([^}]+)}")
PLACEHOLDER_SECRET_VALUES = {
    "*",
    "**",
    "***",
    "changeme",
    "dummy",
    "example",
    "none",
    "null",
    "placeholder",
    "your-api-key",
    "your_api_key",
}


def parse_args() -> argparse.Namespace:
    config = load_yaml_config(Path.home() / ".hermes" / "config.yaml") or {}
    dotenv_values = read_hermes_dotenv(Path.home() / ".hermes" / "config.yaml")
    model_config = config.get("model") if isinstance(config.get("model"), dict) else {}
    configured_backend_url = normalize_base_url(model_config.get("base_url"), dotenv_values) if isinstance(model_config, dict) else ""

    parser = argparse.ArgumentParser(description="Run TradingAgentsGraph with Hermes/OKX defaults.")
    parser.add_argument("--instrument", required=True, help="OKX instrument ID, e.g. BTC-USDT or BTC-USDT-SWAP.")
    parser.add_argument("--date", required=True, help="Analysis date passed to TradingAgentsGraph, YYYY-MM-DD.")
    parser.add_argument("--output-language", required=True, help="Output language, e.g. Chinese or English.")
    parser.add_argument("--deep-model", help=f"Deep-think model. Default probes then prefers {DEFAULT_DEEP_MODEL}.")
    parser.add_argument("--quick-model", help=f"Quick-think model. Default probes then prefers {DEFAULT_QUICK_MODEL}.")
    parser.add_argument(
        "--backend-url",
        default=configured_backend_url or BACKEND_URL,
        help=f"Hermes-compatible base URL. Default: configured model.base_url or {BACKEND_URL}.",
    )
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


def read_hermes_dotenv(config_path: Path) -> dict[str, str]:
    env_path = config_path.with_name(".env")
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def expand_env_refs(value: str, dotenv_values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, dotenv_values.get(name, match.group(0)))

    return ENV_REF_RE.sub(replace, value)


def has_usable_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < 4:
        return False
    if ENV_REF_RE.search(cleaned):
        return False
    if cleaned.lower() in PLACEHOLDER_SECRET_VALUES:
        return False
    return True


def secret_from_value(value: Any, dotenv_values: dict[str, str]) -> str:
    if not isinstance(value, str):
        return ""
    candidate = expand_env_refs(value.strip(), dotenv_values).strip()
    return candidate if has_usable_secret(candidate) else ""


def secret_from_entry(entry: dict[str, Any], dotenv_values: dict[str, str]) -> str:
    for key in ("api_key", "apiKey"):
        candidate = secret_from_value(entry.get(key), dotenv_values)
        if candidate:
            return candidate

    for key in ("key_env", "api_key_env", "keyEnv", "apiKeyEnv"):
        env_name = entry.get(key)
        if not isinstance(env_name, str) or not env_name.strip():
            continue
        candidate = os.environ.get(env_name.strip(), dotenv_values.get(env_name.strip(), ""))
        if has_usable_secret(candidate):
            return candidate.strip()

    return ""


def load_yaml_config(config_path: Path) -> dict[str, Any] | None:
    try:
        import yaml
    except ImportError:
        print("warning: PyYAML is unavailable; cannot read Hermes config.yaml", file=sys.stderr)
        return None

    if not config_path.exists():
        return None

    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"warning: could not read {config_path}: {exc}", file=sys.stderr)
        return None
    except Exception as exc:
        print(
            f"warning: could not parse {config_path} as YAML ({type(exc).__name__})",
            file=sys.stderr,
        )
        return None

    return loaded if isinstance(loaded, dict) else None


def normalize_provider_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace(" ", "-")


def normalize_base_url(value: Any, dotenv_values: dict[str, str]) -> str:
    if not isinstance(value, str):
        return ""
    return expand_env_refs(value.strip(), dotenv_values).rstrip("/")


def provider_match_score(
    entry: dict[str, Any],
    active_provider: str,
    active_base_url: str,
    dotenv_values: dict[str, str],
    provider_key: str = "",
) -> int:
    names = {
        normalize_provider_name(provider_key),
        normalize_provider_name(entry.get("provider_key")),
        normalize_provider_name(entry.get("name")),
        normalize_provider_name(entry.get("provider")),
    }
    names.discard("")

    aliases = set(names)
    aliases.update(f"custom:{name}" for name in names)

    provider_matches = bool(active_provider and active_provider in aliases)
    entry_base_url = (
        normalize_base_url(entry.get("base_url"), dotenv_values)
        or normalize_base_url(entry.get("url"), dotenv_values)
        or normalize_base_url(entry.get("api"), dotenv_values)
    )
    base_url_matches = bool(active_base_url and entry_base_url and active_base_url == entry_base_url)

    if provider_matches and base_url_matches:
        return 3
    if provider_matches:
        return 2
    if base_url_matches:
        return 1
    return 0


def iter_configured_provider_entries(config: dict[str, Any]):
    providers = config.get("providers")
    if isinstance(providers, dict):
        for provider_key, entry in providers.items():
            if isinstance(entry, dict):
                yield str(provider_key), entry

    custom_providers = config.get("custom_providers")
    if isinstance(custom_providers, list):
        for entry in custom_providers:
            if isinstance(entry, dict):
                yield "", entry
    elif isinstance(custom_providers, dict):
        for provider_key, entry in custom_providers.items():
            if isinstance(entry, dict):
                yield str(provider_key), entry


def configured_provider_api_key(
    config: dict[str, Any],
    active_provider: str,
    active_base_url: str,
    dotenv_values: dict[str, str],
) -> str:
    best_score = 0
    best_key = ""

    for provider_key, entry in iter_configured_provider_entries(config):
        api_key = secret_from_entry(entry, dotenv_values)
        if not api_key:
            continue
        score = provider_match_score(
            entry,
            active_provider,
            active_base_url,
            dotenv_values,
            provider_key=provider_key,
        )
        if score > best_score:
            best_score = score
            best_key = api_key

    return best_key


def load_hermes_api_key(config_path: Path | None = None) -> str | None:
    env_key = os.environ.get("HERMES_API_KEY")
    if env_key:
        return env_key

    config_path = config_path or Path.home() / ".hermes" / "config.yaml"
    config = load_yaml_config(config_path)
    if not config:
        return None

    dotenv_values = read_hermes_dotenv(config_path)
    model = config.get("model")
    if isinstance(model, dict):
        api_key = secret_from_entry(model, dotenv_values)
        if api_key:
            os.environ["HERMES_API_KEY"] = api_key
            return api_key

        active_provider = normalize_provider_name(model.get("provider"))
        active_base_url = normalize_base_url(model.get("base_url"), dotenv_values)
    else:
        active_provider = ""
        active_base_url = ""

    api_key = configured_provider_api_key(
        config,
        active_provider,
        active_base_url,
        dotenv_values,
    )
    if api_key:
        os.environ["HERMES_API_KEY"] = api_key
        return api_key

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
