"""Рейтинги из MOEX ISS: нормализация шкал, отозванные, выбор минимального.

Данные в фикстурах — реальные ответы /iss/cci/ для ecbd_656 (ПАО ДОМ.РФ)
и ecbd_406 (ВЭБ.РФ).
"""
import pytest

from bond_strategy.ingestion.ratings import (
    RATING_SCALE,
    is_credit_rating,
    is_withdrawn,
    normalize_level,
    parse_ratings,
    pick_rating,
)


# ---------- нормализация шкал ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # АКРА
        ("AAA(RU)", "AAA"),
        ("AA(RU)", "AA"),
        ("A(RU)", "A"),
        ("BBB-(RU)", "BBB-"),
        # Эксперт РА
        ("ruAAA", "AAA"),
        ("ruAA+", "AA+"),
        ("ruAA", "AA"),
        ("ruA+", "A+"),
        ("ruBBB-", "BBB-"),
        # НКР
        ("AAA.ru", "AAA"),
        ("BBB.ru", "BBB"),
        # НРА
        ("A+|ru|", "A+"),
        ("BBB|ru|", "BBB"),
        ("BB+|ru|", "BB+"),
        # без префиксов и суффиксов
        ("AAA", "AAA"),
        ("AA+", "AA+"),
    ],
)
def test_normalize_all_agency_formats(raw, expected):
    assert normalize_level(raw) == expected


def test_normalize_rejects_garbage():
    assert normalize_level("") is None
    assert normalize_level(None) is None
    assert normalize_level("мусор") is None
    assert normalize_level("Отозван") is None


def test_scale_is_ordered_best_to_worst():
    assert RATING_SCALE[0] == "AAA"
    assert RATING_SCALE.index("AA") < RATING_SCALE.index("A")
    assert RATING_SCALE.index("BBB") < RATING_SCALE.index("BB")


def test_withdrawn_detected_case_insensitive():
    assert is_withdrawn("Отозван") is True
    assert is_withdrawn("отозван") is True
    assert is_withdrawn("AAA(RU)") is False
    assert is_withdrawn(None) is False


# ---------- отсев не-рейтингов ----------


def test_self_creditworthiness_is_not_a_credit_rating():
    """У ДОМ.РФ рядом с AAA(RU) лежит aa(ru) — оценка собственной
    кредитоспособности. Это не кредитный рейтинг, и брать её нельзя:
    получится занижение на два уровня."""
    assert is_credit_rating(
        "Оценка собственной кредитоспособности национальная шкала (АКРА)"
    ) is False


def test_normal_credit_scales_are_kept():
    assert is_credit_rating(
        "Национальная шкала кредитных рейтингов для Российской Федерации"
    ) is True
    assert is_credit_rating("Национальная российская рейтинговая шкала") is True
    assert is_credit_rating(None) is True


# ---------- разбор ответа ISS ----------


def _company_payload(rows):
    """Формат iss.json=extended: список [charsetinfo, {блок: [...]}, ...]."""
    return [{"charsetinfo": {"name": "utf-8"}}, {"cci_rating_companies": rows}]


def _row(agency, level, book="Национальная шкала кредитных рейтингов для Российской Федерации",
         book_id=1, when="2026-07-03 00:00:00"):
    return {
        "agency_id": 3,
        "agency_name_short_ru": agency,
        "rating_book_id": book_id,
        "rating_book_name_short_ru": book,
        "rating_date": when,
        "rating_level_name_short_ru": level,
    }


DOMRF = "Национальная шкала кредитных рейтингов для Российской Федерации"
SELF = "Оценка собственной кредитоспособности национальная шкала (АКРА)"


def test_parse_skips_self_creditworthiness():
    payload = _company_payload([
        _row("АКРА", "AAA(RU)"),
        _row("АКРА", "aa(ru)", book=SELF, book_id=11),
    ])
    ratings = parse_ratings(payload)
    assert len(ratings) == 1
    assert ratings[0].level == "AAA"
    assert ratings[0].raw == "AAA(RU)"


def test_parse_skips_withdrawn():
    payload = _company_payload([_row("НКР", "Отозван", book_id=29)])
    assert parse_ratings(payload) == []


def test_parse_keeps_two_agencies():
    payload = _company_payload([_row("АКРА", "AAA(RU)"), _row("Эксперт РА", "ruAAA", book_id=18)])
    ratings = parse_ratings(payload)
    assert {r.agency for r in ratings} == {"АКРА", "Эксперт РА"}
    assert all(r.level == "AAA" for r in ratings)


def test_parse_reads_date():
    ratings = parse_ratings(_company_payload([_row("АКРА", "AA(RU)")]))
    assert ratings[0].rating_date.isoformat() == "2026-07-03"


def test_parse_accepts_flat_dict_too():
    """Не всякий эндпоинт отдаёт список — плоский словарь тоже должен читаться."""
    flat = {"cci_rating_companies": [_row("АКРА", "AA(RU)")]}
    assert len(parse_ratings(flat)) == 1


def test_parse_empty_payload():
    assert parse_ratings([{"charsetinfo": {}}, {"cci_rating_companies": []}]) == []


# ---------- правило минимума ----------


def test_picks_minimum_of_two_agencies():
    """АКРА AA, Эксперт РА A+ -> берём A+, доверяем худшей оценке."""
    payload = _company_payload([_row("АКРА", "AA(RU)"), _row("Эксперт РА", "ruA+", book_id=18)])
    best = pick_rating(parse_ratings(payload))
    assert best.level == "A+"
    assert best.agency == "Эксперт РА"


def test_minimum_not_maximum():
    """Обратный случай: максимум был бы AAA, минимум — BBB."""
    payload = _company_payload([_row("АКРА", "AAA(RU)"), _row("НКР", "BBB.ru", book_id=29)])
    assert pick_rating(parse_ratings(payload)).level == "BBB"


def test_equal_levels_pick_newest():
    payload = _company_payload([
        _row("АКРА", "AA(RU)", when="2025-01-01 00:00:00"),
        _row("Эксперт РА", "ruAA", book_id=18, when="2026-08-01 00:00:00"),
    ])
    best = pick_rating(parse_ratings(payload))
    assert best.agency == "Эксперт РА"
    assert best.rating_date.isoformat() == "2026-08-01"


def test_withdrawn_does_not_win_minimum():
    """Отозванный не участвует, даже если выглядел бы худшим."""
    payload = _company_payload([
        _row("АКРА", "AAA(RU)"),
        _row("НКР", "Отозван", book_id=29),
    ])
    assert pick_rating(parse_ratings(payload)).level == "AAA"


def test_empty_ratings_give_none():
    assert pick_rating([]) is None


def test_result_independent_of_input_order():
    a = _row("АКРА", "AA(RU)")
    b = _row("Эксперт РА", "ruA+", book_id=18)
    assert pick_rating(parse_ratings(_company_payload([a, b]))) == pick_rating(
        parse_ratings(_company_payload([b, a]))
    )
