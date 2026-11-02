"""Форма портфеля, общая для скринера и ребалансировки.

Почему отдельный модуль. Портфель — один и тот же объект для обеих страниц:
свободные деньги плюс список позиций, сохранённый в `data/portfolio.json`.
Две копии формы означали бы два источника правды для одного файла, и однажды
они разошлись бы: сохранил в скринере — не видно в ребалансировке.

Форма намеренно требует стоимость позиции вручную. ISS не знает портфель
инвестора, а подставлять номинал вместо рыночной стоимости нельзя: доли в
портфеле были бы выдуманы.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from portfolio_inputs import load_portfolio, save_portfolio, validate_portfolio


def load_snapshot(portfolio_path: Path) -> dict:
    """Текущий портфель из session_state, с загрузкой с диска при первом обращении.

    Ошибка чтения прерывает страницу: молча подставить пустой портфель значило
    бы показать инвестору, что у него ничего нет.
    """
    if "portfolio_snapshot" not in st.session_state:
        try:
            st.session_state["portfolio_snapshot"] = load_portfolio(portfolio_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            st.error(
                f"Не удалось прочитать сохранённый портфель: {exc}. Файл не изменён."
            )
            st.stop()
    return st.session_state["portfolio_snapshot"]


def render_portfolio_form(
    portfolio_path: Path,
    *,
    names: dict[str, str] | None = None,
    form_key: str = "portfolio_form",
) -> dict:
    """Форма ввода портфеля. Возвращает актуальный снимок после возможного сохранения.

    `names` — подписи «SECID — наименование» для уже известных бумаг. Названий в
    форме нет: они приходят с борды и появляются только после прогона, поэтому
    подпись показывается отдельно и только когда что-то уже известно.
    """
    snapshot = load_snapshot(portfolio_path)

    with st.form(form_key):
        saved = st.session_state["portfolio_snapshot"]
        free_cash = st.number_input(
            "Свободные деньги, ₽",
            min_value=0.0,
            value=float(saved["cash"]),
            step=1000.0,
        )
        frame = pd.DataFrame(
            [
                {**p, "remove": False}
                for p in saved["positions"]
            ],
            columns=["remove", "secid", "quantity", "market_value"],
        )
        # Колонка «Продано» вместо надежды на догадку. Редактор таблицы в
        # Streamlit удаляет строку только по Shift+Space и Delete — это нигде не
        # написано и не обнаруживается мышью. Пользователь, продавший бумагу,
        # пробовал стереть ячейки или поставить количество 0, получал ошибку
        # «заполните количество» и не мог сохранить портфель вообще.
        editor = st.data_editor(
            frame,
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                "remove": st.column_config.CheckboxColumn(
                    "Продано", help="Отметьте, чтобы убрать позицию из портфеля", default=False
                ),
                "secid": st.column_config.TextColumn("SECID / ISIN", required=True),
                # min_value=0, а не 1: «продал всё» — законный ответ, и раньше
                # редактор не давал его ввести.
                "quantity": st.column_config.NumberColumn(
                    "Количество, шт.",
                    min_value=0,
                    step=1,
                    required=True,
                    help="0 — позиция продана полностью и будет убрана",
                ),
                "market_value": st.column_config.NumberColumn(
                    "Стоимость позиции с НКД, ₽", min_value=0.01, step=100.0, required=True
                ),
            },
        )
        saved_clicked = st.form_submit_button("Сохранить портфель")

    if saved_clicked:
        try:
            validated = validate_portfolio(free_cash, editor.to_dict("records"))
            save_portfolio(portfolio_path, validated)
            st.session_state["portfolio_snapshot"] = validated
            st.success(
                "Портфель сохранён. Запустите расчёт, чтобы учесть изменения."
            )
            # Убранные позиции называются явно. Молчаливое исчезновение строки
            # неотличимо от ошибки ввода: человек не знает, сохранилось ли
            # намеренное удаление или он что-то стёр случайно.
            after_ids = {p["secid"] for p in validated["positions"]}
            gone = [p for p in saved["positions"] if p["secid"] not in after_ids]
            if gone:
                st.info(
                    "Убраны из портфеля: "
                    + ", ".join(p["secid"] for p in gone)
                    + f". Осталось позиций: {len(validated['positions'])}."
                )
        except (ValueError, OSError) as exc:
            st.error(f"Портфель не сохранён: {exc}")

    snapshot = st.session_state["portfolio_snapshot"]
    if snapshot["positions"] and names:
        st.caption(
            "Наименования позиций по данным борды: "
            + "; ".join(
                f"{p['secid']} — {names.get(p['secid']) or 'не найдено'}"
                for p in snapshot["positions"]
            )
        )
    return snapshot
