"""Агент: выплаты по ОФЗ за любой период тремя методами со сверкой.

Код сбора, разбора и сверки не дублируется: агент выполняет ячейки тетрадей 01_api, 02_html,
03_browser и 04_compare, а вместо ячейки периода (DATE_FROM, DATE_TO) подставляет свои даты.
Починка метода делается в его тетради.

Команды:
  python agent/collect.py count --from 2025-03-01 --to 2025-06-30            # 1 запрос: сколько сообщений и сколько займет сбор
  python agent/collect.py run   --from 2025-03-01 --to 2025-06-30            # три метода и сверка; при совпадении результат в data_files/
  python agent/collect.py run   --from 2025-03-01 --to 2025-06-30 --offline  # пересборка без сайта: API и HTML из кэша, браузер из прошлого прогона

Методы работают по очереди и независимо: если один упал, остальные все равно прогоняются, а в конце
печатается статус каждого. Исключение - ответ сайта 503: тогда на сайт больше никто не идет.

Служебные папки, в git не попадают:
  agent/cache/          скачанное методами API и HTML (лента, docx, страницы сообщений), сохраняется до разбора;
  agent/work/<с>_<по>/  промежуточные CSV методов: после успешной сверки папка удаляется,
                        при проблеме остается, пока проблему не решат.

Коды выхода:
  0 - успех: методы совпали, результат в data_files/ (или в периоде нет выплат);
  1 - техническая ошибка хотя бы в одном методе (сеть, верстка, код, зависимости), смысловых проблем нет: чинить самому;
  2 - смысловая проблема хотя бы в одном методе (незнакомая строка таблицы, не сходится «Итого», повторы,
      нет docx у сообщения в периоде), методы разошлись или сайт отказал в доступе: вынести на решение пользователю;
  3 - в режиме --offline чего-то нет в кэше, других проблем нет: нужны запросы к сайту, то есть оценка и разрешение.
"""
import argparse
import contextlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import traceback
import types
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "agent" / "cache"
WORK = ROOT / "agent" / "work"
DATA = ROOT / "data_files"
COMPARE = ROOT / "04_compare" / "compare.ipynb"
# порядок запуска: HTML и API берут почти все из кэша, медленный браузер идет последним
METHODS = {"html": ("02_html", "html.ipynb", "HTML"),
           "api": ("01_api", "api.ipynb", "скрытое API"),
           "browser": ("03_browser", "browser.ipynb", "браузер")}
SKIP = ("pivot_table", "q_26")  # сводка по годам и демонстрация поиска сайта агенту не нужны
SECONDS_PER_REQUEST = 1.7  # замер полного прогона 02_html: 265 с на 159 запросов
SECONDS_PER_BROWSER_PAGE = 3.5  # замер полного прогона 03_browser: 7-9 мин на 143 страницы
COLUMNS = ["payment_date", "issue", "payment_type", "amount_mln_rub", "coupon_no",
           "coupon_rate_pct", "coupon_per_bond_rub", "doc_id", "published_date", "source_url"]
OK, TECHNICAL, SEMANTIC, NEEDS_SITE = "ок", "техническая ошибка", "смысловая проблема", "нужен сайт"


class Semantic(Exception):
    """Смысловая проблема: решает пользователь."""


class Technical(Exception):
    """Техническая ошибка: агент чинит сам."""


class NeedsSite(Exception):
    """В режиме --offline чего-то нет в кэше: нужен запрос к сайту."""


def code_cells(path):
    return ["".join(c["source"]) for c in json.loads(path.read_text(encoding="utf-8"))["cells"]
            if c["cell_type"] == "code"]


def rub(value):
    """12274293.547 -> «12 274 293,547»."""
    return f"{value:,.3f}".replace(",", " ").replace(".", ",")


def site_date(day):
    return day.strftime("%d.%m.%Y")


def refused(exc):
    """Сайт ответил 503: requests кладет код в response, браузерная тетрадь пишет его в начало сообщения."""
    return getattr(getattr(exc, "response", None), "status_code", None) == 503 or str(exc).startswith("503 ")


