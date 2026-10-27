"""Загрузчик облигаций из MOEX ISS с нормализацией в BondIssue."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from ..models import BondIssue
from .moex import MoexClient
from ..normalization.moex import moex_to_bond_issue


def load_bonds_from_moex(
    as_of: date,
    raw_dir: Path | None = None,
    min_maturity_days: int = 180,
    limit: int | None = None,
) -> list[BondIssue]:
    """Загрузка корпоративных облигаций из MOEX ISS.

    Args:
        as_of: дата снимка
        raw_dir: каталог для сохранения исходных JSON (None = не сохранять)
        min_maturity_days: минимальный остаточный срок
        limit: ограничение числа выпусков (для тестов)

    Returns:
        Список BondIssue, готовых к анализу
    """
    client = MoexClient(raw_dir=raw_dir)
    
    # 1. Список выпусков основного режима
    securities = client.list_bonds_tqcb()
    if limit:
        securities = securities[:limit]
    
    # 2. Для каждого выпуска: description, bondization, marketdata
    issues: list[BondIssue] = []
    for sec in securities:
        secid = sec["SECID"]
        
        try:
            desc = client.get_security_description(secid)
            bondz = client.get_bondization(secid)
            md = client.get_marketdata(secid)
            
            issue = moex_to_bond_issue(sec, desc, bondz, md, as_of)
            if issue is None:
                continue  # валютный, истёкший или некорректный
            
            # Фильтр по остаточному сроку
            if issue.maturity_date:
                days = (issue.maturity_date - as_of).days
                if days < min_maturity_days:
                    continue
            
            issues.append(issue)
            
        except Exception as e:
            # Сбой по одному выпуску не должен ронять весь процесс
            print(f"WARN: {secid} пропущен: {e}")
            continue
    
    # 3. Загрузить ОФЗ и сопоставить по срочности
    ofz_curve = load_ofz_curve(client, as_of)
    for issue in issues:
        issue.ofz_ytm = match_ofz_ytm(issue, ofz_curve)
    
    return issues


def load_ofz_curve(client: MoexClient, as_of: date) -> list[dict]:
    """Загрузка кривой доходности ОФЗ."""
    ofz = client.list_ofz()
    curve = []
    for o in ofz:
        matdate = o.get("MATDATE")
        ytm = o.get("YIELD")
        if not matdate or ytm is None:
            continue
        try:
            mat = date.fromisoformat(matdate)
            days = (mat - as_of).days
            if days > 0:
                curve.append({"matdate": mat, "days": days, "ytm": float(ytm) / 100.0})
        except (ValueError, TypeError):
            continue
    return sorted(curve, key=lambda x: x["days"])


def match_ofz_ytm(issue: BondIssue, curve: list[dict]) -> float | None:
    """Сопоставление корпоративного выпуска с ближайшим ОФЗ по сроку."""
    if not issue.maturity_date or not curve:
        return None
    
    from datetime import date as date_type
    as_of = issue.price_date or date_type.today()
    days = (issue.maturity_date - as_of).days
    
    if days <= 0:
        return None
    
    # Ближайший ОФЗ по сроку
    closest = min(curve, key=lambda x: abs(x["days"] - days))
    ytm = closest.get("ytm")
    return ytm if ytm and ytm > 0 else None
