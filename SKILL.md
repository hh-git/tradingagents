---
name: tradingagents
description: Use when the user wants to run a standalone packaged TradingAgentsGraph skill for OKX spot, swap, or futures analysis through a Hermes/OpenAI-compatible backend, without depending on a local TradingAgents repository.
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [tradingagents, okx, hermes, analysis, spot, swap, futures, multi-agent, standalone]
    related_skills: [codex, hermes-agent, okx-cex-market]
---

# TradingAgents

## Overview

Run a standalone, skill-local TradingAgentsGraph runtime for deep OKX analysis.
Use this for graph-based spot, perpetual/swap, or dated-futures research, not for order execution.

This skill is self-contained: the runtime package is vendored under `vendor/` inside the skill directory. The launcher must not discover, import, or call `/home/chux/workspace/TradingAgents` or any other external TradingAgents repository at runtime.

## Standalone Layout

Installed skill layout:

- `SKILL.md` — Hermes skill definition
- `scripts/run_tradingagents.py` — standalone launcher
- `vendor/tradingagents/` — vendored TradingAgents runtime package
- `vendor/cli/` — vendored CLI support package when needed by runtime imports
- `requirements.txt` — Python dependencies required by the vendored runtime
- `references/usage.md` — install and operator notes
- `references/standalone-packaging.md` — refactor checklist and verification commands for converting/re-validating the standalone packaged skill

Install destination on this machine:

- `~/.hermes/skills/okx-agent-skills/tradingagents`

The installed skill must be runnable after copying this directory elsewhere.

## When to Use

- The user asks for TradingAgentsGraph or multi-agent trading analysis.
- The user wants deep OKX analysis for spot, swap/perpetual, or dated futures.
- The user wants a portable TradingAgents Hermes skill that can be copied or packaged independently.
- Do not use for order placement, account operations, or quick one-shot market lookups.

## Workflow

1. Normalize the instrument:
   - symbol only -> `<ASSET>-USDT`
   - swap/perp/contract -> `<ASSET>-USDT-SWAP`
   - dated futures only when explicitly requested

2. Run the installed skill launcher directly:

```bash
python ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py \
  --instrument ETH-USDT \
  --date 2026-05-03 \
  --output-language Chinese
```

3. If running from a packaged checkout before installation:

```bash
python hermes-skill/tradingagents/scripts/run_tradingagents.py \
  --instrument ETH-USDT \
  --date 2026-05-03 \
  --output-language Chinese
```

4. Optional knobs:
   - `--deep-model`
   - `--quick-model`
   - `--backend-url`
   - `--max-debate-rounds`
   - `--max-risk-rounds`
   - `--analysts`
   - `--debug`

5. Use `final_trade_decision` as the primary conclusion. When available, also inspect `decision_summary_json` for rating, target, stop, size, and horizon fields.

## Hot-Contract Workflow

When the user asks to analyze OKX "hot" contracts by rank (for example "热度8-10的合约"), interpret hotness as OKX SWAP 24h USD turnover unless the user specifies another ranking metric.

1. Discover candidates with OKX market data first:

```bash
export PATH="$HOME/.hermes/node/bin:$PATH"
okx market filter --instType SWAP --sortBy volUsd24h --sortOrder desc --limit 10 --json
```

2. Select the requested rank slice from the returned `rows[*].rank`, preserving exact `instId` values such as `LINK-USDT-SWAP`.
3. Run the standalone skill launcher with the skill virtualenv when present:

```bash
SKILL="$HOME/.hermes/skills/okx-agent-skills/tradingagents"
PY="$SKILL/.venv/bin/python"
[ -x "$PY" ] || PY=python3
OUT="$HOME/.hermes/tmp/tradingagents-hot-$(date +%F)"
mkdir -p "$OUT"
"$PY" "$SKILL/scripts/run_tradingagents.py" \
  --instrument LINK-USDT-SWAP \
  --date "$(date +%F)" \
  --output-language Chinese \
  --max-debate-rounds 1 \
  --max-risk-rounds 1 \
  --analysts market,social,news,fundamentals \
  2>&1 | tee "$OUT/LINK-USDT-SWAP.log"
```