def dates(date_from, date_to):
    """Значения для ячейки периода тетрадей: даты выплаты в формате ДД.ММ.ГГГГ."""
    return {"DATE_FROM": site_date(date_from), "DATE_TO": site_date(date_to)}


def settings_ns(method, date_from=None, date_to=None):
    """Период, настройки и функции разбора тетради метода, без обращения к сайту (для count и тестов).

    Без дат берутся те, что вписаны в ячейку периода тетради.
    """
    folder, notebook, _ = METHODS[method]
    cells = code_cells(ROOT / folder / notebook)
    ns = {}
    exec(cells[0], ns)  # ячейка периода
    if date_from:
        ns.update(dates(date_from, date_to))
    with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
        exec(cells[1], ns)  # ячейка настроек создает папку raw/ во временной папке, она не нужна
    exec(next(c for c in cells if "def parse_table" in c), ns)
    return ns


def caching(original, method, offline):
    """Обертка над функцией запроса из тетради (get в HTML, request в API).

    Страницы ленты сохраняет в кэш до разбора, в режиме offline берет только из кэша. Страницы сообщений
    и docx кэширует сама тетрадь (кладет их в RAW и запрашивает, только если их там нет), поэтому обертке
    остается не пускать их на сайт в режиме offline.
    """
    def wrapped(*args, **kwargs):
        params = kwargs.get("params") or {}
        if "P_DATE_from_65" not in params:  # страница сообщения или docx
            if offline:
                raise NeedsSite(args[-1])
            return original(*args, **kwargs)
        path = CACHE / f"listing_{method}" / (f"{params['P_DATE_from_65']}_{params['P_DATE_to_65']}"
                                               f"_p{params.get('page_65', 1)}.html")
        if offline:
            if not path.exists():
                raise NeedsSite(f"лента ({method}) {params}")
            return types.SimpleNamespace(text=path.read_text(encoding="utf-8"), content=path.read_bytes())
        resp = original(*args, **kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)  # сохраняем до разбора: пригодится, если разбор упадет
        return resp
    return wrapped


def seed():
    """Уже скачанное тетрадями 02_html и 01_api кладется в кэш агента и с сайта повторно не запрашивается."""
    for folder, pattern, target in [("02_html", "*.html", CACHE / "html"), ("01_api", "*.docx", CACHE / "api")]:
        target.mkdir(parents=True, exist_ok=True)
        for path in (ROOT / folder / "raw").glob(pattern):
            if not (target / path.name).exists():
                shutil.copy(path, target / path.name)


def execute(name, cells, ns, period, after_settings=None):
    """Выполняет ячейки тетради по порядку.

    Ячейка периода не выполняется: вместо нее подставляются даты агента. После ячейки настроек
    вызывается after_settings, чтобы подменить папку кэша и функцию запросов.
    """
    try:
        for i, source in enumerate(cells):
            if any(marker in source for marker in SKIP):
                continue
            if "DATE_FROM =" in source:
                ns.update(period)
                continue
            if "COLUMNS =" in source and not ns["records"]:  # сообщений нет: таблицу собирать не из чего
                stem = f"{ns['PERIOD'][0]}_{ns['PERIOD'][1]}"
                ns["pd"].DataFrame(columns=COLUMNS).to_csv(f"ofz_payments_{name}_{stem}.csv", index=False)
                return
            exec(compile(source, f"<{name}: ячейка {i + 1}>", "exec"), ns)
            if "PAUSE =" in source and after_settings:
                after_settings(ns)
    except BaseException:
        # браузер тетрадь закрывает сама в последней ячейке; при падении закрываем его здесь, в его же потоке
        if "LOOP" in ns:
            for obj, stop in [("browser", "close"), ("playwright", "stop")]:
                if obj in ns:
                    with contextlib.suppress(Exception):
                        ns["run"](getattr(ns[obj], stop)())
            ns["LOOP"].call_soon_threadsafe(ns["LOOP"].stop)
        raise


