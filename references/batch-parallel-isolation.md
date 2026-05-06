# Batch Parallel Isolation Regression Notes

Use this reference after changing `scripts/run_tradingagents_batch.py` or when diagnosing multi-symbol TradingAgents runs.

## Expected behavior

- Multiple instruments should run concurrently through independent subprocess/process groups.
- `--per-symbol-timeout` kills only the timed-out instrument group.
- A failed or timed-out instrument must still appear in `summary.json` and must not prevent later instruments from launching/completing.
- `--fail-on-incomplete` should return nonzero only after all submitted instruments have finished or timed out.
- Results in `summary.json` should preserve the input order, not completion order.

## Dummy regression runner

Create a temporary dummy single-symbol runner:

```bash
cat > /tmp/tradingagents_dummy_runner.py <<'PY'
#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, time

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--instrument', required=True)
    p.add_argument('--date', required=True)
    p.add_argument('--output-language', required=True)
    p.add_argument('--max-debate-rounds')
    p.add_argument('--max-risk-rounds')
    p.add_argument('--analysts')
    p.add_argument('--deep-model')
    p.add_argument('--quick-model')
    p.add_argument('--backend-url')
    p.add_argument('--debug', action='store_true')
    a = p.parse_args()
    print(f'dummy_start: {a.instrument}', flush=True)
    if a.instrument == 'SLOW':
        time.sleep(5)
        return 0
    if a.instrument == 'FAIL':
        print('dummy_failure', flush=True)
        return 7
    if a.instrument == 'OK2':
        time.sleep(0.2)
    print('final_trade_decision:')
    print(f'Rating: Hold for {a.instrument}')
    print('decision_summary_json:')
    print(json.dumps({'rating': 'Hold', 'instrument': a.instrument}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
PY
```

Run the mixed-outcome batch:

```bash
cd ~/.hermes/skills/okx-agent-skills/tradingagents
OUT=$(mktemp -d /tmp/ta-batch-test-XXXXXX)
set +e
python scripts/run_tradingagents_batch.py \
  --runner-path /tmp/tradingagents_dummy_runner.py \
  --python python3 \
  --instrument OK1,SLOW,FAIL,OK2 \
  --date 2026-05-06 \
  --output-language Chinese \
  --per-symbol-timeout 1 \
  --kill-grace-seconds 0.2 \
  --max-workers 3 \
  --output-dir "$OUT" \
  --fail-on-incomplete > "$OUT/stdout.json" 2> "$OUT/stderr.log"
RC=$?
set -e
python3 - <<'PY' "$OUT/summary.json" "$RC"
import json, sys
summary_path, rc = sys.argv[1], int(sys.argv[2])
s = json.load(open(summary_path))
statuses = {r['instrument']: r['status'] for r in s['results']}
assert rc == 1, rc
assert s['counts']['instruments'] == 4
assert statuses == {'OK1': 'ok', 'SLOW': 'timeout', 'FAIL': 'failed', 'OK2': 'ok'}
assert s['counts']['final_trade_decision'] == 2
assert s['counts']['decision_summary_json'] == 2
print('batch_parallel_isolation_ok')
PY
```

## Live Top-10 run pattern

For a full OKX official hot-contract Top 10 follow-up, preserve the website-extracted universe and run:

```bash
SKILL="$HOME/.hermes/skills/okx-agent-skills/tradingagents"
PY="$SKILL/.venv/bin/python"
[ -x "$PY" ] || PY=python3
OUT="$HOME/.hermes/tmp/tradingagents-official-hot-contracts-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
"$PY" "$SKILL/scripts/run_tradingagents_batch.py" \
  --instrument BTC-USDT-SWAP,ETH-USDT-SWAP,LAB-USDT-SWAP,RAVE-USDT-SWAP,TON-USDT-SWAP,BSB-USDT-SWAP,SPY-USDT-SWAP,ZEC-USDT-SWAP,DOGE-USDT-SWAP,UB-USDT-SWAP \
  --date "$(date +%F)" \
  --output-language Chinese \
  --max-debate-rounds 1 \
  --max-risk-rounds 1 \
  --analysts market,social,news,fundamentals \
  --per-symbol-timeout 900 \
  --kill-grace-seconds 5 \
  --max-workers 3 \
  --output-dir "$OUT" \
  2>&1 | tee "$OUT/batch.log"
```

On 2026-05-06 this pattern completed all 10 official hot-contract instruments successfully with `counts.status == {'ok': 10}`. Earlier serial behavior stalled after BTC/ETH startup; concurrent isolation avoided one slow symbol blocking later symbols.
