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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bond-strategy")
    parser.add_argument(
        "command", choices=["screen"], help="screen — прогон скринера по MOEX ISS"
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="дата расчёта, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=20, help="сколько выпусков разбирать детально")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--raw-root", default="data/raw", help="куда сохранять сырые ответы ISS")
    parser.add_argument("--out", default=None, help="путь для JSON-отчёта")
    args = parser.parse_args(argv)

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