def run_method(method, date_from, date_to, work, offline):
    """Один метод за период; CSV метода остается в agent/work/<с>_<по>/<папка метода>/.

    Падение (незнакомая строка таблицы, ошибка кода, сеть) пробрасывается исключением. Если метод
    дошел до конца, смысловые проблемы его проверок возвращаются в problems, а замечания в notes.
    """
    import pandas as pd
    folder, notebook, label = METHODS[method]
    workdir = work / folder
    csv_path = workdir / f"ofz_payments_{method}_{work.name}.csv"
    started = time.time()
    problems, notes = [], []
    if method == "browser" and offline:
        if not csv_path.exists():
            raise NeedsSite("результата браузера за этот период нет: браузер работает только с сайтом")
        print("Взят результат прошлого прогона:", csv_path.relative_to(ROOT))
    else:
        def prepare(ns):
            if method == "html":
                ns.update(RAW=CACHE / "html", get=caching(ns["get"], method, offline))
            elif method == "api":
                ns.update(RAW=CACHE / "api", request=caching(ns["request"], method, offline))
            # браузер пишет журнал прочитанного в raw/ своей рабочей папки

        workdir.mkdir(parents=True, exist_ok=True)
        csv_path.unlink(missing_ok=True)  # результат прошлого прогона не должен попасть в сверку
        ns = {}
        with contextlib.chdir(workdir):
            try:
                execute(method, code_cells(ROOT / folder / notebook), ns, dates(date_from, date_to), prepare)
            except ValueError as exc:
                if "Неожиданная строка таблицы" in str(exc):
                    raise Semantic(str(exc)) from exc
                raise
        if len(ns.get("mismatch", [])):
            problems.append(f"«Итого» не сходится в сообщениях {list(ns['mismatch'].index)}")
        if len(ns.get("duplicates", [])):
            problems.append(f"повторы ключа: {len(ns['duplicates'])} строк")
        no_docx = ns.get("no_docx")  # только у API: сообщения, к которым на сайте нет docx
        for card in (no_docx.itertuples() if no_docx is not None else []):
            day = ns["payment_date"](card.title).date()
            if date_from <= day <= date_to:
                problems.append(f"нет docx у сообщения {card.doc_id} (выплата {day:%d.%m.%Y}): "
                                f"метод не может его проверить")
            else:
                notes.append(f"в ленте есть сообщение {card.doc_id} (выплата {day:%d.%m.%Y}) без docx, "
                             f"в период оно не входит")
    payments = pd.read_csv(csv_path, dtype=str)
    return {"label": label, "status": SEMANTIC if problems else OK, "problems": problems, "notes": notes,
            "messages": payments["doc_id"].nunique(), "rows": len(payments),
            "seconds": "из прошлого прогона" if method == "browser" and offline else round(time.time() - started)}


def compare(work, date_from, date_to):
    """Сверка кодом 04_compare: тетрадь читает CSV методов по путям ../01_api/... и т.д., в work они так и лежат.

    Возвращает итоговую таблицу (ее тетрадь задает только при полном совпадении, иначе None)
    и число расхождений каждого метода с HTML.
    """
    print("\n== сверка ==", flush=True)
    ns = {}
    (work / "04_compare").mkdir(exist_ok=True)
    with contextlib.chdir(work / "04_compare"):
        try:
            execute("compare", code_cells(COMPARE), ns, dates(date_from, date_to))
        except ValueError as exc:
            if "Методы расходятся" not in str(exc):
                raise
    return ns.get("final"), ns["problems"]


def status_lines(results, differences):
    """Статус каждого метода и итог сверки: видно, сколько методов и что именно нужно чинить."""
    lines = ["| Метод | Статус | Сообщений | Строк | Время, с | Подробности |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['label']} | {r['status']} | {r.get('messages', '')} | {r.get('rows', '')} | "
              f"{r.get('seconds', '')} | {'; '.join(r['problems'] + r.get('notes', []))} |" for r in results.values()]
    if differences is None:
        lines += ["", "Сверка не проводилась: не у всех методов есть результат."]
    else:
        # 04_compare складывает строки, которые есть только у одного метода, и различия по каждой колонке
        lines += ["", "Сверка с HTML, число расхождений по подсчету 04_compare (0 = совпал строка в строку): "
                  + ", ".join(f"{METHODS[name][2]} {n}" for name, n in differences.items())]
    return lines


