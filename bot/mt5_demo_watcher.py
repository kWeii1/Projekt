"""MT5 XAUUSD M5 paper watcher with optional demo-only order execution.

Order execution is disabled unless --trade-demo is passed. It requires an MT5
demo account on every entry, uses broker-calculated risk sizing, server-side SL
and TP, and a persistent per-bar intent journal to avoid duplicate requests.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

from tjr_m5_sim import Bar, Config, simulate

MAGIC = 23102326
RISK_PER_TRADE = 0.01
DAILY_LOSS_LIMIT = 0.02
MAX_TRADES_PER_DAY = 2


def strategy_magic(symbol: str) -> int:
    value = MAGIC
    for char in symbol.upper():
        value = (value * 31 + ord(char)) % 2_000_000_000
    return value or MAGIC


def acquire_single_instance(symbol: str) -> int | None:
    """Hold a machine-wide mutex so two app copies cannot trade together."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())[:24]
    handle = create_mutex(None, True, rf"Global\TradeLog_MT5_Demo_Watcher_{safe_symbol}")
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not create the TradeLog watcher mutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise SystemExit("Another TradeLog MT5 watcher is already running; refusing a second instance.")
    return int(handle)


def release_single_instance(handle: int | None) -> None:
    if handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (ctypes.c_void_p,)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    release_mutex(ctypes.c_void_p(handle))
    close_handle(ctypes.c_void_p(handle))


def notify_entry(symbol: str, side: str, volume: float, price: float, stop: float,
                 target: float, ticket: int | None) -> None:
    """Show a local Windows notification after MT5 confirms a demo fill."""
    if os.name != "nt":
        return
    script = Path(__file__).with_name("notify-entry.ps1")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not script.is_file() or not powershell.is_file():
        raise RuntimeError("Windows notification helper or PowerShell was not found.")
    direction = "LONG" if side == "BUY" else "SHORT"
    title = f"TradeLog • pozycja otwarta: {direction} {symbol}"
    body = f"Wolumen: {volume:g} | Wejście: {price:.2f} | SL: {stop:.2f} | TP: {target:.2f}"
    if ticket:
        body += f" | Zlecenie: {ticket}"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                      "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script), title, body],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags,
                     close_fds=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def journal_intents(path: Path, symbol: str) -> set[int]:
    """Return bar epochs with a recorded intent/result to prevent resubmission."""
    processed: set[int] = set()
    if not path.exists():
        return processed
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if event.get("symbol") == symbol and event.get("kind") in {"order_intent", "order_result", "order_skipped"}:
                    processed.add(int(event["bar_epoch"]))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
    return processed


def is_demo_account() -> bool:
    account = mt5.account_info()
    return account is not None and int(account.trade_mode) == int(mt5.ACCOUNT_TRADE_MODE_DEMO)


def last_sunday_utc(year: int, month: int) -> datetime:
    last_day = datetime(year, month, 31, 1, tzinfo=timezone.utc)
    return last_day - timedelta(days=(last_day.weekday() + 1) % 7)


def server_day_key(server_epoch: int, server_name: str = "") -> str:
    """Use FundedNext's seasonal GMT+2/GMT+3 reset; other servers use UTC."""
    moment = datetime.fromtimestamp(server_epoch, timezone.utc)
    if "fundednext" in server_name.casefold():
        daylight_start = last_sunday_utc(moment.year, 3)
        daylight_end = last_sunday_utc(moment.year, 10)
        daylight_start = daylight_start.replace(hour=1)
        daylight_end = daylight_end.replace(hour=1)
        offset_hours = 3 if daylight_start <= moment < daylight_end else 2
        moment += timedelta(hours=offset_hours)
    return moment.date().isoformat()


