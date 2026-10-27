"""Конвейер: загрузка MOEX ISS -> нормализация -> снимок -> скринер.

Снимок пишется в data/snapshots/<дата>/ и содержит как результат, так и
перечень пробелов в данных. Скринер не должен получать «тихо подставленные»
значения — только реальные или отсутствующие.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .analytics.ofz_curve import benchmark_for, build_curve
from .ingestion.http_client import FetchError, IssClient
from .ingestion.moex import (
    fetch_board_snapshot,
    fetch_bondization,
    fetch_description,
    fetch_history,
    fetch_ofz_snapshot,
    select_eligible_securities,
)
from .models import BondIssue, DataStatus
from .normalization.enrichment import enrich_all, load_issuer_registry
from .normalization.moex_bonds import NormalizationReport, build_bond_issue

DEFAULT_SNAPSHOT_DIR = Path("data/snapshots")
DEFAULT_REGISTRY = Path("config/issuers.json")


@dataclass
class IngestResult:
    as_of: date
    bonds: list[BondIssue] = field(default_factory=list)
    curve_points: int = 0
    report: NormalizationReport = field(default_factory=NormalizationReport)
    errors: list[str] = field(default_factory=list)
    snapshot_path: Path | None = None
    enrichment: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "bonds_loaded": len(self.bonds),
            "ofz_curve_points": self.curve_points,
            "skipped": len(self.report.skipped),
            "skipped_reasons": _group_reasons(self.report.skipped),
            "floating_coupon": self.report.floating_coupon,
            "fx_issues": self.report.fx_issues,
            "missing_rating": len(self.report.missing_rating),
            "missing_industry": len(self.report.missing_industry),
            "enrichment": self.enrichment,
            "errors": self.errors[:20],
        }


def _group_reasons(skipped: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, reason in skipped:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def save_snapshot(result: IngestResult, directory: str | Path = DEFAULT_SNAPSHOT_DIR) -> Path:
    folder = Path(directory) / result.as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True)

    payload = {
        "summary": result.summary(),
        "bonds": [
            {
                **{
                    k: (v.isoformat() if isinstance(v, date) else v)
                    for k, v in asdict(b).items()
                    if k not in {"status", "amortization"}
                },
                "amortization": [
                    {"date": p.payment_date.isoformat(), "share": p.share_of_nominal}
                    for p in b.amortization
                ],
                "status": {k: v.value for k, v in b.status.items()},
            }
            for b in result.bonds
        ],
    }
    path = folder / "bonds.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    result.snapshot_path = path
    return path


def load_snapshot(path: str | Path) -> IngestResult:
    """Восстановление ранее сохранённого снимка без обращения к сети."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    as_of = date.fromisoformat(data["summary"]["as_of"])
    bonds: list[BondIssue] = []

    for raw in data.get("bonds", []):
        fields = dict(raw)
        fields.pop("amortization", None)
        status = {k: DataStatus(v) for k, v in (fields.pop("status") or {}).items()}
        for key in ("maturity_date", "price_date", "rating_date"):
            if fields.get(key):
                fields[key] = date.fromisoformat(fields[key])
        bond = BondIssue(**fields, status=status)
        bond.amortization = [
            _amort(p) for p in raw.get("amortization", [])
        ]
        bonds.append(bond)

    return IngestResult(as_of=as_of, bonds=bonds, snapshot_path=Path(path))


def _amort(raw: dict[str, Any]):
    from .models import AmortizationPayment

    return AmortizationPayment(date.fromisoformat(raw["date"]), raw["share"])


def ingest_from_moex(
    as_of: date,
    cfg=None,
    client: IssClient | None = None,
    max_candidates: int = 40,
    min_turnover: float = 300_000.0,
    history_days: int = 40,
    save: bool = True,
    registry_file: str | Path = DEFAULT_REGISTRY,
    rating_max_age_days: int = 400,
) -> IngestResult:
    """Полный цикл загрузки. Ошибки отдельных выпусков не останавливают конвейер."""
    client = client or IssClient()
    result = IngestResult(as_of=as_of)

    # 1. Кривая ОФЗ
    curve = []
    try:
        ofz_rows = fetch_ofz_snapshot(client)
        curve = build_curve(ofz_rows, as_of)
        result.curve_points = len(curve)
    except FetchError as exc:
        result.errors.append(f"OFZ: {exc}")

    # 2. Справочник корпоративных выпусков
    try:
        board = fetch_board_snapshot(client)
    except FetchError as exc:
        result.errors.append(f"BOARD: {exc}")
        return result

    marketdata = {r["SECID"]: r for r in board["marketdata"]}
    candidates = select_eligible_securities(
        board["securities"], marketdata, as_of, min_turnover, max_candidates
    )

    # 3. Детализация по каждому кандидату
    for security in candidates:
        secid = security["SECID"]
        try:
            description = fetch_description(client, secid)
            bondization = fetch_bondization(client, secid)
            history = fetch_history(client, secid, as_of, history_days)
        except FetchError as exc:
            result.errors.append(f"{secid}: {exc}")
            result.report.note_skip(secid, "ошибка загрузки")
            continue

        benchmark = benchmark_for(curve, _to_date(description.get("MATDATE")), None, as_of)
        issue = build_bond_issue(
            security=security,
            market=security.get("market", {}),
            description=description,
            bondization=bondization,
            history=history,
            ofz_yield=benchmark,
            settlement=as_of,
            report=result.report,
        )
        if issue is not None:
            result.bonds.append(issue)

    # 4. Кредитное обогащение из отдельного справочника
    registry_path = Path(registry_file)
    if registry_path.exists():
        registry = load_issuer_registry(registry_path)
        result.bonds, result.enrichment = enrich_all(
            result.bonds, registry, as_of, rating_max_age_days
        )
    else:
        result.errors.append(f"REGISTRY: файл {registry_path} не найден")

    if save:
        save_snapshot(result)
    return result


def _to_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