def count(date_from, date_to):
    """Один запрос к ленте: сколько сообщений в периоде и оценка времени сбора по каждому методу."""
    from bs4 import BeautifulSoup
    ns = settings_ns("html", date_from, date_to)
    resp = ns["get"](ns["LIST_URL"], params={"P_DATE_from_65": ns["PUBLISHED_FROM"],
                                              "P_DATE_to_65": ns["PUBLISHED_TO"]})
    found = BeautifulSoup(resp.text, "lxml").select_one(".count_value")
    n_messages = int(found.get_text(strip=True)) if found else 0
    n_pages = max(1, math.ceil(n_messages / 9))
    # HTML и API: лента и каждое сообщение, но то, что уже лежит в кэше, на сайт не запрашивается
    # браузер: лента и каждая страница, кэша у него нет
    requests_max = {"html": n_pages + n_messages, "api": n_pages + n_messages, "browser": n_pages + n_messages}
    minutes_max = {"html": round(requests_max["html"] * SECONDS_PER_REQUEST / 60, 1),
                   "api": round(requests_max["api"] * SECONDS_PER_REQUEST / 60, 1),
                   "browser": round((n_messages * SECONDS_PER_BROWSER_PAGE + n_pages * SECONDS_PER_REQUEST) / 60, 1)}
    minutes_max["total"] = round(sum(minutes_max.values()), 1)
    # в ленте сообщения с запасом по дате публикации, поэтому в период по дате выплаты попадет не больше messages
    print(json.dumps({"published_from": ns["PUBLISHED_FROM"], "published_to": ns["PUBLISHED_TO"],
                      "messages": n_messages, "listing_pages": n_pages,
                      "requests_max": requests_max, "minutes_max": minutes_max}, ensure_ascii=False))


def run(date_from, date_to, offline=False):
    started = time.time()
    stem = f"{date_from.isoformat()}_{date_to.isoformat()}"
    work = WORK / stem
    if not offline:
        shutil.rmtree(work, ignore_errors=True)  # полный прогон начинается с чистой рабочей папки
    seed()
    results = {}
    for method, (_, _, label) in METHODS.items():
        print(f"\n== {label} ==", flush=True)
        try:
            results[method] = run_method(method, date_from, date_to, work, offline)
        except NeedsSite as exc:
            results[method] = {"label": label, "status": NEEDS_SITE, "problems": [str(exc)]}
        except Semantic as exc:
            results[method] = {"label": label, "status": SEMANTIC, "problems": [str(exc)]}
        except Exception as exc:
            if refused(exc):
                raise  # сайт отказал в доступе: остальные методы на сайт уже не идут
            print(traceback.format_exc(), flush=True)
            results[method] = {"label": label, "status": TECHNICAL, "problems": [f"{type(exc).__name__}: {exc}"]}
        print("Статус:", results[method]["status"], *results[method]["problems"], flush=True)
        html = results.get("html")
        if method == "html" and html["status"] == OK and html["rows"] == 0:
            shutil.rmtree(work, ignore_errors=True)
            print(f"\nВыплат с {date_from:%d.%m.%Y} по {date_to:%d.%m.%Y} нет: сверять нечего, файл не записан.")
            return

    final, differences = None, None
    if all((work / folder / f"ofz_payments_{method}_{stem}.csv").exists() for method, (folder, _, _) in METHODS.items()):
        final, differences = compare(work, date_from, date_to)
    statuses = {r["status"] for r in results.values()}

    if statuses == {OK} and final is not None:
        DATA.mkdir(exist_ok=True)
        csv_path, report_path = DATA / f"ofz_payments_{stem}.csv", DATA / f"report_{stem}.md"
        final.to_csv(csv_path, index=False, encoding="utf-8")
        by_type = final.groupby("payment_type")["amount_mln_rub"].agg(["count", "sum"])
        notes = [note for r in results.values() for note in r["notes"]]
        report_path.write_text("\n".join([
            f"# Выплаты по ОФЗ с {date_from:%d.%m.%Y} по {date_to:%d.%m.%Y}", "",
            f"Собрано тремя методами, они совпали строка в строку{' (пересборка без сайта)' if offline else ''}. "
            f"Время работы: {time.time() - started:.0f} с.", "",
            "| Метод | Сообщений | Строк | Время, с |", "|---|---|---|---|",
            *[f"| {r['label']} | {r['messages']} | {r['rows']} | {r['seconds']} |" for r in results.values()], "",
            f"- сообщений: {final['doc_id'].nunique()}, строк: {len(final)}, "
            f"выплаты с {date.fromisoformat(final['payment_date'].min()):%d.%m.%Y} "
            f"по {date.fromisoformat(final['payment_date'].max()):%d.%m.%Y}",
            *[f"- {kind}: строк {int(row['count'])}, сумма {rub(row['sum'])} млн руб."
              for kind, row in by_type.iterrows()],
            "- проверки в каждом методе: «Итого» сходится во всех сообщениях, повторов ключа нет",
            "- сверка методов: строк только в одном методе 0, различий по значениям 0",
            *[f"- примечание: {note}" for note in notes], "",
            f"Файл: data_files/{csv_path.name}",
        ]) + "\n", encoding="utf-8")
        shutil.rmtree(work)  # промежуточные CSV больше не нужны
        print()
        print(report_path.read_text(encoding="utf-8"))
        return

    print("\n== итог ==")
    print("\n".join(status_lines(results, differences)))
    print(f"\nИтоговый файл не записан. Рабочие файлы методов: agent/work/{stem}/")
    if SEMANTIC in statuses or (differences and any(differences.values())):
        raise Semantic("нужно решение пользователя, статусы методов выше")
    if TECHNICAL in statuses:
        raise Technical("метод упал, трассировка выше")
    raise NeedsSite("методу нужен сайт, статусы методов выше")


def main():
    for stream in (sys.stdout, sys.stderr):  # консоль Windows по умолчанию не в UTF-8
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сбор выплат по ОФЗ за период тремя методами со сверкой")
    parser.add_argument("command", choices=["count", "run"])
    parser.add_argument("--from", dest="date_from", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", required=True, type=date.fromisoformat)
    parser.add_argument("--offline", action="store_true", help="только для run: не обращаться к сайту")
    args = parser.parse_args()
    if args.date_from > args.date_to:
        parser.error("--from позже --to")
    try:
        if args.command == "count":
            count(args.date_from, args.date_to)
        else:
            run(args.date_from, args.date_to, offline=args.offline)
    except NeedsSite as exc:
        print(f"НУЖНЫ ЗАПРОСЫ К САЙТУ: {exc}", file=sys.stderr)
        sys.exit(3)
    except Semantic as exc:
        print(f"СМЫСЛОВАЯ ПРОБЛЕМА: {exc}", file=sys.stderr)
        sys.exit(2)
    except Technical as exc:
        print(f"ТЕХНИЧЕСКАЯ ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        if refused(exc):
            print("СМЫСЛОВАЯ ПРОБЛЕМА: сайт ответил 503 «Доступ временно ограничен», "
                  "сбор остановлен целиком", file=sys.stderr)
            sys.exit(2)
        traceback.print_exc()
        if args.command == "count" and "ConnectionError" in [cls.__name__ for cls in type(exc).__mro__]:
            # первый же запрос не дошел до сайта: обычно у среды нет доступа в интернет (песочница чата)
            print("НЕТ ДОСТУПА К САЙТУ: соединение с minfin.gov.ru не устанавливается "
                  "(сеть среды, прокси или DNS). Это не поломка кода", file=sys.stderr)
            sys.exit(1)
        print("ТЕХНИЧЕСКАЯ ОШИБКА: см. трассировку выше", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
