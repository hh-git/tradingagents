"""SQLite-backed trade journal for execution records and P&L attribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class JournalEvent:
    """Normalized trade journal event returned by write APIs."""

    id: int
    event_type: str
    symbol: str
    side: str
    quantity: float
    price: Optional[float]
    fees: float
    gross_value: Optional[float]
    net_value: Optional[float]
    realized_pnl: Optional[float]


class TradeJournal:
    """Append-only SQLite journal with weighted average cost spot accounting."""

    _POSITION_EPSILON = 1e-12

    def __init__(self, path_or_config: str | Path | dict[str, Any]):
        if isinstance(path_or_config, dict):
            path = path_or_config.get("trade_journal_path")
        else:
            path = path_or_config
        if not path:
            raise ValueError("trade_journal_path is required")

        self.db_path = Path(path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def record_decision(
        self,
        ticker: Optional[str] = None,
        side: str = "hold",
        decision_text: str = "",
        rationale: str = "",
        mode: str = "demo",
        source_run_id: Optional[str] = None,
        tags: Optional[Iterable[str] | str] = None,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
        source: str = "tradingagents",
    ) -> dict[str, Any]:
        """Record a strategy decision without changing inventory accounting."""
        return self.record_trade_event(
            ticker=ticker,
            symbol=symbol,
            side=side,
            quantity=0,
            price=None,
            fees=0,
            mode=mode,
            decision_text=decision_text,
            rationale=rationale,
            source_run_id=source_run_id,
            source=source,
            tags=tags,
            trade_date=trade_date,
            event_type="decision",
        )

    def record_trade_event(
        self,
        ticker: Optional[str] = None,
        side: str = "hold",
        quantity: float = 0,
        price: Optional[float] = None,
        fees: float = 0,
        mode: str = "demo",
        decision_text: str = "",
        rationale: str = "",
        source_run_id: Optional[str] = None,
        note: Optional[str] = None,
        tags: Optional[Iterable[str] | str] = None,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
        source: str = "manual",
        event_type: str = "execution",
    ) -> dict[str, Any]:
        """Append an event and update symbol position for buy/sell executions."""
        symbol = self._normalize_symbol(symbol or ticker)
        side = self._normalize_side(side)
        mode = (mode or "demo").lower()
        quantity = self._to_float(quantity, "quantity")
        fees = self._to_float(fees, "fees")
        price_value = None if price is None else self._to_float(price, "price")
        event_type = (event_type or "execution").lower()

        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        if fees < 0:
            raise ValueError("fees must be non-negative")
        if side in {"buy", "sell"} and event_type == "execution":
            if quantity <= 0:
                raise ValueError("buy/sell executions require a positive quantity")
            if price_value is None or price_value <= 0:
                raise ValueError("buy/sell executions require a positive price")
        elif side in {"hold", "no-trade", "none"}:
            quantity = 0
            price_value = None if price_value is None else price_value

        now = self._utc_now()
        trade_date = str(trade_date) if trade_date is not None else now[:10]
        tags_json = self._encode_tags(tags)
        gross_value = quantity * price_value if price_value is not None else None
        net_value: Optional[float] = gross_value
        realized_pnl: Optional[float] = None

        with self._connect() as conn:
            position = self._get_or_create_position(conn, symbol)
            new_qty = float(position["quantity"])
            new_avg_cost = float(position["avg_cost"])
            cumulative_realized = float(position["realized_pnl"])
            total_fees = float(position["total_fees"])
            closed_count = int(position["closed_trade_count"])
            winning_count = int(position["winning_closed_trade_count"])

            if event_type == "execution" and side == "buy":
                gross_value = quantity * float(price_value)
                net_value = gross_value + fees
                existing_cost = new_qty * new_avg_cost
                new_qty += quantity
                new_avg_cost = (existing_cost + net_value) / new_qty
                realized_pnl = 0.0
                total_fees += fees
            elif event_type == "execution" and side == "sell":
                if quantity - new_qty > self._POSITION_EPSILON:
                    raise ValueError(
                        f"cannot sell {quantity:g} {symbol}; current position is {new_qty:g}"
                    )
                gross_value = quantity * float(price_value)
                net_value = gross_value - fees
                realized_pnl = net_value - (new_avg_cost * quantity)
                new_qty -= quantity
                if abs(new_qty) <= self._POSITION_EPSILON:
                    new_qty = 0.0
                    new_avg_cost = 0.0
                cumulative_realized += realized_pnl
                total_fees += fees
                closed_count += 1
                if realized_pnl > 0:
                    winning_count += 1

            cursor = conn.execute(
                """
                insert into trade_events (
                    created_at, trade_date, event_type, symbol, side, mode,
                    quantity, price, fees, gross_value, net_value, realized_pnl,
                    decision_text, rationale, source_run_id, source, tags, note
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    trade_date,
                    event_type,
                    symbol,
                    side,
                    mode,
                    quantity,
                    price_value,
                    fees,
                    gross_value,
                    net_value,
                    realized_pnl,
                    decision_text,
                    rationale,
                    source_run_id,
                    source,
                    tags_json,
                    note,
                ),
            )

            conn.execute(
                """
                update symbol_positions
                set quantity = ?,
                    avg_cost = ?,
                    realized_pnl = ?,
                    total_fees = ?,
                    closed_trade_count = ?,
                    winning_closed_trade_count = ?,
                    updated_at = ?
                where symbol = ?
                """,
                (
                    new_qty,
                    new_avg_cost,
                    cumulative_realized,
                    total_fees,
                    closed_count,
                    winning_count,
                    now,
                    symbol,
                ),
            )
            event_id = int(cursor.lastrowid)

        return self._event_to_dict(
            JournalEvent(
                id=event_id,
                event_type=event_type,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price_value,
                fees=fees,
                gross_value=gross_value,
                net_value=net_value,
                realized_pnl=realized_pnl,
            )
        )

    def record_optimization_note(
        self,
        trade_event_id: int,
        what_worked: str = "",
        what_failed: str = "",
        next_rule_adjustment: str = "",
        confidence: Optional[float] = None,
        setup_tags: Optional[Iterable[str] | str] = None,
    ) -> dict[str, Any]:
        """Attach post-trade feedback to a closed trade event."""
        now = self._utc_now()
        setup_tags_json = self._encode_tags(setup_tags)
        with self._connect() as conn:
            exists = conn.execute(
                "select id from trade_events where id = ?", (trade_event_id,)
            ).fetchone()
            if not exists:
                raise ValueError(f"trade_event_id does not exist: {trade_event_id}")
            cursor = conn.execute(
                """
                insert into trade_optimizations (
                    trade_event_id, created_at, what_worked, what_failed,
                    next_rule_adjustment, confidence, setup_tags
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_event_id,
                    now,
                    what_worked,
                    what_failed,
                    next_rule_adjustment,
                    confidence,
                    setup_tags_json,
                ),
            )

        return {
            "id": int(cursor.lastrowid),
            "trade_event_id": int(trade_event_id),
            "what_worked": what_worked,
            "what_failed": what_failed,
            "next_rule_adjustment": next_rule_adjustment,
            "confidence": confidence,
            "setup_tags": self._decode_tags(setup_tags_json),
        }

    def get_symbol_summary(self, symbol: str) -> dict[str, Any]:
        symbol = self._normalize_symbol(symbol)
        with self._connect() as conn:
            position = self._get_or_create_position(conn, symbol)
            event_count = conn.execute(
                "select count(*) as count from trade_events where symbol = ?",
                (symbol,),
            ).fetchone()["count"]
            lessons = conn.execute(
                """
                select o.*
                from trade_optimizations o
                join trade_events e on e.id = o.trade_event_id
                where e.symbol = ?
                order by o.created_at desc, o.id desc
                limit 10
                """,
                (symbol,),
            ).fetchall()

        closed_count = int(position["closed_trade_count"])
        winning_count = int(position["winning_closed_trade_count"])
        realized_pnl = float(position["realized_pnl"])
        return {
            "symbol": symbol,
            "position_qty": float(position["quantity"]),
            "avg_cost": float(position["avg_cost"]),
            "realized_pnl": realized_pnl,
            "total_fees": float(position["total_fees"]),
            "closed_trade_count": closed_count,
            "win_rate": (winning_count / closed_count) if closed_count else 0.0,
            "avg_realized_pnl": (realized_pnl / closed_count) if closed_count else 0.0,
            "event_count": int(event_count),
            "recent_lessons": [self._optimization_row_to_dict(row) for row in lessons],
        }

    def get_recent_closed_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select *
                from trade_events
                where event_type = 'execution'
                  and side = 'sell'
                  and realized_pnl is not null
                order by created_at desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
            trades = [self._trade_row_to_dict(row) for row in rows]
            for trade in trades:
                trade["optimization"] = self._get_optimization_for_trade(
                    conn, trade["id"]
                )
        return trades

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists trade_events (
                    id integer primary key autoincrement,
                    created_at text not null,
                    trade_date text not null,
                    event_type text not null,
                    symbol text not null,
                    side text not null,
                    mode text not null,
                    quantity real not null default 0,
                    price real,
                    fees real not null default 0,
                    gross_value real,
                    net_value real,
                    realized_pnl real,
                    decision_text text,
                    rationale text,
                    source_run_id text,
                    source text,
                    tags text,
                    note text
                );

                create index if not exists idx_trade_events_symbol_created
                on trade_events(symbol, created_at);

                create index if not exists idx_trade_events_source_run
                on trade_events(source_run_id);

                create table if not exists symbol_positions (
                    symbol text primary key,
                    quantity real not null default 0,
                    avg_cost real not null default 0,
                    realized_pnl real not null default 0,
                    total_fees real not null default 0,
                    closed_trade_count integer not null default 0,
                    winning_closed_trade_count integer not null default 0,
                    updated_at text not null
                );

                create table if not exists trade_optimizations (
                    id integer primary key autoincrement,
                    trade_event_id integer not null,
                    created_at text not null,
                    what_worked text,
                    what_failed text,
                    next_rule_adjustment text,
                    confidence real,
                    setup_tags text,
                    foreign key(trade_event_id) references trade_events(id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def _get_or_create_position(
        self, conn: sqlite3.Connection, symbol: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "select * from symbol_positions where symbol = ?", (symbol,)
        ).fetchone()
        if row is not None:
            return row
        conn.execute(
            """
            insert into symbol_positions (
                symbol, quantity, avg_cost, realized_pnl, total_fees,
                closed_trade_count, winning_closed_trade_count, updated_at
            )
            values (?, 0, 0, 0, 0, 0, 0, ?)
            """,
            (symbol, self._utc_now()),
        )
        return conn.execute(
            "select * from symbol_positions where symbol = ?", (symbol,)
        ).fetchone()

    def _get_optimization_for_trade(
        self, conn: sqlite3.Connection, trade_event_id: int
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            select *
            from trade_optimizations
            where trade_event_id = ?
            order by created_at desc, id desc
            limit 1
            """,
            (trade_event_id,),
        ).fetchone()
        return self._optimization_row_to_dict(row) if row else None

    @staticmethod
    def _trade_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = TradeJournal._decode_tags(data.get("tags"))
        return data

    @staticmethod
    def _optimization_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["setup_tags"] = TradeJournal._decode_tags(data.get("setup_tags"))
        return data

    @staticmethod
    def _event_to_dict(event: JournalEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "side": event.side,
            "quantity": event.quantity,
            "price": event.price,
            "fees": event.fees,
            "gross_value": event.gross_value,
            "net_value": event.net_value,
            "realized_pnl": event.realized_pnl,
        }

    @staticmethod
    def _normalize_symbol(symbol: Optional[str]) -> str:
        if not symbol or not str(symbol).strip():
            raise ValueError("symbol/ticker is required")
        return str(symbol).strip().upper()

    @staticmethod
    def _normalize_side(side: str) -> str:
        normalized = str(side or "hold").strip().lower().replace("_", "-")
        aliases = {
            "no trade": "no-trade",
            "notrade": "no-trade",
            "none": "no-trade",
        }
        normalized = aliases.get(normalized, normalized)
        allowed = {"buy", "sell", "hold", "no-trade"}
        if normalized not in allowed:
            raise ValueError(f"unsupported side: {side}")
        return normalized

    @staticmethod
    def _to_float(value: Any, field: str) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric") from exc

    @staticmethod
    def _encode_tags(tags: Optional[Iterable[str] | str]) -> str:
        if tags is None:
            values: list[str] = []
        elif isinstance(tags, str):
            values = [tag.strip() for tag in tags.split(",") if tag.strip()]
        else:
            values = [str(tag).strip() for tag in tags if str(tag).strip()]
        return json.dumps(values, ensure_ascii=True)

    @staticmethod
    def _decode_tags(raw: Optional[str]) -> list[str]:
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [str(value) for value in values] if isinstance(values, list) else []

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["JournalEvent", "TradeJournal"]
