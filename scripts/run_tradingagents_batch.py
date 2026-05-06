#!/usr/bin/env python3
"""Run TradingAgentsGraph for multiple instruments with per-symbol isolation.

Each instrument is delegated to scripts/run_tradingagents.py in its own process
group. A stalled symbol is timed out and terminated without preventing later
symbols from running.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FINAL_MARKER = "final_trade_decision:"
SUMMARY_MARKER = "decision_summary_json:"
BATCH_EVENT_MARKER = "run_tradingagents_batch_event:"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run skill-local TradingAgentsGraph for multiple OKX instruments.",
    )
    parser.add_argument(
        "--instrument",
        action="append",
        default=[],
        help="OKX instrument ID. May be repeated or contain comma/whitespace-separated IDs.",
    )
    parser.add_argument(
        "--instrument-file",
        help="File containing OKX instrument IDs, one per line or comma/whitespace-separated.",
    )
    parser.add_argument("--date", required=True, help="Analysis date passed to TradingAgentsGraph, YYYY-MM-DD.")
    parser.add_argument("--output-language", required=True, help="Output language, e.g. Chinese or English.")
    parser.add_argument(
        "--per-symbol-timeout",
        type=float,
        default=1800.0,
        help="Seconds to allow each instrument before killing only that instrument. Use 0 for no timeout.",
    )
    parser.add_argument(
        "--kill-grace-seconds",
        type=float,
        default=10.0,
        help="Seconds to wait after SIGTERM before SIGKILL on timeout.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help=(
            "Maximum concurrent instrument subprocess workers. "
            "Default: 1 for a single instrument, 2 for multiple instruments."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for per-symbol logs and summary.json. Default: ~/.hermes/tmp/tradingagents-batch-<UTC timestamp>.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="JSON summary path. Default: <output-dir>/summary.json.",
    )
    parser.add_argument(
        "--runner-path",
        type=Path,
        default=Path(__file__).with_name("run_tradingagents.py"),
        help="Path to the single-instrument runner.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run the single-instrument runner. Default: current interpreter.",
    )
    parser.add_argument("--deep-model", help="Forwarded to run_tradingagents.py.")
    parser.add_argument("--quick-model", help="Forwarded to run_tradingagents.py.")
    parser.add_argument("--backend-url", help="Forwarded to run_tradingagents.py.")
    parser.add_argument("--max-debate-rounds", type=int, default=1, help="Forwarded to run_tradingagents.py.")
    parser.add_argument("--max-risk-rounds", type=int, default=1, help="Forwarded to run_tradingagents.py.")
    parser.add_argument(
        "--analysts",
        default="market,social,news,fundamentals",
        help="Forwarded to run_tradingagents.py.",
    )
    parser.add_argument("--debug", action="store_true", help="Forwarded to run_tradingagents.py.")
    parser.add_argument(
        "--sleep-between-symbols",
        type=float,
        default=0.0,
        help=(
            "Optional seconds between instrument launches. With --max-workers 1, "
            "this preserves the old sleep-after-each-instrument behavior."
        ),
    )
    parser.add_argument(
        "--fail-on-incomplete",
        action="store_true",
        help="Exit 1 if any instrument times out, fails, or lacks final_trade_decision.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp_for_path() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def split_instrument_values(values: list[str]) -> list[str]:
    instruments: list[str] = []
    for value in values:
        for item in re.split(r"[\s,]+", value.strip()):
            if item:
                instruments.append(item)
    return instruments


def read_instrument_file(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            values.append(stripped)
    return split_instrument_values(values)


def collect_instruments(args: argparse.Namespace) -> list[str]:
    instruments = split_instrument_values(args.instrument)
    if args.instrument_file:
        instruments.extend(read_instrument_file(Path(args.instrument_file).expanduser()))
    return instruments


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "instrument"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find unique log path for {path}")


def output_dir_for(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return args.output_dir.expanduser().resolve()
    return (Path.home() / ".hermes" / "tmp" / f"tradingagents-batch-{timestamp_for_path()}").resolve()


def max_workers_for(args: argparse.Namespace, instrument_count: int) -> int:
    if args.max_workers is not None:
        if args.max_workers < 1:
            raise ValueError("--max-workers must be at least 1")
        return min(args.max_workers, instrument_count)
    if instrument_count <= 1:
        return 1
    return min(2, instrument_count)


def build_command(args: argparse.Namespace, runner_path: Path, instrument: str) -> list[str]:
    command = [
        args.python,
        str(runner_path),
        "--instrument",
        instrument,
        "--date",
        args.date,
        "--output-language",
        args.output_language,
        "--max-debate-rounds",
        str(args.max_debate_rounds),
        "--max-risk-rounds",
        str(args.max_risk_rounds),
        "--analysts",
        args.analysts,
    ]
    if args.deep_model:
        command.extend(["--deep-model", args.deep_model])
    if args.quick_model:
        command.extend(["--quick-model", args.quick_model])
    if args.backend_url:
        command.extend(["--backend-url", args.backend_url])
    if args.debug:
        command.append("--debug")
    return command


def append_log(log_path: Path, message: str) -> None:
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n{BATCH_EVENT_MARKER} timestamp={utc_now()} {message}\n")


def terminate_process_group(proc: subprocess.Popen[Any], grace_seconds: float, log_path: Path) -> int | None:
    if proc.poll() is not None:
        return proc.returncode

    append_log(log_path, f"timeout reached; sending SIGTERM to process group {proc.pid}")
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.poll()

    try:
        return proc.wait(timeout=max(0.0, grace_seconds))
    except subprocess.TimeoutExpired:
        append_log(log_path, f"process group {proc.pid} did not exit after SIGTERM; sending SIGKILL")
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return proc.poll()
        return proc.wait()


def extract_section(text: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    section = text[start + len(start_marker) :]
    end = len(section)
    for marker in end_markers:
        marker_index = section.find(marker)
        if marker_index >= 0:
            end = min(end, marker_index)
    return section[:end].strip()


def extract_decision_summary_json(text: str) -> tuple[dict[str, Any] | None, str]:
    start = text.find(SUMMARY_MARKER)
    if start < 0:
        return None, ""

    tail = text[start + len(SUMMARY_MARKER) :].lstrip()
    if not tail:
        return None, "decision_summary_json marker was present but empty"

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(tail)
    except json.JSONDecodeError as exc:
        return None, f"{exc.msg} at line {exc.lineno} column {exc.colno}"

    if not isinstance(parsed, dict):
        return None, f"decision_summary_json was {type(parsed).__name__}, not object"
    return parsed, ""


def fallback_summary_from_decision(decision_text: str) -> dict[str, str]:
    summary = {"rating": "", "price_target": "", "stop_loss": "", "max_position_size": "", "time_horizon": ""}
    patterns = {
        "rating": (
            r"(?im)^\s*\*\*Rating\*\*\s*[:：]\s*(.+)$",
            r"(?im)^\s*Rating\s*[:：]\s*(.+)$",
            r"(?im)^\s*评级\s*[:：]\s*(.+)$",
        ),
        "price_target": (
            r"(?im)^\s*\*\*Price Target\*\*\s*[:：]\s*(.+)$",
            r"(?im)^\s*Price Target\s*[:：]\s*(.+)$",
            r"(?im)^\s*目标价\s*[:：]\s*(.+)$",
        ),
        "stop_loss": (
            r"(?im)^\s*\*\*Stop Loss\*\*\s*[:：]\s*(.+)$",
            r"(?im)^\s*Stop Loss\s*[:：]\s*(.+)$",
            r"(?im)^\s*止损\s*[:：]\s*(.+)$",
        ),
        "max_position_size": (
            r"(?im)^\s*\*\*Max Position Size\*\*\s*[:：]\s*(.+)$",
            r"(?im)^\s*Max Position Size\s*[:：]\s*(.+)$",
            r"(?im)^\s*最大仓位\s*[:：]\s*(.+)$",
        ),
        "time_horizon": (
            r"(?im)^\s*\*\*Time Horizon\*\*\s*[:：]\s*(.+)$",
            r"(?im)^\s*Time Horizon\s*[:：]\s*(.+)$",
            r"(?im)^\s*时间周期\s*[:：]\s*(.+)$",
        ),
    }
    for key, key_patterns in patterns.items():
        for pattern in key_patterns:
            match = re.search(pattern, decision_text)
            if match:
                summary[key] = match.group(1).strip()
                break
    return summary


def trunc(value: str, limit: int = 1200) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def parse_log(log_path: Path) -> dict[str, Any]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    final_trade_decision = extract_section(
        text,
        FINAL_MARKER,
        (SUMMARY_MARKER, "selected_state:", BATCH_EVENT_MARKER),
    )
    decision_summary_json, summary_error = extract_decision_summary_json(text)
    decision_summary = (
        decision_summary_json
        if decision_summary_json is not None
        else fallback_summary_from_decision(final_trade_decision)
    )
    rating = decision_summary.get("rating", "") if isinstance(decision_summary, dict) else ""

    return {
        "final_trade_decision_present": bool(final_trade_decision),
        "final_trade_decision_chars": len(final_trade_decision),
        "final_trade_decision_excerpt": trunc(final_trade_decision),
        "decision_summary_json_present": decision_summary_json is not None,
        "decision_summary_json_error": summary_error,
        "decision_summary_json": decision_summary_json,
        "decision_summary": decision_summary,
        "rating": str(rating).strip(),
    }


def run_one(
    args: argparse.Namespace,
    runner_path: Path,
    instrument: str,
    index: int,
    output_dir: Path,
) -> dict[str, Any]:
    log_path = unique_path(output_dir / f"{index:03d}_{safe_name(instrument)}.log")
    command = build_command(args, runner_path, instrument)
    started_at = utc_now()
    started_monotonic = time.monotonic()
    timed_out = False

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{BATCH_EVENT_MARKER} timestamp={started_at} command={shlex.join(command)}\n")
        log_file.flush()
        proc = subprocess.Popen(
            command,
            cwd=str(skill_root()),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            if args.per_symbol_timeout and args.per_symbol_timeout > 0:
                return_code = proc.wait(timeout=args.per_symbol_timeout)
            else:
                return_code = proc.wait()
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = terminate_process_group(proc, args.kill_grace_seconds, log_path)

    finished_at = utc_now()
    duration_seconds = round(time.monotonic() - started_monotonic, 3)
    append_log(
        log_path,
        f"finished status={'timeout' if timed_out else 'completed'} return_code={return_code} duration_seconds={duration_seconds}",
    )
    parsed = parse_log(log_path)

    if timed_out:
        status = "timeout"
    elif return_code == 0 and parsed["final_trade_decision_present"]:
        status = "ok"
    elif return_code == 0:
        status = "missing_final_trade_decision"
    else:
        status = "failed"

    return {
        "instrument": instrument,
        "status": status,
        "return_code": return_code,
        "timed_out": timed_out,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "log_path": str(log_path),
        **parsed,
    }


def summarize_results(
    args: argparse.Namespace,
    runner_path: Path,
    output_dir: Path,
    summary_file: Path,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        status_counts[status] = status_counts.get(status, 0) + 1

    final_count = sum(1 for result in results if result["final_trade_decision_present"])
    summary_count = sum(1 for result in results if result["decision_summary_json_present"])

    return {
        "generated_at": utc_now(),
        "date": args.date,
        "output_language": args.output_language,
        "per_symbol_timeout_seconds": args.per_symbol_timeout,
        "kill_grace_seconds": args.kill_grace_seconds,
        "python": args.python,
        "runner_path": str(runner_path),
        "output_dir": str(output_dir),
        "summary_file": str(summary_file),
        "counts": {
            "instruments": len(results),
            "final_trade_decision": final_count,
            "decision_summary_json": summary_count,
            "both_final_trade_decision_and_decision_summary_json": sum(
                1
                for result in results
                if result["final_trade_decision_present"] and result["decision_summary_json_present"]
            ),
            "status": status_counts,
        },
        "results": results,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def print_result_line(instrument: str, result: dict[str, Any]) -> None:
    print(
        (
            f"{instrument}: {result['status']} "
            f"final_trade_decision={result['final_trade_decision_present']} "
            f"decision_summary_json={result['decision_summary_json_present']} "
            f"log={result['log_path']}"
        ),
        file=sys.stderr,
        flush=True,
    )


def launcher_failure_result(output_dir: Path, instrument: str, index: int, exc: Exception) -> dict[str, Any]:
    finished_at = utc_now()
    error = f"launcher worker failed: {type(exc).__name__}: {exc}"
    log_path = unique_path(output_dir / f"{index:03d}_{safe_name(instrument)}.launcher-error.log")
    append_log(log_path, error)
    return {
        "instrument": instrument,
        "status": "failed",
        "return_code": None,
        "timed_out": False,
        "started_at": "",
        "finished_at": finished_at,
        "duration_seconds": 0.0,
        "log_path": str(log_path),
        "final_trade_decision_present": False,
        "final_trade_decision_chars": 0,
        "final_trade_decision_excerpt": "",
        "decision_summary_json_present": False,
        "decision_summary_json_error": error,
        "decision_summary_json": None,
        "decision_summary": fallback_summary_from_decision(""),
        "rating": "",
    }


def run_all(
    args: argparse.Namespace,
    runner_path: Path,
    instruments: list[str],
    output_dir: Path,
) -> list[dict[str, Any]]:
    worker_count = max_workers_for(args, len(instruments))
    args.max_workers = worker_count

    if worker_count == 1:
        results: list[dict[str, Any]] = []
        for index, instrument in enumerate(instruments, start=1):
            print(f"running {instrument} ({index}/{len(instruments)})", file=sys.stderr, flush=True)
            try:
                result = run_one(args, runner_path, instrument, index, output_dir)
            except Exception as exc:
                result = launcher_failure_result(output_dir, instrument, index, exc)
            results.append(result)
            print_result_line(instrument, result)
            if args.sleep_between_symbols > 0 and index < len(instruments):
                time.sleep(args.sleep_between_symbols)
        return results

    print(
        f"running {len(instruments)} instruments with max_workers={worker_count}",
        file=sys.stderr,
        flush=True,
    )
    results_by_index: dict[int, dict[str, Any]] = {}
    futures: dict[Future[dict[str, Any]], tuple[int, str]] = {}
    next_submit = 0

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while next_submit < len(instruments) and len(futures) < worker_count:
            index = next_submit + 1
            instrument = instruments[next_submit]
            print(f"starting {instrument} ({index}/{len(instruments)})", file=sys.stderr, flush=True)
            futures[executor.submit(run_one, args, runner_path, instrument, index, output_dir)] = (
                index,
                instrument,
            )
            next_submit += 1
            if args.sleep_between_symbols > 0 and next_submit < len(instruments):
                time.sleep(args.sleep_between_symbols)

        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                index, instrument = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = launcher_failure_result(output_dir, instrument, index, exc)
                results_by_index[index] = result
                print_result_line(instrument, result)

                if next_submit < len(instruments):
                    next_index = next_submit + 1
                    next_instrument = instruments[next_submit]
                    print(
                        f"starting {next_instrument} ({next_index}/{len(instruments)})",
                        file=sys.stderr,
                        flush=True,
                    )
                    futures[
                        executor.submit(run_one, args, runner_path, next_instrument, next_index, output_dir)
                    ] = (next_index, next_instrument)
                    next_submit += 1
                    if args.sleep_between_symbols > 0 and next_submit < len(instruments):
                        time.sleep(args.sleep_between_symbols)

    return [results_by_index[index] for index in range(1, len(instruments) + 1)]


def main() -> int:
    args = parse_args()
    instruments = collect_instruments(args)
    if not instruments:
        print("error: provide at least one --instrument or --instrument-file entry", file=sys.stderr)
        return 2

    runner_path = args.runner_path.expanduser().resolve()
    if not runner_path.exists():
        print(f"error: runner does not exist: {runner_path}", file=sys.stderr)
        return 2

    output_dir = output_dir_for(args)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_file = (args.summary_file.expanduser().resolve() if args.summary_file else output_dir / "summary.json")

    try:
        results = run_all(args, runner_path, instruments, output_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = summarize_results(args, runner_path, output_dir, summary_file, results)
    write_json_atomic(summary_file, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.fail_on_incomplete:
        incomplete = any(result["status"] != "ok" for result in results)
        return 1 if incomplete else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
