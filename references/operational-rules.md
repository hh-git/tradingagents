# TradingAgents Operational Rules

This reference preserves detailed operator rules that used to make `SKILL.md` too long. Load it when the task is a scheduled OKX workflow, live-position analysis, hot-rank analysis, or any workflow that may trade after analysis.

## Decision Authority

- Treat `final_trade_decision` and `decision_summary_json` as the authoritative result.
- Ignore upstream `market_report` / debate-stage `BUY` noise when the final rating is weaker, for example `Hold` or `Underweight`.
- A TradingAgents `Hold` rating is not actionable for new entries even when target/stop fields are present.
- For scheduled demo trading, only `Buy` / `Long` / `Overweight` / `加仓` / `做多` can qualify for new long entries.
- `Sell` / `Short` / `Underweight` can justify reduction only when the account already holds that instrument.
- In rank-slice workflows, an `Underweight` conclusion on a symbol with no current position must not be converted into a fresh short.
- If TradingAgents cannot produce both `final_trade_decision` and `decision_summary_json`, report analysis unavailable and execute no new trade.
- Do not substitute ad-hoc directional guesses from price action or sentiment to force a trade.

## Backend and Authentication Pitfalls

- Probe `/models` before trusting configured model IDs.
- `/models` success plus graph `401 INVALID_API_KEY` often means unsupported model IDs, not necessarily bad credentials.
- For OKX SPOT hot-list workflows, if `/models` returns `HTTP 401 INVALID_API_KEY`, first verify whether the launcher is using the correct Hermes `model.base_url`.
- On this environment, `~/.hermes/config.yaml` may point the active provider to local Sub2API (`http://127.0.0.1:8080/v1`), while old launcher defaults used `https://api.86gamestore.com/v1`; probing the wrong base URL with the local provider key causes false 401.
- The launcher should default `--backend-url` from `model.base_url` and parse YAML config / `.env` references rather than scanning only top-level `api_key:` lines.
- Treat analysis as unavailable only after verifying configured base URL and key source.

## Hot-Rank Universe Rules

- For follow-up TradingAgents analysis after an OKX official hot-rank extraction, preserve the already extracted website ranking and instruments as the candidate universe.
- Do not re-query `okx market filter` and substitute a different volume-sorted universe unless the website source is unavailable or the user explicitly changes the ranking basis.
- For OKX SPOT hot-list workflows, prefer the official OKX website ranking page (`/markets/rankings/spot/hot-crypto`) as the primary universe source when the user says “热度榜/热门榜”.
- Use `okx market filter --instType SPOT --quoteCcy USDT --sortBy volUsd24h --sortOrder desc` only as a clearly labeled fallback when the website ranking cannot be extracted.
- When browser/DOM tools are unavailable but the OKX hot-rank page HTML is fetchable, extract visible ranking payload directly from embedded page data containing fields such as `instId`, `turnOver24h`, `rankIndex`, `lastPrice`, and `changePerDay24h`.
- For SWAP hot-contract rank requests, `okx market filter --instType SWAP --sortBy volUsd24h --sortOrder desc` is the normal source unless a prior official website list must be preserved.

## Batch Monitoring Rules