4. Summarize `final_trade_decision` and `decision_summary_json`, plus rank, last price, 24h change, `volUsd24h`, `oiUsd`, and funding rate from the market screener.

### Fallback when `market filter` fails

If `okx market filter --instType SWAP --sortBy volUsd24h ...` fails due to transient network/API issues, do **not** stop the whole workflow immediately. Use this fallback:

1. Fetch the full swap ticker set instead:

```bash
okx market tickers SWAP --json
```

2. Restrict to the target universe explicitly (for most OKX hot-contract tasks, prefer `*-USDT-SWAP` unless the user asked for inverse/USD contracts or other settlement types).
3. Approximate 24h USD turnover as `last * volCcy24h` for each returned row, because ticker output does not include `volUsd24h` directly.
4. Sort descending by that approximation and clearly label the result as a **fallback approximation**, not the exact `market filter` ranking.
5. Only feed exact `instId` values from that fallback ranking into TradingAgents.

This keeps scheduled jobs moving when the screener endpoint is temporarily unavailable, while preserving an audit trail that the rank source was approximate rather than the canonical filter endpoint.

## Runtime Facts

- standalone runtime root: `<skill_dir>/vendor`
- the standalone skill may need a local virtualenv at `<skill_dir>/.venv`; if `ModuleNotFoundError` occurs for runtime deps such as `langgraph`, create/use that venv and install `requirements.txt` before retrying:
  `python3 -m venv ~/.hermes/skills/okx-agent-skills/tradingagents/.venv && ~/.hermes/skills/okx-agent-skills/tradingagents/.venv/bin/python -m pip install -r ~/.hermes/skills/okx-agent-skills/tradingagents/requirements.txt`
- for hot-contract rank requests, use OKX `market filter --instType SWAP --sortBy volUsd24h --sortOrder desc` to identify the contracts first, then feed the exact SWAP instIds into TradingAgents
- if a TradingAgents batch appears idle with little stdout, inspect the per-instrument log files and child process tree rather than assuming it is stuck; graph output often arrives in large chunks after long model calls
- backend_url default: `https://api.86gamestore.com/v1`
- asset_universe: `okx`
- default deep model: `gpt-5.5`
- default quick model: `gpt-5.4-mini`
- probe `/models` before trusting model IDs
- if `HERMES_API_KEY` is unset, the helper attempts to load it from `~/.hermes/config.yaml`
- the wrapper injects `<skill_dir>/vendor` into `sys.path` before importing `tradingagents`
- `--repo-root` and `$TRADINGAGENTS_REPO_ROOT` are intentionally unsupported because this skill must be standalone
- before graph startup, the launcher must call `tradingagents.dataflows.config.set_config(config)` after setting `asset_universe="okx"`; otherwise module-level dataflow config can remain on yfinance and OKX instIds may trigger yfinance “possibly delisted” messages
- OKX crypto runs should skip the equity-only pending outcome resolver that uses yfinance/SPY; otherwise past pending entries for OKX instIds can cause yfinance 404/no-timezone noise before the graph runs
- in cron-style hot-contract workflows, treat `final_trade_decision` / `decision_summary_json` as the authoritative summary and ignore upstream `market_report` / debate-stage BUY noise when the final rating is weaker (for example `Hold` or `Underweight`)
- when the rank-8-10 workflow says to use Finance MCP but MCP tools are unavailable, use the local Finance-Tracker service/SQLite in **read-only fallback mode** to confirm platform existence and gather historical overview/stats; do not create or mutate records unless an approved writer path is available or a real trade must be recorded through the supported service layer
- before trimming an existing demo position on an `Underweight` conclusion, compare the current notional against the strategy cap and the recommendation strength; if the position is already small and the recommendation is only a moderate de-risking signal rather than a hard exit, it is acceptable to skip trading and report that no new executable action met the rules
- in cron-style OKX **SPOT** top-volume workflows, if TradingAgents cannot produce `final_trade_decision` / `decision_summary_json` because the backend `/models` probe returns `HTTP 401 INVALID_API_KEY`, treat the analysis as unavailable and stop all new trade execution. Do **not** substitute ad-hoc directional guesses from price action or sentiment to force a trade.
- for OKX **SPOT** hot-list workflows, prefer `okx market filter --instType SPOT --quoteCcy USDT --sortBy volUsd24h --sortOrder desc` so the universe stays in tradeable USDT spot pairs; metals/tokenized assets (for example `XAUT-USDT`, `PAXG-USDT`) may still appear and should be reported as returned by the screener unless the task explicitly excludes them
- before reporting account context for demo SPOT cron runs, read `okx --profile demo account balance USDT --json`, `okx --profile demo account balance --json`, `okx --profile demo account asset-balance --valuation --json`, `okx --profile demo spot orders --json`, and `okx --profile demo spot fills --json`; use the combination to verify available USDT, holdings, open orders, and recent fills before deciding whether a sell is actually allowed

## Dependency Setup

The skill vendors the TradingAgents source code, not third-party Python wheels. Install dependencies into the Python environment used to run the skill:

```bash
python -m pip install -r ~/.hermes/skills/okx-agent-skills/tradingagents/requirements.txt
```

For a disposable virtual environment:

```bash
cd ~/.hermes/skills/okx-agent-skills/tradingagents
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/run_tradingagents.py --instrument BTC-USDT --date 2026-05-04 --output-language Chinese
```

## Common Pitfalls

- `/models` success plus graph `401 INVALID_API_KEY` often means unsupported model IDs, not bad credentials.
- Confirm runtime `data_vendors` resolves major categories to `okx`.
- `BTC-USDT` and `BTC-USDT-SWAP` are different markets.
- Do not add repo-root discovery back into the launcher; portability depends on importing only from the skill-local `vendor/` directory.
- When converting a repo-backed skill into a standalone packaged skill, update both the source payload (for example `hermes-skill/tradingagents/`) and the installed Hermes copy under `~/.hermes/skills/...`; otherwise the current session may still use the stale installed skill.
- Prefer installer copy mode (`bash scripts/install_skill.sh --copy --force`) for standalone packaging verification; symlink mode can hide missing vendored files because it still points at the development checkout.
- Preserve unrelated local changes in the launcher during packaging refactors, such as model defaults, retry handling, and output summarization; merge them into the standalone rewrite instead of overwriting the file wholesale.
- If Codex delegation is preferred but fails due to auth, usage limits, or sandbox prerequisites, continue the refactor directly and verify independently instead of stopping at the delegation error.
- Pytest may be absent in the active Python environment; syntax/compile checks plus a launcher health check are acceptable for skill packaging changes.
- Some symbols may fail transiently during the `/models` probe or graph startup with `ssl.SSLEOFError` / `UNEXPECTED_EOF_WHILE_READING`; retry the exact helper command at least once before treating the symbol as a hard failure.
- For scheduled OKX demo trading runs, a TradingAgents `Hold` rating is **not** actionable even when target/stop are present; only `Buy` / `Long` / `Overweight` / `加仓` / `做多` should qualify for new long entries, while `Sell` / `Short` / `Underweight` only justify reduction if the demo account already holds that instrument.
- In cron-style rank-8-10 workflows, check existing demo positions before trading: an `Underweight` conclusion on a symbol with **no current position** must not be converted into a fresh short.
- When Finance MCP tools are unavailable but the local Finance-Tracker SQLite DB is present, it is acceptable to use read-only SQLite inspection as a fallback for platform existence and historical-context checks; however, do **not** create or mutate finance records through ad-hoc SQL unless the task explicitly authorizes a non-MCP fallback writer.

## Verification

From a packaged checkout:

```bash
python -m py_compile hermes-skill/tradingagents/scripts/run_tradingagents.py
python -m compileall -q hermes-skill/tradingagents/vendor/tradingagents hermes-skill/tradingagents/vendor/cli
python hermes-skill/tradingagents/scripts/run_tradingagents.py --help
```

From an installed skill:

```bash
python -m py_compile ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py
python -m compileall -q ~/.hermes/skills/okx-agent-skills/tradingagents/vendor/tradingagents ~/.hermes/skills/okx-agent-skills/tradingagents/vendor/cli
python ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py --help
```

Runtime health check: the launcher should print `runtime_config` with `standalone: true`, `skill_root`, and `runtime_root`, then run TradingAgentsGraph and print `final_trade_decision` plus `decision_summary_json`.
