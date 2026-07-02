from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

from src.kb.source_extractors import (
    SpreadsheetRow,
    build_excel_answer_records,
    clean_bot_text,
    extract_dates,
    extract_event_registry,
    extract_intent_examples,
    extract_phones,
    parse_docx_intent_blocks,
    read_xlsx_sheets,
)


def test_clean_bot_text_preserves_content_and_removes_markup() -> None:
    assert clean_bot_text("<b>Привет</b><br><br>Ссылка: https://myrosmol.ru") == (
        "Привет\nСсылка: https://myrosmol.ru"
    )


def test_clean_bot_text_strips_export_quote_artifact() -> None:
    assert clean_bot_text("Обращайтесь!😊'") == "Обращайтесь!😊"


def test_clean_bot_text_renders_known_random_template_deterministically() -> None:
    assert clean_bot_text("{{ ['Всегда рад!', 'Рад быть полезным!']|random}}") == (
        "Всегда рад!"
    )


def test_clean_bot_text_fixes_known_source_artifacts() -> None:
    assert clean_bot_text(
        "❗️Обрати внимание:сейчас регистрация форум «Амур» закрыта.<br>"
        "Актуалльные даты будут известны поздее.<br>"
        "Паспорт иличные документы.<br>"
        "⚡️Важный момеент: сделать это возможно."
    ) == (
        "❗️Обрати внимание: сейчас регистрация на форум «Амур» закрыта.\n"
        "Актуальные даты будут известны позднее.\n"
        "Паспорт и личные документы.\n"
        "⚡️Важный момент: сделать это возможно."
    )


def test_read_xlsx_sheets_reads_inline_strings(tmp_path: Path) -> None:
    xlsx = tmp_path / "source.xlsx"
    _write_minimal_xlsx(xlsx)

    sheets = read_xlsx_sheets(xlsx)

    assert list(sheets) == ["category", "Интенты", "Словарь_событий"]
    assert sheets["category"][0].cell(0) == "Территория смыслов"
    assert sheets["category"][1].cell(1) == "Письмо-вызов"


def test_extract_intent_examples_carries_current_intent() -> None:
    rows = [
        SpreadsheetRow("Интенты", 1, ("№", "Название интента", "Язык", "Пример текста")),
        SpreadsheetRow("Интенты", 2, ("1", "Оплата проезда", "ru", "кто платит за дорогу")),
        SpreadsheetRow("Интенты", 3, ("", "", "", "оплатят ли билет")),
    ]

    assert extract_intent_examples(rows) == {
        "Оплата проезда": ["кто платит за дорогу", "оплатят ли билет"]
    }


def test_build_excel_records_carries_category_and_attaches_examples() -> None:
    rows = [
        SpreadsheetRow(
            "category",
            7,
            ("Территория смыслов", "Оплата проезда", "Проезд до Москвы оплачивается участником."),
        ),
        SpreadsheetRow("category", 8, ("", "Письмо-вызов", "Письмо можно запросить в регионе.")),
    ]
    registry = [{"name": "Территория смыслов", "normalized": "Территория смыслов", "aliases": []}]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category=None,
        source_file="Новый бот Росмол .xlsx",
        intent_examples={"Оплата проезда": ["кто платит за дорогу"]},
        registry=registry,
        extraction_date=date(2026, 6, 11),
    )

    assert len(records) == 2
    assert records[0]["forum_normalized"] == "Территория смыслов"
    assert records[1]["source_category"] == "Территория смыслов"
    assert records[0]["intent_examples"] == ["кто платит за дорогу"]
    assert records[0]["chunk_id"].startswith("xlsx_category_r0007_")


def test_fallback_excel_records_do_not_inherit_forum_metadata() -> None:
    rows = [
        SpreadsheetRow(
            "FALLBACK",
            21,
            (
                "Машук",
                "Возможности бота / abilities",
                "Я отвечаю только по базе знаний Росмолодёжи и могу подсказать по форумам.",
            ),
        )
    ]
    registry = [{"name": "Машук", "normalized": "Машук", "aliases": ["Машук"]}]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category="fallback",
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=registry,
        extraction_date=date(2026, 6, 11),
    )

    assert records[0]["category"] == "общее"
    assert records[0]["source_category"] == "Машук"
    assert records[0]["forum_normalized"] is None


def test_excel_records_trim_noisy_profile_id_fallback_answer() -> None:
    rows = [
        SpreadsheetRow(
            "FALLBACK",
            8,
            (
                "",
                "Где найти ID профиля?",
                (
                    "Чтобы скопировать ID профиля, нажмите на кнопку ID — она находится "
                    "рядом с аватаром в вашем личном кабинете myrosmol.ru/profile.\n"
                    "Ошибка входа. Данный аккаунт привязан к кабинету другого пользователя.\n"
                    "Выйдите из аккаунта."
                ),
            ),
        )
    ]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category="fallback",
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=[],
        extraction_date=date(2026, 6, 27),
    )

    assert records[0]["text_clean"] == (
        "Чтобы скопировать ID профиля, нажмите на кнопку ID — она находится "
        "рядом с аватаром в вашем личном кабинете myrosmol.ru/profile."
    )
    assert "Ошибка входа" not in records[0]["text_clean"]


