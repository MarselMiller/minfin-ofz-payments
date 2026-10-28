"""Быстрые тесты разбора таблиц всех трех методов на сохраненных файлах (без обращения к сайту).

Запуск: python agent/test_collect.py
Прогоняется после любой технической починки: разбор должен давать те же строки, что и раньше.
Файлы четырех сообщений лежат в agent/fixtures: страница для HTML, docx для API и журнал строк,
которые прочитал браузер.
"""
import ast
import json
import sys
from pathlib import Path

import docx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect import METHODS, ROOT, code_cells, settings_ns  # noqa: E402

FIXTURES = ROOT / "agent" / "fixtures"

# doc_id: (дата выплаты из заголовка, [(выпуск, тип, сумма), ...], «Итого») - значения из итогового датасета
CASES = {
    "317695": ("2026-10-07", [("26226RMFS", "купон", 14556.237), ("26232RMFS", "купон", 13402.177),
                              ("26245RMFS", "купон", 62117.801), ("29008RMFS", "купон", 16298.271),
                              ("26226RMFS", "погашение", 367210.831)], 473585.317),
    "317739": ("2026-10-14", [("26228RMFS", "купон", 22720.035)], 22720.035),  # раздел «Выплата»
    "305873": ("2024-01-25", [("46023RMFS", "купон", 3601.95),
                              ("46023RMFS", "частичное погашение", 29500.0)], 33101.95),
    "306071": ("2024-01-31", [("26233RMFS", "купон", 13689.0), ("26239RMFS", "купон", 17205.0),
                              ("29006RMFS", "купон", 16598.906), ("29022RMFS", "купон", 37330.0),
                              ("29024RMFS", "купон", 27766.208)], 112589.115),  # «Итого» больше на 0,001
}


def html_page(doc_id):
    return BeautifulSoup((FIXTURES / f"{doc_id}.html").read_text(encoding="utf-8"), "lxml")


def rows_html(doc_id):
    table = next(t for t in html_page(doc_id).find_all("table") if "Итого" in t.get_text())
    return [[td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]


def rows_api(doc_id):
    document = docx.Document(FIXTURES / f"{doc_id}.docx")
    return [[cell.text for cell in row.cells] for table in document.tables for row in table.rows]


def rows_browser(doc_id):
    return json.loads((FIXTURES / f"{doc_id}.json").read_text(encoding="utf-8"))


ROWS = {"html": rows_html, "api": rows_api, "browser": rows_browser}

failed = 0
for method in METHODS:
    ns = settings_ns(method)  # только настройки и функции разбора тетради, сайт не трогаем
    for doc_id, (expected_date, expected_rows, expected_total) in CASES.items():
        records, total = ns["parse_table"](ROWS[method](doc_id))
        got_rows = [(r["issue"], r["payment_type"], r["amount_mln_rub"]) for r in records]
        ok = got_rows == expected_rows and total == expected_total
        if method == "html":  # дату выплаты из заголовка проверяем на странице сообщения
            got_date = ns["payment_date"](html_page(doc_id).select_one("h1").get_text(" ", strip=True))
            ok = ok and got_date.date().isoformat() == expected_date
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {method:8} {doc_id} {expected_date}"
              + ("" if ok else f": {got_rows} {total}"))

# карточка ленты без docx (так было с сообщением за 28.10.2026): метод API не падает, а откладывает сообщение
cell = next(c for c in code_cells(ROOT / "01_api" / "api.ipynb") if "def parse_cards" in c)
ns = settings_ns("api")
exec(compile(ast.Module([n for n in ast.parse(cell).body if isinstance(n, ast.FunctionDef)], []), "<api>", "exec"), ns)
CARD = ('<div class="document_card"><a class="document_title" href="/ru/x/info_payment?id_65={id}-x" '
        'title="Информация о выплатах купонного дохода {day} октября 2026 г.">x</a>{file}'
        '<div class="document_footer"><span class="date">{day}.10.2026</span></div></div>')
listing = BeautifulSoup(CARD.format(id="317861", day=28, file="")
                        + CARD.format(id="317815", day=22, file='<a class="file_item" href="/c/22.docx">docx</a>'), "lxml")
got = [(c["doc_id"], c["docx_url"]) for c in ns["parse_cards"](listing)]
ok = got == [("317861", None), ("317815", "https://minfin.gov.ru/c/22.docx")]
failed += not ok
print(f"{'OK  ' if ok else 'FAIL'} api      карточка без docx" + ("" if ok else f": {got}"))

print("Все тесты прошли" if not failed else f"Провалено тестов: {failed}")
sys.exit(1 if failed else 0)
