"""CLI аналитического ядра: прогон конвейера по MOEX ISS.

Только чтение и расчёт. Заявки не отправляются.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .ingestion.moex import IssClient
from .pipeline import export, run_pipeline

CONFIG_PATH = "config/strategy.json"
REBALANCE_CONFIG_PATH = "config/rebalance.json"
PORTFOLIO_PATH = "data/portfolio.json"


def run_rebalance_command(args) -> int:
    """Ручной запуск ребалансировки: портфель из файла, рынок из MOEX ISS."""
    from portfolio_inputs import load_portfolio

    from .rebalance.config import load_rebalance_config
    from .rebalance.iss import load_market
    from .rebalance.runner import rebalance

    as_of = date.fromisoformat(args.date)
    strategy_cfg = load_config(args.config)
    rcfg = load_rebalance_config(args.rebalance_config)
    client = IssClient(raw_root=args.raw_root)

    snapshot = load_portfolio(Path(args.portfolio))
    universe, _load_errors = load_market(
        as_of, strategy_cfg, client, issuers_path=args.issuers, workers=args.workers
    )
    by_secid = {issue.secid: issue for issue in universe}

    # Позиции сопоставляются по SECID. Выпуск, которого нет на рынке, не
    # теряется и не оценивается выдуманной ценой: он попадает в отдельный
    # список, чтобы инвестор проверил его вручную.
    holdings = []
    unmatched = []
    for position in snapshot["positions"]:
        issue = by_secid.get(position["secid"])
        if issue is None:
            unmatched.append(position["secid"])
            continue
        holdings.append((issue, position["quantity"]))

    report, _plan = rebalance(as_of, snapshot["cash"], holdings, universe, rcfg)

    print(f"Ребалансировка на {as_of.isoformat()}   конфиг: {rcfg.config_id}")
    print(
        f"Остаток: {snapshot['cash']:,.2f} ₽   "
        f"позиций в файле: {len(snapshot['positions'])}   "
        f"выпусков на рынке: {len(universe)}"
    )
    if unmatched:
        print(f"Нет на рынке (проверить вручную): {', '.join(unmatched)}")
    print("-" * 78)

    for position in report.positions:
        print(f"{position.secid:14} {position.status.label:22} {position.issuer_name}")
        for reason in position.reasons[:2]:
            print(f"    {reason}")

    if report.purchases:
        print("-" * 78)
        for purchase in report.purchases:
            print(
                f"КУПИТЬ {purchase['secid']:14} лотов: {purchase['lots']:>3}  "
                f"сумма: {purchase['amount']:>12,.2f} ₽  {purchase['security_name'] or ''}"
            )
            if purchase["reasons"]:
                print(f"    причина: {'; '.join(purchase['reasons'])}")
    else:
        print("Покупки не предложены.")

    if report.stop_reason:
        print(f"Причина: {report.stop_reason}")
    print(f"Остаток после рекомендаций: {report.cash_after:,.2f} ₽")

    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nОтчёт сохранён: {path}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bond-strategy")
    parser.add_argument(
        "command",
        choices=["screen", "rebalance"],
        help=(
            "screen — прогон скринера по MOEX ISS; "
            "rebalance — ручной запуск ребалансировки портфеля"
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="дата расчёта, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=20, help="сколько выпусков разбирать детально")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--raw-root", default="data/raw", help="куда сохранять сырые ответы ISS")
    parser.add_argument("--out", default=None, help="путь для JSON-отчёта")
    parser.add_argument("--portfolio", default=PORTFOLIO_PATH, help="файл портфеля (cash + positions)")
    parser.add_argument("--rebalance-config", default=REBALANCE_CONFIG_PATH)
    parser.add_argument("--issuers", default="config/issuers.json")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    if args.command == "rebalance":
        return run_rebalance_command(args)

    cfg = load_config(args.config)
    as_of = date.fromisoformat(args.date)
    client = IssClient(raw_root=args.raw_root)

    result = run_pipeline(as_of, cfg, client, limit=args.limit)

    print(f"Дата расчёта: {result.as_of}   конфиг: {result.config_id}")
    counts = result.summary.get("counts", {})
    print("  ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"Всего выпусков на борде: {result.summary.get('board_rows')}")
    print(f"Отобрано после отсева:   {result.summary.get('preselected')}")
    print(f"Разобрано детально:      {result.summary.get('evaluated')}")
    print(f"Точек кривой ОФЗ:        {result.summary.get('ofz_points')}")
    print("-" * 78)

    for s in result.signals:
        print(f"{s['secid']:14} {s['signal']:13} score={s['score']:5.1f}  {s['issuer']}")
        for reason in (s["failed"] or [])[:2]:
            print(f"        причина: {reason}")

    if result.errors:
        print(f"\nОшибок загрузки: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"  {err}")

    if args.out:
        path = export(result, args.out)
        print(f"\nОтчёт сохранён: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
