# Standalone packaging refactor notes

Use these notes when a previously repo-backed TradingAgents Hermes skill must become portable and self-contained.

## Proven approach

1. Vendor runtime source into the skill payload:
   - `hermes-skill/tradingagents/vendor/tradingagents/`
   - `hermes-skill/tradingagents/vendor/cli/` if runtime imports need it
   - exclude `__pycache__/` and `*.pyc`
2. Add `requirements.txt` to the skill payload from the package dependency list. Vendor source code, not third-party wheels.
3. Rewrite `scripts/run_tradingagents.py` so it:
   - computes `skill_root = Path(__file__).resolve().parents[1]`
   - injects only `<skill_root>/vendor` into `sys.path`
   - verifies `vendor/tradingagents/__init__.py` exists
   - removes `--repo-root`, `TRADINGAGENTS_REPO_ROOT`, and hardcoded local repo fallbacks
   - reports `runtime_config` with `standalone: true`, `skill_root`, and `runtime_root`
4. Preserve unrelated local launcher improvements during the rewrite, especially model defaults, `/models` retry behavior, and decision summary output.
5. Update `SKILL.md` and `references/usage.md` to remove repo-boundary guidance and document the standalone layout.
6. Install with copy mode for verification:
   ```bash
   bash scripts/install_skill.sh --copy --force
   ```

## Verification commands

From the packaging repo:

```bash
python3 -m py_compile hermes-skill/tradingagents/scripts/run_tradingagents.py
python3 -m compileall -q hermes-skill/tradingagents/vendor/tradingagents hermes-skill/tradingagents/vendor/cli
python3 hermes-skill/tradingagents/scripts/run_tradingagents.py --help
```

After install:

```bash
python3 -m py_compile ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py
python3 ~/.hermes/skills/okx-agent-skills/tradingagents/scripts/run_tradingagents.py --help
```

Portable-copy smoke test:

```bash
TESTDIR=$(mktemp -d /tmp/tradingagents-skill-standalone-test.XXXXXX)
cp -a hermes-skill/tradingagents "$TESTDIR/portable-skill"
python3 "$TESTDIR/portable-skill/scripts/run_tradingagents.py" --help > "$TESTDIR/help.txt"
! grep -E -- '--repo-root|TRADINGAGENTS_REPO_ROOT|/home/chux/workspace/TradingAgents' "$TESTDIR/help.txt"
```

Also scan runtime Python files for forbidden repo-discovery markers:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path('hermes-skill/tradingagents')
bad = []
for p in [root/'scripts/run_tradingagents.py'] + list((root/'vendor').rglob('*.py')):
    text = p.read_text(errors='ignore')
    for forbidden in ['/home/chux/workspace/TradingAgents', 'TRADINGAGENTS_REPO_ROOT', '--repo-root', 'detect_repo_root']:
        if forbidden in text:
            bad.append((str(p), forbidden))
if bad:
    for item in bad:
        print(item)
    raise SystemExit(1)
print('runtime python files have no repo discovery/path markers')
PY
```

## Pitfalls

- `skill_view` reads the installed skill copy, not necessarily the source payload in the packaging repo. Keep both synchronized when editing a package source.
- Symlink installs are convenient for development but weak evidence for portability; use copy mode before claiming the skill is standalone.
- A denied destructive shell command should be replaced with a safer `mktemp`-based check rather than retried verbatim.