def test_excel_records_prefer_source_category_forum_over_grant_alias() -> None:
    rows = [
        SpreadsheetRow(
            "category",
            86,
            (
                "Машук",
                "Росмолодежь.Гранты",
                "Гранты для физических лиц проходят в рамках форума.",
            ),
        )
    ]
    registry = [
        {"name": "Машук", "normalized": "Машук", "aliases": []},
        {
            "name": "Гранты для физических лиц",
            "normalized": "Гранты для физических лиц",
            "aliases": [],
        },
    ]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category=None,
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=registry,
        extraction_date=date(2026, 6, 11),
    )

    assert records[0]["category"] == "гранты"
    assert records[0]["source_category"] == "Машук"
    assert records[0]["forum_normalized"] == "Машук"


def test_excel_records_use_source_category_as_forum_when_registry_lacks_event() -> None:
    rows = [
        SpreadsheetRow(
            "category",
            644,
            (
                "Российский Север",
                "Оплата проезда",
                "Дорога до Москвы оплачивается участником самостоятельно.",
            ),
        )
    ]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category=None,
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=[],
        extraction_date=date(2026, 6, 27),
    )

    assert records[0]["forum_normalized"] == "Российский Север"


def test_excel_records_match_partial_source_category_to_registry_event() -> None:
    rows = [
        SpreadsheetRow(
            "category",
            100,
            (
                "Арктика",
                "Оплата проезда",
                "Проезд оплачивается участником самостоятельно.",
            ),
        )
    ]
    registry = [
        {
            "name": "Арктика. Лёд тронулся",
            "normalized": "Арктика. Лёд тронулся",
            "aliases": ["форум Арктика. Лёд тронулся"],
        }
    ]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category=None,
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=registry,
        extraction_date=date(2026, 6, 27),
    )

    assert records[0]["forum_normalized"] == "Арктика. Лёд тронулся"


def test_excel_records_keep_forum_category_when_only_answer_mentions_grants() -> None:
    rows = [
        SpreadsheetRow(
            "category",
            474,
            (
                "Ростов",
                "О мероприятии",
                "Форум помогает участникам запускать проекты и участвовать в грантовом конкурсе.",
            ),
        )
    ]
    registry = [{"name": "Ростов", "normalized": "Ростов", "aliases": []}]

    records = build_excel_answer_records(
        rows=rows,
        sheet_category=None,
        source_file="Новый бот Росмол .xlsx",
        intent_examples={},
        registry=registry,
        extraction_date=date(2026, 6, 11),
    )

    assert records[0]["category"] == "форумы"
    assert records[0]["forum_normalized"] == "Ростов"


def test_parse_docx_intent_blocks_keeps_mult_paragraph_answer(tmp_path: Path) -> None:
    docx = tmp_path / "Форум «Российский Север» интенты.docx"
    _write_minimal_docx(
        docx,
        [
            "Интент: Возрастные ограничения",
            "Текст бота: Мы ждём участников от 14 до 35 лет.",
            "Проверяй условия перед подачей заявки.",
            "Интент: Письмо-вызов",
            "Текст бота:",
            "Письмо-вызов направят организаторы.",
        ],
    )

    blocks = parse_docx_intent_blocks(docx)

    assert [block.intent for block in blocks] == ["Возрастные ограничения", "Письмо-вызов"]
    assert blocks[0].answer == (
        "Мы ждём участников от 14 до 35 лет.\nПроверяй условия перед подачей заявки."
    )
    assert blocks[1].paragraph_start == 5
    assert blocks[1].paragraph_end == 6


def test_extract_event_registry_splits_aliases() -> None:
    rows = [
        SpreadsheetRow("Словарь_событий", 1, ("entity", "synonyms")),
        SpreadsheetRow("Словарь_событий", 2, ("Машук", "Машук, машуке")),
    ]

    assert extract_event_registry(rows) == [
        {"name": "Машук", "normalized": "Машук", "aliases": ["Машук", "машуке"]}
    ]


def test_extract_dates_ignores_relative_day_counts() -> None:
    text = "Письмо придёт за 14 дней до форума, результаты опубликуют 5 июня."

    assert extract_dates(text) == ["5 июня"]


def test_extract_phones_ignores_extension_tail() -> None:
    assert extract_phones("Телефон: 8(495)123-33-44 (доб.1)") == ["8(495)123-33-44"]


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _write_minimal_xlsx(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                "<sheets>"
                '<sheet name="category" sheetId="1" r:id="rId1"/>'
                '<sheet name="Интенты" sheetId="2" r:id="rId2"/>'
                '<sheet name="Словарь_событий" sheetId="3" r:id="rId3"/>'
                "</sheets>"
                "</workbook>"
            ),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
                '<Relationship Id="rId3" Target="worksheets/sheet3.xml"/>'
                "</Relationships>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _sheet_xml(
                [
                    ["Территория смыслов", "Оплата проезда", "Проезд<br>оплачивается"],
                    ["", "Письмо-вызов", "Письмо можно запросить"],
                ]
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            _sheet_xml([["№", "Название интента", "Язык", "Пример текста"]]),
        )
        archive.writestr(
            "xl/worksheets/sheet3.xml",
            _sheet_xml([["entity", "synonyms"], ["Машук", "Машук, машуке"]]),
        )


def _sheet_xml(rows: list[list[str]]) -> str:
    row_xml = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(row, start=1):
            column = chr(ord("A") + column_number - 1)
            cells.append(
                f'<c r="{column}{row_number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )
