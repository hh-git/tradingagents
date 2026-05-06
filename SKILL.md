---
name: tradingagents
description: Use when the user wants to run a standalone packaged TradingAgentsGraph skill for OKX spot, swap, or futures analysis through a Hermes/OpenAI-compatible backend, without depending on a local TradingAgents repository.
version: 2.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [tradingagents, okx, hermes, analysis, spot, swap, futures, multi-agent, standalone]
    related_skills: [codex, hermes-agent, okx-cex-market]
---

# TradingAgents

## Overview

Run a standalone, skill-local TradingAgentsGraph runtime for deep OKX analysis. Use it for graph-based spot, perpetual/swap, or dated-futures research; do **not** use it for order execution.

The skill is self-contained: runtime source is vendored under `vendor/`. The launcher must not discover, import, or call `/home/chux/workspace/TradingAgents` or any external TradingAgents checkout.

## Layout

Installed path: `~/.hermes/skills/okx-agent-skills/tradingagents`

- `SKILL.md` — concise operator entrypoint
- `scripts/run_tradingagents.py` — single-instrument standalone launcher
- `scripts/run_tradingagents_batch.py` — multi-instrument launcher with subprocess isolation, per-symbol timeout, logs, and `summary.json`
- `vendor/tradingagents/`, `vendor/cli/` — vendored runtime packages
- `requirements.txt` — third-party Python deps for the runtime environment
- `references/usage.md` — install/operator notes
- `references/standalone-packaging.md` — standalone packaging verification
- `references/batch-parallel-isolation.md` — regression recipe for the batch launcher
- `references/operational-rules.md` — detailed trading/cron/live-position rules and security pitfalls

## When to Use

- The user asks for TradingAgentsGraph or multi-agent trading analysis.
- The user wants deep OKX analysis for spot, swap/perpetual, or dated futures.
- The user wants a portable TradingAgents Hermes skill that can be copied or packaged independently.
- Do not use for order placement, account operations, quick one-shot market lookups, or simple price checks.

## Quick Workflow

1. Normalize instrument IDs:
   - symbol only -> `<ASSET>-USDT`
   - swap/perp/contract -> `<ASSET>-USDT-SWAP`
   - dated futures only when explicitly requested
2. Use the skill virtualenv when present:

```bash
SKILL="$HOME/.hermes/skills/okx-agent-skills/tradingagents"
PY="$SKILL/.venv/bin/python"
[ -x "$PY" ] || PY=python3
```

3. Run a single instrument:

```bash
"$PY" "$SKILL/scripts/run_tradingagents.py" \
  --instrument ETH-USDT-SWAP \
  --date "$(date +%F)" \
  --output-language Chinese \
  --max-debate-rounds 1 \
  --max-risk-rounds 1 \
  --analysts market,social,news,fundamentals
```

4. For multi-symbol or cron-style work, prefer the batch launcher:

```bash
OUT="$HOME/.hermes/tmp/tradingagents-batch-$(date -u +%Y%m%dT%H%M%SZ)"
"$PY" "$SKILL/scripts/run_tradingagents_batch.py" \
  --instrument BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --date "$(date +%F)" \
  --output-language Chinese \
  --per-symbol-timeout 1800 \
  --max-workers 2 \
  --output-dir "$OUT"
```

5. Summarize `final_trade_decision` as the primary conclusion. When available, also use `decision_summary_json` for rating, target, stop, size, and horizon fields. For batches, read `<output-dir>/summary.json`.

## Hot-Rank / Hot-Contract Workflow

When the user asks to analyze OKX “hot” contracts by rank, interpret hotness as OKX SWAP 24h USD turnover unless they specify another ranking metric.

If the request follows an already extracted OKX official website hot-rank table, preserve that exact Top N and the exact displayed instruments. Do **not** silently replace it with `okx market filter` results; use CLI market data only as supplemental context or clearly labeled fallback.

Default SWAP discovery:

```bash
export PATH="$HOME/.hermes/node/bin:$PATH"
okx market filter --instType SWAP --sortBy volUsd24h --sortOrder desc --limit 10 --json
```

Fallback if `market filter` fails:

1. Fetch `okx market tickers SWAP --json`.
2. Restrict to the target universe, normally `*-USDT-SWAP` unless inverse/USD settlement was requested.
3. Approximate 24h USD turnover as `last * volCcy24h` because ticker output lacks `volUsd24h`.
4. Clearly label the ranking as a fallback approximation.
5. Feed only exact `instId` values into TradingAgents.

For official hot-rank SPOT workflows, see `references/operational-rules.md` before analysis or trading decisions.

## Live Position Analysis Workflow

For “analyze my positions/holdings”:

1. Load/use OKX portfolio skill for authenticated account reads.
2. Redact credentials: never show raw `okx config show --json` output.
3. Gather open positions and account equity:
   - `okx --profile live account positions --json`
   - `okx --profile live account balance --json`
   - `okx --profile live account asset-balance --valuation --json`
4. Feed only open contract `instId` values into TradingAgents, preferably through the batch launcher.
5. Add public market overlay for each open swap:
   - `okx market ticker <instId> --json`
   - `okx market funding-rate <instId> --json`
   - `okx market orderbook <instId> --sz 5 --json`
   - `okx market open-interest --instType SWAP --instId <instId> --json`
6. Final summary must include: side, size, average price, mark/current price, notional as % equity, UPL, liquidation distance, funding, order-book spread, TradingAgents rating/target/stop, and whether the rating is actionable for the existing position.

Important: a `Hold` rating on an existing long means “hold/observe, no blind add,” not “open a new trade.” More live-position and security details are in `references/operational-rules.md`.

## Runtime Facts

- standalone runtime root: `<skill_dir>/vendor`
- asset universe: `okx`
- default deep model: `gpt-5.5`
- default quick model: `gpt-5.4-mini`
- backend default: configured Hermes `model.base_url` when available, otherwise historical fallback `https://api.86gamestore.com/v1`
- if `HERMES_API_KEY` is unset, the helper attempts to load it from `~/.hermes/config.yaml` and referenced `.env` values
- probe `/models` before trusting model IDs
- wrapper injects `<skill_dir>/vendor` into `sys.path` before importing `tradingagents`
- `--repo-root` and `$TRADINGAGENTS_REPO_ROOT` are intentionally unsupported; portability depends on skill-local imports only
- before graph startup, launcher must call `tradingagents.dataflows.config.set_config(config)` after setting `asset_universe="okx"`; otherwise OKX instIds may fall through to yfinance behavior
- OKX crypto runs should skip equity-only pending outcome resolvers that use yfinance/SPY
- if a batch appears idle, inspect per-instrument logs and child processes before assuming it is stuck; model output often arrives in large chunks
- do not duplicate-retry later batch symbols merely because their logs still show only startup sections while the parent batch is running; wait for parent completion or configured per-symbol timeout and inspect `summary.json`

## Dependency Setup

The skill vendors TradingAgents source, not third-party Python wheels. Install deps into the Python used to run the skill:

```bash
cd ~/.hermes/skills/okx-agent-skills/tradingagents
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/run_tradingagents.py --instrument BTC-USDT --date 2026-05-04 --output-language Chinese
```

## GitHub Publishing

This installed skill directory is itself a git repository pushed to:

- `https://github.com/hh-git/tradingagents`

When refreshing/pushing, work directly from `~/.hermes/skills/okx-agent-skills/tradingagents`. Keep `.venv/`, caches, credentials, databases, and local config untracked. Verify local HEAD matches remote `main` and that remote `SKILL.md`, `scripts/run_tradingagents.py`, `scripts/run_tradingagents_batch.py`, and `vendor/tradingagents/` are present.

## Common Pitfalls

- `BTC-USDT` and `BTC-USDT-SWAP` are different markets.
- `final_trade_decision` / `decision_summary_json` are authoritative; do not trade from intermediate debate-stage reports.
- `Hold` is not actionable for new entries; for existing positions it means hold/observe.
- Some symbols may fail transiently during `/models` probe or graph startup with `ssl.SSLEOFError` / `UNEXPECTED_EOF_WHILE_READING`; retry the exact helper command at least once before treating as hard failure.
- If TradingAgents cannot produce structured final decisions, report unavailability and execute no new trade.
- For scheduled trading, Finance MCP unavailable means use local Finance-Tracker only in approved/read-only fallback modes as described in `references/operational-rules.md`.
- Do not expose OKX credentials; redact profile output in the same command.
- Preserve unrelated local changes during packaging or launcher refactors.
- Pytest may be absent; syntax/compile checks plus launcher help/health checks are acceptable.

## Verification

From an installed skill:

```bash
cd ~/.hermes/skills/okx-agent-skills/tradingagents
python -m py_compile scripts/run_tradingagents.py
python -m py_compile scripts/run_tradingagents_batch.py
python -m compileall -q vendor/tradingagents vendor/cli
python scripts/run_tradingagents.py --help
python scripts/run_tradingagents_batch.py --help
```

After modifying the batch launcher, also run the dummy mixed-outcome regression in `references/batch-parallel-isolation.md` to verify success, failure, and timeout can coexist while later symbols still complete.

Runtime health check: launcher should print `runtime_config` with `standalone: true`, `skill_root`, and `runtime_root`, then run TradingAgentsGraph and print `final_trade_decision` plus `decision_summary_json`.