- Prefer `scripts/run_tradingagents_batch.py` for multi-symbol or cron runs.
- Use `--per-symbol-timeout`; timeouts must mark only that symbol as `timeout`, preserve its log, and continue later instruments.
- For OKX demo SPOT hot-top cron jobs, prefer `--per-symbol-timeout 3600` over 1800 because five-symbol runs can otherwise lose usable decisions.
- For full OKX official hot-contract Top 10 requests, use concurrent batch execution, preserve the official website ranking universe, run with `--max-workers 3`, and a per-symbol timeout around 900s when appropriate.
- Do not kill the whole batch just because one symbol is slow; let the launcher mark individual `timeout` / `failed` statuses unless the parent process is hung and all log mtimes stop advancing beyond the configured timeout.
- If a batch appears idle with little stdout, inspect per-instrument log files and the child process tree; graph output often arrives in large chunks after long model calls.
- Do not start a duplicate retry for later symbols merely because their logs show only `runtime_config` / `data_vendors_before_graph` while earlier symbols have just completed. With `--max-workers 2`, later logs can remain at startup for several minutes during model/tool calls. Wait for parent completion or configured per-symbol timeout, then inspect `summary.json`.
- If a scheduled large hot-list batch has only the first one or two logs, no `summary.json`, and file mtimes stop advancing for several minutes, treat it as stalled. Preserve partial output, inspect completed logs, and use only symbols that actually contain structured decisions.
- If only startup sections exist for a symbol, report that symbol as unavailable.
- A single-symbol retry is acceptable for a priority held/hot name, but if it still emits only startup sections and no final decision, classify it as unavailable and do not trade.
- After modifying the batch launcher, verify with `references/batch-parallel-isolation.md`.

## Scheduled Demo SPOT Rules

- If TradingAgents cannot produce structured final decisions, report official hot-rank / holdings snapshot and sentiment posture but execute no new spot trade.
- Existing dust balances, such as sub-cent residual HYPE/OKB amounts, are non-actionable and should be excluded from the effective holdings candidate set unless large enough to matter for a real spot decision.
- Before trimming an existing demo position on an `Underweight` conclusion, compare current notional against the strategy cap and the recommendation strength. If position is already small and recommendation is moderate de-risking rather than hard exit, it is acceptable to skip trading and report no executable action met the rules.
- Finance fallback writers may be used for scheduled demo trading when Finance MCP tools are unavailable and a supported local Finance-Tracker database exists. Only record trades actually confirmed on OKX, and label the report as a Finance fallback path rather than claiming MCP execution.

## Live Position Analysis

- Start from `okx --profile live account positions --json` plus `account balance` / `asset-balance --valuation`.
- Feed only open contract `instId` values into TradingAgents.
- Include a position-aware overlay in the final summary:
  - side
  - size
  - average price
  - current / mark price
  - notional as a percentage of account equity
  - UPL
  - liquidation distance
  - funding cost
  - order-book spread
  - TradingAgents rating
  - target / stop
  - whether rating is actionable for the existing position
- A `Hold` rating on an existing long means “hold / observe, no blind add”, not “open a new trade”.
- Public market follow-up checks that materially improve the overlay: ticker, funding-rate, orderbook, open-interest.
- OKX CLI syntax for public overlays:
  - `okx market ticker <instId> --json`
  - `okx market funding-rate <instId> --json`
  - `okx market orderbook <instId> --sz 5 --json`
  - `okx market open-interest --instType SWAP --instId <instId> --json`
- Using `--instId` with `ticker`, `funding-rate`, or `orderbook` returns “Missing required parameter instId”; `open-interest` is the exception that requires `--instId`.

## Security and Finance Fallback

- Never run or display raw `okx config show --json` output in a terminal result; it includes `api_key`, `secret_key`, and `passphrase`.
- If routing requires profile detection, redact credential fields in the same command or write redacted output to a private temp file.
- Avoid shell heredoc filters that consume stdin incorrectly and can accidentally lead to retries with unredacted output.
- Preferred redaction pattern:

```bash
okx config show --json | python3 -c 'import json,sys; d=json.load(sys.stdin);\nfor p in (d.get("profiles") or {}).values():\n    [p.__setitem__(k,"<redacted>") for k in ("api_key","secret_key","passphrase") if p.get(k)]\nprint(json.dumps(d,ensure_ascii=False,indent=2))'
```

- When Finance MCP tools are unavailable, use local Finance-Tracker SQLite only in read-only fallback mode to confirm existing OKX Demo / OKX 模拟盘 platform records and gather overview / transaction / asset stats.
- Do not create or mutate Finance records through ad-hoc SQL unless a supported writer path is available and an actual trade occurred, or the task explicitly authorizes a non-MCP fallback writer.
