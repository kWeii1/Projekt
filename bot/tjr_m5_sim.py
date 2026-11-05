"""Deterministic, signal-only approximation of TJR concepts for M5 OHLC CSV data.

This module never connects to a broker and never submits orders. See README.md
for the exact proxy definitions and the CSV schema.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Config:
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    pivot_left: int = 2
    pivot_right: int = 2
    sweep_lookback: int = 80
    max_bars_after_sweep_for_bos: int = 12
    max_bars_after_bos_for_retest: int = 12
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 2.0
    max_trades_per_day: int = 2
    reward_risk_fallback: float = 2.0
    stop_buffer_price: float = 0.0
    require_equilibrium: bool = True
    entry_model: str = "FVG"  # FVG, OB, or EITHER
    htf_bias_bars: int = 48
    require_htf_bias: bool = True
    require_ltf_confirmation: bool = False
    session_start_utc: int | None = None
    session_end_utc: int | None = None


@dataclass
class Position:
    side: str
    entry_time: str
    entry: float
    stop: float
    target: float
    risk_cash: float
    risk_distance: float
    setup: str
    sweep_time: str
    bos_time: str


def read_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"time", "open", "high", "low", "close"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("CSV musi zawierać kolumny: time,open,high,low,close")
        for row in reader:
            bar = Bar(row["time"], float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
            if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
                raise ValueError(f"Nieprawidłowe OHLC w wierszu {reader.line_num}")
            bars.append(bar)
    bars.sort(key=lambda b: b.time)
    return bars


def pivot_events(bars: list[Bar], cfg: Config) -> dict[int, tuple[list[tuple[int, float]], list[tuple[int, float]]]]:
    """Index pivots by the bar when they become knowable, avoiding lookahead."""
    events: dict[int, tuple[list[tuple[int, float]], list[tuple[int, float]]]] = {}
    for p in range(cfg.pivot_left, len(bars) - cfg.pivot_right):
        window = bars[p - cfg.pivot_left : p + cfg.pivot_right + 1]
        left = bars[p - cfg.pivot_left : p]
        right = bars[p + 1 : p + cfg.pivot_right + 1]
        confirmation = p + cfg.pivot_right
        hi, lo = events.setdefault(confirmation, ([], []))
        if all(bars[p].high > x.high for x in left + right):
            hi.append((p, bars[p].high))
        if all(bars[p].low < x.low for x in left + right):
            lo.append((p, bars[p].low))
    return events


def _day(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return stamp[:10]


def _fvg(bars: list[Bar], i: int, side: str) -> tuple[float, float] | None:
    if i < 2:
        return None
    if side == "BUY" and bars[i].low > bars[i - 2].high:
        return bars[i - 2].high, bars[i].low
    if side == "SELL" and bars[i].high < bars[i - 2].low:
        return bars[i].high, bars[i - 2].low
    return None


def _intersects(bar: Bar, zone: tuple[float, float]) -> bool:
    lo, hi = zone
    return bar.low <= hi and bar.high >= lo


def _equilibrium_ok(side: str, entry: float, sweep_extreme: float, bos_level: float) -> bool:
    lo, hi = sorted((sweep_extreme, bos_level))
    eq = (lo + hi) / 2
    return entry <= eq if side == "BUY" else entry >= eq


def _htf_bias(bars: list[Bar], index: int, cfg: Config) -> str:
    """Closed-bar directional proxy: sequence of HTF-window highs/lows."""
    start = max(0, index - cfg.htf_bias_bars + 1)
    window = bars[start:index + 1]
    if len(window) < 4:
        return "NEUTRAL"
    half = max(2, len(window) // 2)
    older, newer = window[:-half], window[-half:]
    if not older or not newer:
        return "NEUTRAL"
    old_high, new_high = max(x.high for x in older), max(x.high for x in newer)
    old_low, new_low = min(x.low for x in older), min(x.low for x in newer)
    if new_high > old_high and new_low > old_low:
        return "BULLISH"
    if new_high < old_high and new_low < old_low:
        return "BEARISH"
    return "NEUTRAL"


def _ob_zone(bars: list[Bar], bos_index: int, side: str, lookback: int = 12) -> tuple[float, float] | None:
    """Last opposing candle before BOS, using only bars already closed."""
    for j in range(bos_index - 1, max(-1, bos_index - lookback - 1), -1):
        candle = bars[j]
        bearish = candle.close < candle.open
        bullish = candle.close > candle.open
        if (side == "BUY" and bearish) or (side == "SELL" and bullish):
            return min(candle.open, candle.close), max(candle.open, candle.close)
    return None


def _in_session(stamp: str, cfg: Config) -> bool:
    if cfg.session_start_utc is None or cfg.session_end_utc is None:
        return True
    try:
        hour = datetime.fromisoformat(stamp.replace("Z", "+00:00")).hour
    except ValueError:
        return False
    if cfg.session_start_utc <= cfg.session_end_utc:
        return cfg.session_start_utc <= hour < cfg.session_end_utc
    return hour >= cfg.session_start_utc or hour < cfg.session_end_utc


def simulate(bars: list[Bar], starting_balance: float, cfg: Config) -> dict:
    if starting_balance <= 0:
        raise ValueError("Saldo początkowe musi być większe od zera.")
    if not bars:
        raise ValueError("Brak danych OHLC.")
    if cfg.entry_model not in {"FVG", "OB", "EITHER"}:
        raise ValueError("entry_model musi być FVG, OB albo EITHER")

    balance = starting_balance
    position: Position | None = None
    pending: dict | None = None
    trades: list[dict] = []
    day_start_balance = balance
    active_day = _day(bars[0].time)
    trades_today = 0
    stopped_today = False
    known_highs: list[tuple[int, float]] = []
    known_lows: list[tuple[int, float]] = []
    pivots_by_bar = pivot_events(bars, cfg)

    def close_position(stamp: str, raw_exit: float, reason: str) -> None:
        nonlocal balance, position, stopped_today
        assert position is not None
        direction = 1 if position.side == "BUY" else -1
        r_mult = ((raw_exit - position.entry) * direction) / position.risk_distance
        pnl = position.risk_cash * r_mult
        balance += pnl
        trades.append({**asdict(position), "exit_time": stamp, "exit": raw_exit, "exit_reason": reason,
                       "r_multiple": round(r_mult, 4), "pnl": round(pnl, 2), "balance_after": round(balance, 2)})
        if pnl < 0 and (day_start_balance - balance) / day_start_balance >= cfg.max_daily_loss_pct / 100:
            stopped_today = True
        position = None

    for i, bar in enumerate(bars):
        day = _day(bar.time)
        if day != active_day:
            active_day, day_start_balance, trades_today, stopped_today = day, balance, 0, False
        new_highs, new_lows = pivots_by_bar.get(i, ([], []))
        known_highs.extend(new_highs)
        known_lows.extend(new_lows)

        # Conservative OHLC fill policy: if stop and target are both crossed in
        # one bar, count the stop first because intrabar order is unknown.
        had_position = position is not None
        if position:
            if position.side == "BUY":
                if bar.low <= position.stop:
                    close_position(bar.time, position.stop, "SL")
                elif bar.high >= position.target:
                    close_position(bar.time, position.target, "TP")
            else:
                if bar.high >= position.stop:
                    close_position(bar.time, position.stop, "SL")
                elif bar.low <= position.target:
                    close_position(bar.time, position.target, "TP")
            if position or had_position:
                continue

        if stopped_today or trades_today >= cfg.max_trades_per_day:
            pending = None
            continue

        recent_highs = [(p, v) for p, v in known_highs if i - cfg.sweep_lookback <= p < i]
        recent_lows = [(p, v) for p, v in known_lows if i - cfg.sweep_lookback <= p < i]

        if pending and pending.get("phase") == "retest":
            age = i - pending["bos_index"]
            if age > cfg.max_bars_after_bos_for_retest:
                pending = None
            elif age > 0:
                zone = pending["zone"]
                midpoint = sum(zone) / 2
                bias = _htf_bias(bars, i, cfg)
                bias_ok = not cfg.require_htf_bias or bias == ("BULLISH" if pending["side"] == "BUY" else "BEARISH")
                session_ok = _in_session(bar.time, cfg)
                ltf_ok = not cfg.require_ltf_confirmation or (bar.close > bar.open if pending["side"] == "BUY" else bar.close < bar.open)
                if (_intersects(bar, zone) and bias_ok and session_ok and ltf_ok
                        and (not cfg.require_equilibrium or _equilibrium_ok(pending["side"], midpoint, pending["sweep_extreme"], pending["bos_level"]))):
                    side = pending["side"]
                    stop = pending["sweep_extreme"] - cfg.stop_buffer_price if side == "BUY" else pending["sweep_extreme"] + cfg.stop_buffer_price
                    risk_distance = midpoint - stop if side == "BUY" else stop - midpoint
                    if risk_distance > 0:
                        target_candidates = [v for p, v in (recent_highs if side == "BUY" else recent_lows)
                                             if (v > midpoint if side == "BUY" else v < midpoint)]
                        target = (min(target_candidates) if side == "BUY" else max(target_candidates)) if target_candidates else None
                        if target is None:
                            target = midpoint + risk_distance * cfg.reward_risk_fallback * (1 if side == "BUY" else -1)
                        risk_cash = balance * cfg.risk_per_trade_pct / 100
                        position = Position(side, bar.time, midpoint, stop, target, risk_cash, risk_distance,
                                            f"sweep+BOS+{pending['zone_type']}" + ("+EQ" if cfg.require_equilibrium else ""),
                                            pending["sweep_time"], pending["bos_time"])
                        trades_today += 1
                        pending = None
                        continue

        # A sweep is a wick beyond a confirmed pivot followed by a close back
        # inside. Wait for BOS to cross the last opposing confirmed pivot.
        if pending is None and recent_lows and recent_highs and _in_session(bar.time, cfg):
            low_p, low_v = recent_lows[-1]
            high_p, high_v = recent_highs[-1]
            if bar.low < low_v and bar.close > low_v:
                pending = {"phase": "sweep", "side": "BUY", "sweep_index": i, "sweep_time": bar.time,
                           "sweep_extreme": bar.low, "bos_level": high_v, "bos_pivot_index": high_p}
            elif bar.high > high_v and bar.close < high_v:
                pending = {"phase": "sweep", "side": "SELL", "sweep_index": i, "sweep_time": bar.time,
                           "sweep_extreme": bar.high, "bos_level": low_v, "bos_pivot_index": low_p}

        if pending and pending.get("phase") == "sweep":
            if i - pending["sweep_index"] > cfg.max_bars_after_sweep_for_bos:
                pending = None
            elif i > pending["sweep_index"]:
                side = pending["side"]
                crossed = bar.close > pending["bos_level"] if side == "BUY" else bar.close < pending["bos_level"]
                bias = _htf_bias(bars, i, cfg)
                bias_ok = not cfg.require_htf_bias or bias == ("BULLISH" if side == "BUY" else "BEARISH")
                fvg = _fvg(bars, i, side)
                ob = _ob_zone(bars, i, side)
                zone = fvg if cfg.entry_model == "FVG" else ob if cfg.entry_model == "OB" else (fvg or ob)
                zone_type = "FVG" if zone == fvg else "OB"
                if crossed and bias_ok and zone:
                    pending.update({"phase": "retest", "bos_index": i, "bos_time": bar.time, "zone": zone,
                                    "zone_type": zone_type, "htf_bias": bias})

    if position:
        close_position(bars[-1].time, bars[-1].close, "END_OF_DATA")
    wins = sum(1 for x in trades if x["pnl"] > 0)
    return {"symbol": cfg.symbol, "timeframe": cfg.timeframe, "config": asdict(cfg),
            "starting_balance": starting_balance, "ending_balance": round(balance, 2),
            "net_pnl": round(balance - starting_balance, 2), "trades": len(trades),
            "wins": wins, "losses": len(trades) - wins,
            "win_rate_pct": round(wins * 100 / len(trades), 2) if trades else 0,
            "trade_log": trades,
            "limitations": ["symulator OHLC, brak spreadu, prowizji i poślizgu",
                            "w tym samym barze SL ma pierwszeństwo przed TP",
                            "brak kalendarza news; filtr sesji jest opcjonalny i wyłączony, jeśli nie podano godzin UTC",
                            "HTF bias, OB, sesja i potwierdzenie LTF są mechanicznymi proxy do walidacji, nie regułami w pełni zrekonstruowanymi z nagrań",
                            "kontrakty/loty XAUUSD zależą od brokera i nie są tu wyliczane"]}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Symulator sygnałów TJR na świecach M5; nie łączy się z brokerem.")
    parser.add_argument("csv", type=Path, help="OHLC CSV: time,open,high,low,close")
    parser.add_argument("--balance", type=float, required=True, help="Saldo startowe symulatora")
    parser.add_argument("--output", type=Path, default=Path("tjr_m5_report.json"))
    args = parser.parse_args()
    result = simulate(read_csv(args.csv), args.balance, Config())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "trade_log"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