def ensure_risk_state(path: Path, server_epoch: int, equity: float, server_name: str = "") -> dict:
    day_key = server_day_key(server_epoch, server_name)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("day") == day_key and float(saved.get("starting_equity", 0)) > 0:
                return saved
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    state = {"day": day_key, "starting_equity": float(equity), "reference": "first bot activation or first quote of MT5 server day"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def count_today_intents(path: Path, symbol: str, day_key: str, server_name: str = "") -> int:
    count = 0
    if not path.exists():
        return count
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if (event.get("kind") == "order_intent" and event.get("symbol") == symbol
                        and server_day_key(int(event["bar_epoch"]), server_name) == day_key):
                    count += 1
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
                continue
    return count


def floor_volume(raw: float, info) -> float:
    step = float(info.volume_step)
    if step <= 0:
        return 0.0
    steps = math.floor((raw + step * 1e-9) / step)
    volume = steps * step
    decimals = max(0, min(8, round(-math.log10(step)) if step < 1 else 0))
    return round(volume, decimals)


def filling_mode(info) -> int | None:
    # MQL5 SYMBOL_FILLING_MODE flags: FOK=1, IOC=2. Python order filling
    # enum values differ (ORDER_FILLING_FOK=0, IOC=1, RETURN=2).
    flags = int(info.filling_mode)
    if flags & 2:
        return mt5.ORDER_FILLING_IOC
    if flags & 1:
        return mt5.ORDER_FILLING_FOK
    if int(info.trade_exemode) != int(mt5.SYMBOL_TRADE_EXECUTION_MARKET):
        return mt5.ORDER_FILLING_RETURN
    return None


def submit_demo_order(symbol: str, bar: Bar, signal: dict, max_spread: float,
                      max_entry_drift_pct: float, deviation: int, journal: Path,
                      bar_epoch: int) -> None:
    if not is_demo_account():
        raise RuntimeError("Safety stop: active MT5 account is not demo; order blocked.")
    account = mt5.account_info()
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if account is None or info is None or tick is None:
        raise RuntimeError(f"Missing account/symbol/tick data: {mt5.last_error()}")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select {symbol}: {mt5.last_error()}")
    if int(info.trade_mode) != int(mt5.SYMBOL_TRADE_MODE_FULL):
        raise RuntimeError("Symbol is not in full trading mode; order blocked.")
    spread = float(tick.ask - tick.bid)
    if int(tick.time) < bar_epoch or int(tick.time) - bar_epoch > 600:
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "stale_or_misaligned_quote",
                               "tick_epoch": int(tick.time)})
        return
    if spread <= 0 or spread > max_spread:
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "spread_gate", "spread": spread,
                               "max_spread": max_spread})
        return

    side = signal["side"]
    is_buy = side == "BUY"
    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    price = float(tick.ask if is_buy else tick.bid)
    stop = round(float(signal["stop"]), int(info.digits))
    target = round(float(signal["target"]), int(info.digits))
    risk_distance = price - stop if is_buy else stop - price
    if risk_distance <= 0 or (is_buy and target <= price) or (not is_buy and target >= price):
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "invalid_live_levels", "price": price,
                               "stop": stop, "target": target})
        return
    drift = abs(price - float(signal["entry"]))
    max_drift = max(0.0, risk_distance * max_entry_drift_pct / 100)
    if drift > max_drift:
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "entry_drift_gate", "price": price,
                               "signal_entry": signal["entry"], "max_drift": max_drift})
        return

    stop_gap = float(info.trade_stops_level) * float(info.point)
    if ((price - stop < stop_gap) or (target - price < stop_gap)) if is_buy else ((stop - price < stop_gap) or (price - target < stop_gap)):
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "broker_stop_distance", "price": price,
                               "stop": stop, "target": target, "min_gap": stop_gap})
        return

    one_lot_result = mt5.order_calc_profit(order_type, symbol, 1.0, price, stop)
    if one_lot_result is None or not math.isfinite(float(one_lot_result)):
        raise RuntimeError(f"Broker risk calculation failed: {mt5.last_error()}")
    loss_per_lot = abs(float(one_lot_result))
    risk_cash = float(account.balance) * RISK_PER_TRADE
    if loss_per_lot <= 0 or risk_cash <= 0:
        raise RuntimeError("Invalid risk sizing inputs; order blocked.")
    volume = floor_volume(risk_cash / loss_per_lot, info)
    if volume < float(info.volume_min) or volume > float(info.volume_max):
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "volume_out_of_range", "volume": volume,
                               "min_volume": info.volume_min, "max_volume": info.volume_max})
        return
    actual_risk = loss_per_lot * volume
    if actual_risk > risk_cash * 1.000001:
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": "min_step_exceeds_risk", "risk": actual_risk,
                               "risk_cap": risk_cash})
        return

    filling = filling_mode(info)
    if filling is None:
        raise RuntimeError("Broker exposes no supported market filling mode; order blocked.")
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
               "type": order_type, "price": round(price, int(info.digits)), "sl": stop, "tp": target,
               "deviation": deviation, "magic": strategy_magic(symbol), "comment": f"TJR_{symbol}"[:31],
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
    check = mt5.order_check(request)
    if check is None or int(check.retcode) != 0:
        reason = "order_check_failed" if check is None else f"order_check_retcode_{check.retcode}"
        append_event(journal, {"observed_at": utc_now(), "kind": "order_skipped", "symbol": symbol,
                               "bar_epoch": bar_epoch, "reason": reason,
                               "comment": None if check is None else check.comment})
        return

    intent = {"observed_at": utc_now(), "kind": "order_intent", "symbol": symbol,
              "timeframe": "M5", "bar_epoch": bar_epoch, "bar_time": bar.time,
              "magic": strategy_magic(symbol), "side": side, "volume": volume, "price": request["price"],
              "stop": stop, "target": target, "risk_cash_cap": round(risk_cash, 2),
              "estimated_risk": round(actual_risk, 2), "spread": spread,
              "execution": "demo_only"}
    append_event(journal, intent)  # Persist before one and only one send attempt.
    result = mt5.order_send(request)
    accepted = result is not None and int(result.retcode) in (
        int(mt5.TRADE_RETCODE_DONE), int(mt5.TRADE_RETCODE_DONE_PARTIAL))
    append_event(journal, {"observed_at": utc_now(), "kind": "order_result", "symbol": symbol,
                           "bar_epoch": bar_epoch, "accepted": accepted,
                           "retcode": None if result is None else int(result.retcode),
                           "comment": None if result is None else result.comment,
                           "order": None if result is None else int(result.order),
                           "deal": None if result is None else int(result.deal),
                           "volume": None if result is None else float(result.volume),
                           "price": None if result is None else float(result.price)})
    print(f"ORDER {'ACCEPTED' if accepted else 'REJECTED'} {side} {volume} {symbol}: "
          f"{None if result is None else result.comment}", flush=True)
    if accepted:
        try:
            notify_entry(symbol, side, float(result.volume), float(result.price), stop, target,
                         int(result.order) if result.order else None)
            append_event(journal, {"observed_at": utc_now(), "kind": "notification_dispatched",
                                   "symbol": symbol, "bar_epoch": bar_epoch,
                                   "order": int(result.order) if result.order else None})
        except Exception as exc:
            append_event(journal, {"observed_at": utc_now(), "kind": "notification_error",
                                   "symbol": symbol, "bar_epoch": bar_epoch, "error": str(exc)})
            print(f"Position opened, but local notification failed: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 XAUUSD M5 signal watcher; demo execution is opt-in.")
    parser.add_argument("--terminal", required=True, type=Path)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--poll-seconds", type=int, default=3)
    parser.add_argument("--history-bars", type=int, default=10000)
    parser.add_argument("--journal", type=Path, default=Path("bot/mt5_signal_journal.jsonl"))
    parser.add_argument("--risk-state", type=Path, default=Path("bot/risk_state.json"))
    parser.add_argument("--trade-demo", action="store_true", help="Enable demo-only order sending")
    parser.add_argument("--entry-model", choices=("FVG", "OB", "EITHER"), default="FVG")
    parser.add_argument("--no-htf-bias", action="store_true", help="Disable experimental closed-bar HTF directional proxy")
    parser.add_argument("--require-ltf-confirmation", action="store_true", help="Require entry-bar directional close")
    parser.add_argument("--session-start-utc", type=int)
    parser.add_argument("--session-end-utc", type=int)
    parser.add_argument("--max-spread-price", type=float, default=0.50)
    parser.add_argument("--max-entry-drift-pct", type=float, default=5.0)
    parser.add_argument("--deviation-points", type=int, default=20)
    args = parser.parse_args()
    if args.poll_seconds < 1 or args.poll_seconds > 60:
        raise SystemExit("--poll-seconds must be in [1, 60]")
    if args.history_bars < 500 or args.history_bars > 100000:
        raise SystemExit("--history-bars must be in [500, 100000]")
    if args.max_spread_price <= 0 or args.max_entry_drift_pct < 0 or args.deviation_points < 0:
        raise SystemExit("Spread, drift, or deviation option is invalid.")
    if not args.terminal.is_file():
        raise SystemExit(f"MT5 terminal not found: {args.terminal}")
    mutex_handle = acquire_single_instance(args.symbol)
    if not mt5.initialize(path=str(args.terminal), timeout=20000):
        release_single_instance(mutex_handle)
        raise SystemExit(f"Cannot connect to MT5: {mt5.last_error()}")

    try:
        account = mt5.account_info()
        if account is None:
            raise SystemExit(f"Cannot read MT5 account: {mt5.last_error()}")
        if int(account.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO):
            raise SystemExit("Safety stop: active account is not demo; bot refuses to run.")
        if not mt5.symbol_select(args.symbol, True):
            raise SystemExit(f"Symbol unavailable in Market Watch: {args.symbol} ({mt5.last_error()})")

        if (args.session_start_utc is None) != (args.session_end_utc is None):
            raise SystemExit("Set both session start and end UTC hours, or neither.")
        if args.session_start_utc is not None and (not 0 <= args.session_start_utc <= 23 or not 0 <= args.session_end_utc <= 23):
            raise SystemExit("Session hours must be between 0 and 23 UTC.")
        cfg = Config(entry_model=args.entry_model, require_htf_bias=not args.no_htf_bias,
                     require_ltf_confirmation=args.require_ltf_confirmation,
                     session_start_utc=args.session_start_utc, session_end_utc=args.session_end_utc)
        processed_bars = journal_intents(args.journal, args.symbol)
        last_closed_epoch = None
        startup_tick = mt5.symbol_info_tick(args.symbol)
        if startup_tick is None:
            raise SystemExit(f"Cannot read MT5 quote for daily risk baseline: {mt5.last_error()}")
        risk_state = ensure_risk_state(args.risk_state, int(startup_tick.time), float(account.equity), str(account.server))
        print(f"Connected to MT5 demo; {args.symbol} M5; mode={'AUTO DEMO' if args.trade_demo else 'SIGNALS ONLY'}; "
              f"risk=1%, daily stop=2%, max trades/day=2", flush=True)
        if args.trade_demo:
            print(f"Auto-demo execution enabled. Spread cap={args.max_spread_price}; one strategy position max; "
                  f"day={risk_state['day']}; equity baseline={risk_state['starting_equity']:.2f}.", flush=True)
        else:
            print("Signals-only mode; this process will not send orders.", flush=True)

        last_risk_snapshot_at = 0.0
        while True:
            if time.monotonic() - last_risk_snapshot_at >= 30:
                risk_tick = mt5.symbol_info_tick(args.symbol)
                risk_account = mt5.account_info()
                if risk_tick is not None and risk_account is not None:
                    risk_state = ensure_risk_state(args.risk_state, int(risk_tick.time), float(risk_account.equity), str(risk_account.server))
                    drawdown = max(0.0, float(risk_state["starting_equity"]) - float(risk_account.equity))
                    loss_limit_cash = float(risk_state["starting_equity"]) * DAILY_LOSS_LIMIT
                    append_event(args.journal, {"observed_at": utc_now(), "kind": "risk_snapshot",
                                   "symbol": args.symbol, "mt5_server_day": risk_state["day"],
                                   "currency": str(risk_account.currency),
                                   "starting_equity": round(float(risk_state["starting_equity"]), 2),
                                   "equity": round(float(risk_account.equity), 2),
                                   "equity_drawdown": round(drawdown, 2),
                                   "limit_cash": round(loss_limit_cash, 2),
                                   "remaining_cash": round(max(0.0, loss_limit_cash - drawdown), 2),
                                   "used_pct": round(drawdown / loss_limit_cash * 100, 2) if loss_limit_cash > 0 else 0.0})
                    last_risk_snapshot_at = time.monotonic()
            rates = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_M5, 1, args.history_bars)
            if rates is None or len(rates) < 500:
                print(f"Waiting for M5 history: {mt5.last_error()}", flush=True)
                time.sleep(args.poll_seconds)
                continue
            bars = [Bar(datetime.fromtimestamp(int(row["time"]), timezone.utc).isoformat(),
                        float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
                    for row in rates]
            closed_epoch = int(rates[-1]["time"])
            if last_closed_epoch is None:
                # On startup, observe the current last closed bar but never
                # trade a signal that formed before this process started.
                last_closed_epoch = closed_epoch
                print(f"Armed on next M5 close; latest closed bar={bars[-1].time}", flush=True)
            elif closed_epoch != last_closed_epoch:
                last_closed_epoch = closed_epoch
                newest = bars[-1]
                result = simulate(bars, float(account.balance), cfg)
                signals = [trade for trade in result["trade_log"] if trade["entry_time"] == newest.time]
                tick = mt5.symbol_info_tick(args.symbol)
                spread = None if tick is None else float(tick.ask - tick.bid)
                if not signals:
                    print(f"{utc_now()} closed_bar={newest.time} signal=none spread={spread}", flush=True)
                for signal in signals:
                    append_event(args.journal, {"observed_at": utc_now(), "kind": "signal_proposed",
                                   "symbol": args.symbol, "timeframe": "M5", "bar_time": newest.time,
                                   "bar_epoch": closed_epoch, "spread_price": spread,
                                   "signal": signal,
                                   "execution": "demo_only" if args.trade_demo else "read_only"})
                    if not args.trade_demo:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "paper_signal",
                                       "symbol": args.symbol, "timeframe": "M5", "bar_time": newest.time,
                                       "bar_epoch": closed_epoch, "spread_price": spread,
                                       "signal": signal, "execution": "read_only"})
                        print(json.dumps(signal, ensure_ascii=False), flush=True)
                        continue
                    if closed_epoch in processed_bars:
                        continue
                    quote = mt5.symbol_info_tick(args.symbol)
                    if quote is None or int(quote.time) < closed_epoch or int(quote.time) - closed_epoch > 600:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "order_skipped",
                                       "symbol": args.symbol, "bar_epoch": closed_epoch,
                                       "reason": "stale_or_misaligned_quote",
                                       "tick_epoch": None if quote is None else int(quote.time)})
                        processed_bars.add(closed_epoch)
                        continue
                    account = mt5.account_info()
                    if account is None:
                        raise SystemExit(f"Cannot refresh account state: {mt5.last_error()}")
                    risk_state = ensure_risk_state(args.risk_state, int(quote.time), float(account.equity), str(account.server))
                    entries_today = count_today_intents(args.journal, args.symbol, risk_state["day"], str(account.server))
                    drawdown = float(risk_state["starting_equity"]) - float(account.equity)
                    loss_limit_cash = float(risk_state["starting_equity"]) * DAILY_LOSS_LIMIT
                    if drawdown >= loss_limit_cash:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "order_skipped",
                                       "symbol": args.symbol, "bar_epoch": closed_epoch,
                                       "reason": "daily_loss_limit", "equity_drawdown": round(drawdown, 2),
                                       "limit_cash": round(loss_limit_cash, 2), "currency": str(account.currency),
                                       "mt5_server_day": risk_state["day"]})
                        processed_bars.add(closed_epoch)
                        continue
                    if entries_today >= MAX_TRADES_PER_DAY:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "order_skipped",
                                       "symbol": args.symbol, "bar_epoch": closed_epoch,
                                       "reason": "max_trades_per_day", "entries_today": entries_today})
                        processed_bars.add(closed_epoch)
                        continue
                    if not is_demo_account():
                        raise SystemExit("Safety stop: account changed from demo; no order sent.")
                    open_positions = [pos for pos in (mt5.positions_get(symbol=args.symbol) or ())
                                      if int(getattr(pos, "magic", -1)) == strategy_magic(args.symbol)]
                    if open_positions:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "order_skipped",
                                       "symbol": args.symbol, "bar_epoch": closed_epoch,
                                       "reason": "strategy_position_already_open",
                                       "tickets": [int(p.ticket) for p in open_positions]})
                        processed_bars.add(closed_epoch)
                        continue
                    try:
                        submit_demo_order(args.symbol, newest, signal, args.max_spread_price,
                                          args.max_entry_drift_pct, args.deviation_points,
                                          args.journal, closed_epoch)
                    except Exception as exc:
                        append_event(args.journal, {"observed_at": utc_now(), "kind": "order_skipped",
                                       "symbol": args.symbol, "bar_epoch": closed_epoch,
                                       "reason": "execution_guard_error", "error": str(exc)})
                        print(f"Entry blocked by safety guard: {exc}", flush=True)
                    processed_bars.add(closed_epoch)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("Bot stopped by user.", flush=True)
    finally:
        mt5.shutdown()
        release_single_instance(mutex_handle)


if __name__ == "__main__":
    main()
