# Standalone TradingAgents skill usage

This directory is a complete Hermes `tradingagents` skill payload. It no longer requires a local `/home/chux/workspace/TradingAgents` checkout at runtime.

## Layout

- `SKILL.md`: installable Hermes skill definition
- `scripts/run_tradingagents.py`: standalone OKX/Hermes launcher
- `vendor/tradingagents/`: vendored TradingAgents runtime source
- `vendor/cli/`: vendored CLI support package for runtime imports
- `requirements.txt`: third-party Python dependencies for the vendored runtime
- `references/usage.md`: this operator note

## Install

Recommended from the packaging repository:

```bash
bash scripts/install_skill.sh --copy --force
```

Default destination:

```text
~/.hermes/skills/okx-agent-skills/tradingagents
```

Copy mode is preferred for a portable packaged skill. Symlink mode is still supported for development.

## Dependencies

The skill vendors project source code, not third-party wheels. Install dependencies into the Python environment that will run the skill:

```bash
python -m pip install -r ~/.hermes/skills/okx-agent-skills/tradingagents/requirements.txt
```

Optional isolated environment:

```bash
cd ~/.hermes/skills/okx-agent-skills/tradingagents
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

Installed skill:

```bash
python ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py \
  --instrument BTC-USDT \
  --date 2026-05-04 \
  --output-language Chinese
```

Packaged checkout before installation:

```bash
python hermes-skill/tradingagents/scripts/run_tradingagents.py \
  --instrument BTC-USDT-SWAP \
  --date 2026-05-04 \
  --output-language Chinese
```

## Important runtime rule

`scripts/run_tradingagents.py` imports from `<skill_dir>/vendor` only. It intentionally does not support `--repo-root`, `TRADINGAGENTS_REPO_ROOT`, or a hardcoded local TradingAgents repository path.

## Verification

```bash
python -m py_compile hermes-skill/tradingagents/scripts/run_tradingagents.py
python -m compileall -q hermes-skill/tradingagents/vendor/tradingagents hermes-skill/tradingagents/vendor/cli
python hermes-skill/tradingagents/scripts/run_tradingagents.py --help
```

Expected `--help` output should not mention `--repo-root`.

A real run should print `runtime_config` containing:

- `standalone: true`
- `skill_root`
- `runtime_root`

Then it should print `final_trade_decision` and `decision_summary_json`.
