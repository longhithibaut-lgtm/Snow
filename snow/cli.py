"""Command line: ``snow backtest | run | status | flatten | stop | resume``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from snow.config import BotConfig, Config, load_config

log = logging.getLogger("snow")


def load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from ``path`` into the environment; variables already set win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def setup_logging(log_dir: str, verbose: bool) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(Path(log_dir) / "snow.log", encoding="utf-8")],
    )
    for noisy in ("urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Deprecation noise from inside websockets (pulled in by alpaca-py), not actionable here.
    warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"websockets\..*")


def selected_bots(cfg: Config, name: str | None) -> list[BotConfig]:
    return [cfg.bot(name)] if name else cfg.bots


def mode_label(live: bool) -> str:
    return "LIVE" if live else "paper"


# --- backtest -----------------------------------------------------------------

def warmup_start(bot: BotConfig, start: str) -> str:
    from snow.strategies import make_strategy

    bars = make_strategy(bot.strategy.name, bot.strategy.params).warmup + bot.risk.vol_lookback + 5
    days = bars * (1.6 if bot.timeframe == "1Day" else 1 / 4)
    return (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def cmd_backtest(cfg: Config, args) -> int:
    import pandas as pd

    from snow.backtest import run_backtest
    from snow.data import YahooData

    yahoo = YahooData()
    out_root = Path(args.out or Path(cfg.log_dir) / "backtests")
    for bot in selected_bots(cfg, args.bot):
        fetch_from = warmup_start(bot, args.start)
        histories = {s: yahoo.get_history(s, bot.timeframe, fetch_from, args.end) for s in bot.symbols}
        result = run_backtest(bot, histories, cfg.backtest, start=args.start, end=args.end)

        print(f"\n=== {bot.name} - {bot.strategy.name} {bot.strategy.params} sur {', '.join(bot.symbols)} ===")
        rows = []
        for label, (strat, bench) in result.metrics(args.split).items():
            rows.append({"segment": label, "série": "stratégie", **strat.row()})
            rows.append({"segment": label, "série": "buy & hold", **bench.row()})
        print(pd.DataFrame(rows).to_string(index=False))
        exposure = result.exposure.mean() if result.exposure is not None else 0
        print(f"ordres exécutés: {len(result.fills)}  frais: {result.fees:,.2f} $  "
              f"exposition moyenne: {exposure:.0%}")

        out = out_root / f"{bot.name}_{datetime.now():%Y%m%d_%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        pd.concat([result.equity, result.benchmark], axis=1).to_csv(out / "equity.csv")
        result.fills_frame().to_csv(out / "fills.csv", index=False)
        print(f"courbe et ordres: {out}")
    return 0


# --- live ---------------------------------------------------------------------

def make_broker(bot_symbols: list[str], live: bool):
    from snow.brokers import AlpacaBroker

    return AlpacaBroker(bot_symbols, live=live)


def run_all(cfg: Config, args) -> bool:
    from snow.data import AlpacaData
    from snow.engine import run_bot

    data = AlpacaData()
    ok = True
    for bot in selected_bots(cfg, args.bot):
        try:
            broker = make_broker(bot.symbols, args.live)
            report = run_bot(cfg, bot, broker, data, mode=mode_label(args.live), dry_run=args.dry_run)
            print(report.summary("dry-run" if args.dry_run else mode_label(args.live)))
            ok = ok and not report.errors
        except Exception as exc:  # noqa: BLE001 - one bot failing must not stop the others
            from snow.notify import notify

            log.exception("bot %s failed", bot.name)
            notify(f"[snow:{bot.name}] run failed: {exc}")
            ok = False
    return ok


def cmd_run(cfg: Config, args) -> int:
    if args.live:
        log.warning("LIVE mode: orders use real money")
    if not args.loop:
        return 0 if run_all(cfg, args) else 1
    log.info("loop: one pass every %d minute(s); Ctrl+C to stop", args.every)
    while True:
        started = time.monotonic()
        run_all(cfg, args)
        time.sleep(max(0, args.every * 60 - (time.monotonic() - started)))


def cmd_status(cfg: Config, args) -> int:
    from snow.engine import KILL_SWITCH

    if (Path(cfg.state_dir) / KILL_SWITCH).exists():
        print("kill switch ON: no bot will trade (snow resume to lift it)")
    all_symbols = [s for b in cfg.bots for s in b.symbols]
    broker = make_broker(all_symbols, args.live)
    acct = broker.account()
    print(f"compte {mode_label(args.live)}: equity {acct.equity:,.2f} $  cash {acct.cash:,.2f} $  "
          f"veille {acct.last_equity:,.2f} $ ({acct.equity / acct.last_equity - 1:+.2%})")
    positions = broker.positions()
    for bot in cfg.bots:
        held = {s: q for s, q in positions.items() if s in bot.symbols}
        print(f"  {bot.name}: " + (", ".join(f"{s} {q:g}" for s, q in held.items()) or "aucune position"))
    others = {s: q for s, q in positions.items() if s not in all_symbols}
    if others:
        print("  hors bots: " + ", ".join(f"{s} {q:g}" for s, q in others.items()))
    return 0


def cmd_flatten(cfg: Config, args) -> int:
    bot = cfg.bot(args.bot)
    if not args.yes:
        print(f"this closes every {bot.name} position ({', '.join(bot.symbols)}) at market; add --yes to confirm")
        return 1
    closed = make_broker(bot.symbols, args.live).close_all(bot.symbols)
    print("closed: " + (", ".join(closed) or "nothing held"))
    return 0


def cmd_stop(cfg: Config, args) -> int:
    from snow.engine import KILL_SWITCH

    path = Path(cfg.state_dir) / KILL_SWITCH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stopped {datetime.now().isoformat()}\n", encoding="utf-8")
    print(f"kill switch ON ({path}): runs place no order until `snow resume`")
    return 0


def cmd_resume(cfg: Config, args) -> int:
    from snow.engine import KILL_SWITCH

    (Path(cfg.state_dir) / KILL_SWITCH).unlink(missing_ok=True)
    print("kill switch OFF")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="snow", description="Trading bots on Alpaca (stocks and crypto)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backtest", help="replay the bots on Yahoo Finance history")
    b.add_argument("--bot")
    b.add_argument("--start", required=True, help="YYYY-MM-DD")
    b.add_argument("--end", help="YYYY-MM-DD (default: today)")
    b.add_argument("--split", help="YYYY-MM-DD: also report before/after this date (out-of-sample check)")
    b.add_argument("--out", help="folder for equity.csv and fills.csv")

    r = sub.add_parser("run", help="one pass of the bots (or a loop)")
    r.add_argument("--bot")
    r.add_argument("--dry-run", action="store_true", help="plan and log the orders without sending them")
    r.add_argument("--live", action="store_true", help="real money; also needs SNOW_LIVE_CONFIRM=yes")
    r.add_argument("--loop", action="store_true", help="keep running, one pass every --every minutes")
    r.add_argument("--every", type=int, default=60, help="minutes between passes with --loop (default 60)")

    s = sub.add_parser("status", help="account and positions per bot")
    s.add_argument("--live", action="store_true")

    f = sub.add_parser("flatten", help="close every position of one bot at market")
    f.add_argument("--bot", required=True)
    f.add_argument("--live", action="store_true")
    f.add_argument("--yes", action="store_true")

    sub.add_parser("stop", help="kill switch: no bot places orders until resume")
    sub.add_parser("resume", help="lift the kill switch")
    return p


COMMANDS = {"backtest": cmd_backtest, "run": cmd_run, "status": cmd_status,
            "flatten": cmd_flatten, "stop": cmd_stop, "resume": cmd_resume}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    cfg = load_config(args.config)
    setup_logging(cfg.log_dir, args.verbose)
    try:
        return COMMANDS[args.command](cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
