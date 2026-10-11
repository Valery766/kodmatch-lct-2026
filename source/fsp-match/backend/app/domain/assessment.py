"""Versioned, parameterized work samples with trusted deterministic checkers.

Shown Python/TS patches are never executed. SQL is read-only in disposable SQLite
with explicit operation, table, function, instruction, time and output limits.
Synthetic forms are correctness-checked, not psychometrically calibrated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import sqlite3
import time
from typing import Any

VERSION = "assessment-1.0.0"
SPECIALIZATIONS = ("backend_python", "frontend_ts", "data_sql")
GRADES = ("junior", "middle")
MAX_ANSWERS_BYTES = 20000
MAX_SQL_BYTES = 4000
SQL_STEP_BUDGET = 30000
SQL_TIMEOUT_SECONDS = 0.10
SQL_MAX_ROWS = 50
SQL_MAX_VALUE_BYTES = 65536
SQL_MAX_RESULT_BYTES = 65536
SQL_MAX_COMPILED_OPS = 10000
SQL_FUNCTIONS = frozenset({"sum", "count", "min", "max", "avg", "coalesce", "nullif", "abs", "round", "lower", "upper", "length"})


def _seed(seed: int | str) -> tuple[str, random.Random]:
    if isinstance(seed, bool) or not isinstance(seed, (int, str)):
        raise ValueError("seed должен быть числом или строкой")
    token = str(seed)
    if len(token) > 200:
        raise ValueError("Слишком длинный seed")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest, random.Random(int(digest, 16))


class _Form:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.questions: list[dict] = []
        self.oracle: dict[str, dict] = {}

    def add(self, kind: str, title: str, prompt: str, competency: str, expected: Any,
            *, code: str | None = None, options: list[tuple[str, str]] | None = None,
            guidance: str = "Проверьте правила задачи и граничные случаи.", **extra: Any) -> None:
        qid = f"q{len(self.questions) + 1}"
        question = {"id": qid, "kind": kind, "title": title, "prompt": prompt,
                    "competency": competency, "max_points": 20, **extra}
        if code is not None:
            question["code"] = code
        if options is not None:
            shuffled = list(options)
            self.rng.shuffle(shuffled)
            ids = {key: f"o{i + 1}" for i, (key, _) in enumerate(shuffled)}
            question["options"] = [{"id": ids[key], "text": text} for key, text in shuffled]
            expected = [ids[k] for k in expected] if kind == "multi_choice" else ids[expected]
        schema = {
            "choice": {"type": "string", "enum": [o["id"] for o in question.get("options", [])]},
            "multi_choice": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "trace": {"type": "array", "description": "JSON-массив результатов в указанном порядке"},
            "sql": {"type": "string", "maxLength": MAX_SQL_BYTES, "description": "Один read-only SELECT"},
        }
        question["input_schema"] = schema[kind]
        self.questions.append(question)
        self.oracle[qid] = {"expected": expected, "guidance": guidance}


def _backend(form: _Form, grade: str, rng: random.Random) -> None:
    if grade == "junior":
        floor = rng.choice([0, 5, 10, 15])
        cases = [[], [None, floor, floor + 7, -3], [floor - 1, floor, None, floor + 2], [0, None, -5]]
        defect = rng.choice([f"x > {floor}", f"x >= {floor}", f"x is not None and x > {floor}"])
        form.add("choice", "Исправить фильтр сумм", f"Суммируйте все числа >= {floor}; None пропускается. Выберите исправление одной строки.", "python", "correct",
                 code=f"def total(values):\n    return sum(x for x in values if {defect})",
                 options=[("correct", f"return sum(x for x in values if x is not None and x >= {floor})"),
                          ("strict", f"return sum(x for x in values if x is not None and x > {floor})"),
                          ("truthy", f"return sum(x for x in values if x and x >= {floor})"),
                          ("none", f"return sum(x for x in values if x >= {floor})")],
                 guidance="Проверьте обработку None и включение границы; truthiness не равна проверке типа.")
        # Avoid semantic duplicate options when zero contributes zero: truthy patch
        # is contrasted with a function returning the count, which does differ.
        form.questions[-1]["options"] = [
            {**o, "text": f"return sum(1 for x in values if x is not None and x >= {floor})"}
            if "if x and" in o["text"] else o for o in form.questions[-1]["options"]]
        expected = [sum(x for x in c if x is not None and x >= floor) for c in cases]
        form.add("trace", "Проверить результат патча", f"Для корректного total верните JSON-массив сумм этих входов по порядку: {json.dumps(cases)}", "python", expected,
                 guidance="Каждый вход проверяется отдельно; пустой вход дает сумму 0.")
        form.add("multi_choice", "Граничные проверки", "Какие проверки непосредственно покрывают указанные правила функции? Выберите все подходящие.", "testing", ["none", "boundary", "empty"],
                 options=[("none", "Список с None и обычным числом"), ("boundary", f"Число ровно {floor} и число меньше границы"),
                          ("empty", "Пустой список"), ("network", "Потеря соединения с HTTP-сервером")])
        form.add("choice", "Не менять входные данные", "Функция должна оставлять переданный список неизменным. Какой вариант соблюдает это правило и суммирует по исходной спецификации?", "testing", "generator",
                 options=[("generator", "Итерировать вход через генератор, не выполняя присваивания/удаления"),
                          ("pop", "Удалять элементы через pop, пока список не пуст"), ("sort", "Сначала сортировать исходный список in-place"),
                          ("clear", "После вычисления выполнить clear входного списка")])
        form.add("choice", "Контракт HTTP-входа", "API ожидает JSON-массив конечных чисел или null. Что делать со строкой вместо массива и значением 'NaN'?", "api", "validate",
                 options=[("validate", "Отклонить невалидную структуру/нечисловое значение контролируемым 4xx до вычисления"),
                          ("zero", "Молча вернуть 0"), ("exec", "Исполнить строку как выражение Python"), ("crash", "Допустить необработанный 500")])
    else:
        balance = rng.randrange(200, 501, 25)
        amount = rng.randrange(25, min(balance // 2, 150), 5)
        key_a, key_b = f"pay-{rng.randrange(1000, 9999)}", f"retry-{rng.randrange(1000, 9999)}"
        form.add("choice", "Идемпотентный платеж", "Для одного ключа повтор того же payload возвращает прежний результат без списания; другой payload – conflict. Неуспешное списание не резервирует ключ. Выберите атомарную схему.", "api", "transaction",
                 code="def charge(key, amount):\n    balance -= amount\n    cache[key] = amount\n    return 'ok'",
                 options=[("transaction", "В транзакции проверить key+payload, баланс, списать и записать результат; уникальность ключа и блокировка баланса"),
                          ("precheck", "Проверить cache вне транзакции, потом независимо списать и записать"),
                          ("cachefirst", "Записать ключ до проверки баланса, затем пытаться списать"),
                          ("ignorepayload", "Для существующего key всегда вернуть ok, payload не сравнивать")],
                 guidance="Повторная доставка требует проверки payload и общей транзакционной границы.")
        events = [[key_a, amount], [key_a, amount], [key_a, amount + 1], [key_b, balance + 1], [key_b, amount]]
        statuses = ["ok", "replayed", "conflict", "insufficient", "ok"]
        if rng.choice([False, True]):
            # Same competences and number of checks, different event order:
            # failed first key, successful retry, replay and payload conflict.
            events = [[key_b, balance + 1], [key_b, amount], [key_a, amount], [key_a, amount], [key_b, amount + 1]]
            statuses = ["insufficient", "ok", "ok", "replayed", "conflict"]
        form.add("trace", "Трасса повторных запросов", f"Начальный баланс {balance}. Последовательные [key,amount]: {json.dumps(events)}. Верните [status1,...,status5,final_balance], статусы ok/replayed/conflict/insufficient. Failed attempt не сохраняет key.", "api", statuses + [balance - 2 * amount],
                 guidance="Различайте повтор успешного результата, конфликт payload и повтор после неуспеха.")
        mutable_code = "def collect(value, buffer=[]):\n    buffer.append(value)\n    return buffer"
        form.add("multi_choice", "Mutable default в Python", "Два вызова collect без явного buffer. Какие утверждения верны для приведенного кода?", "python", ["shared", "definition", "none"],
                 code=mutable_code,
                 options=[("shared", "Два вызова используют один и тот же изменяемый список по умолчанию"),
                          ("definition", "Объект аргумента по умолчанию создается при определении функции"),
                          ("none", "buffer=None с созданием нового списка внутри устраняет общий default"),
                          ("fresh", "Список buffer автоматически пересоздается при каждом вызове")])
        values = rng.sample(range(10, 90), 3)
        form.add("trace", "Трасса изменяемого аргумента", f"После свежего определения collect вызовы: collect({values[0]}), collect({values[1]}), collect({values[2]}, []). Каждый результат сразу сериализуется в JSON до следующего вызова. Верните JSON-массив трех снимков списков.", "python", [[values[0]], values[:2], [values[2]]],
                 code=mutable_code, guidance="Различайте общий default и явно переданный новый список; снимок фиксируется до следующего вызова.")
        form.add("choice", "Проверка мутацией", "Какой тест выявляет ошибку 'проверка ключа вне транзакции' лучше обычного happy-path?", "testing", "parallel",
                 options=[("parallel", "Барьер двух конкурентных обработчиков: один ключ/payload, проверить одно списание и один результат"),
                          ("single", "Один успешный платеж"), ("name", "Проверка длины названия компании"), ("css", "Проверка цвета кнопки оплаты")])


def _frontend(form: _Form, grade: str, rng: random.Random) -> None:
    if grade == "junior":
        initial = rng.randrange(1, 20)
        increments = [rng.randrange(1, 6), rng.randrange(1, 6), rng.randrange(1, 6)]
        form.add("choice", "Обновление состояния React", "Три обновления счетчика выполняются в одном handler и batch. Какой патч сохраняет все приращения?", "typescript", "functional",
                 code="increments.forEach(delta => setCount(count + delta));",
                 options=[("functional", "increments.forEach(delta => setCount(prev => prev + delta));"),
                          ("snapshot", "increments.forEach(delta => setCount(count + delta));"),
                          ("mutate", "increments.forEach(delta => { count += delta; });"),
                          ("reset", "increments.forEach(delta => setCount(delta));")],
                 guidance="Учитывайте snapshot состояния внутри handler и functional updates.")
        form.add("trace", "Batch и замыкание", f"Начальный count={initial}, increments={increments}. Верните [итог исходного кода, итог functional-патча].", "typescript", [initial + increments[-1], initial + sum(increments)])
        text = rng.choice(["код", "тест", "профиль"])
        form.add("choice", "Безопасный текст в DOM", f"Имя кандидата приходит как '{text}<img src=x onerror=alert(1)>'. Как отобразить его в React?", "javascript", "text",
                 options=[("text", "Передать как текстовое значение JSX: <span>{name}</span>"),
                          ("html", "Использовать dangerouslySetInnerHTML без очистки"),
                          ("eval", "Передать name в eval"), ("template", "Склеить HTML-строку и вставить через innerHTML")])
        form.add("multi_choice", "Валидация формы", "Поле salary_min приходит строкой. Какие проверки нужны до отправки валидной RUB-вилки?", "javascript", ["finite", "order", "presence"],
                 options=[("finite", "Преобразовать и проверить конечные неотрицательные числа"),
                          ("order", "Проверить salary_min <= salary_max"), ("presence", "Проверить наличие обеих границ"),
                          ("trust", "Проверять только в браузере; сервер обязан доверять значениям")])
        form.add("choice", "Тест состояния формы", "Какая проверка обнаружит потерю части functional updates?", "testing", "sum",
                 options=[("sum", "Несколько приращений в одном event, ожидать начальное значение + сумму всех приращений"),
                          ("once", "Только один клик с +1"), ("font", "Только наличие выбранного шрифта"), ("title", "Только заголовок вкладки")])
    else:
        query_a, query_b = rng.sample(["python", "sql", "react", "api", "data"], 2)
        lag = rng.randrange(40, 100)
        form.add("choice", "Устаревший ответ поиска", "A-запрос отправлен раньше B, но ответ A пришел после B. Старый ответ не должен перезаписать актуальный. Выберите патч с проверкой актуальности результата и unmount.", "async", "generation",
                 code="async function search(q) {\n  const rows = await fetchRows(q);\n  setRows(rows);\n}",
                 options=[("generation", "Увеличить generation на запрос/cleanup; применять ответ только если его generation текущая и компонент активен"),
                          ("lastresponse", "Применять каждый ответ, последний по времени завершения правильный"),
                          ("delay", "Добавить фиксированный setTimeout перед setRows"),
                          ("samequery", "Проверять только длину поискового текста")],
                 guidance="Порядок завершения не равен порядку актуальных запросов; cleanup тоже инвалидирует generation.")
        events = [f"send A({query_a}) generation=1", f"send B({query_b}) generation=2", f"resolve B at {lag}ms", f"resolve A at {lag + 50}ms", "cleanup generation=3", "late resolve B"]
        trace = ["B", "B", "B"]
        if rng.choice([False, True]):
            events = [f"send A({query_a}) generation=1", f"send B({query_b}) generation=2", "cleanup generation=3", f"resolve B at {lag}ms", f"resolve A at {lag + 50}ms", "late resolve B"]
            trace = ["empty", "empty", "empty"]
        form.add("trace", "Трасса конкурентных ответов", f"Начальные rows='empty'. С generation-патчем события: {json.dumps(events)}. Верните rows после resolve B, resolve A, late resolve B как JSON-массив строк (empty/A/B); cleanup оставляет последнее rows, но блокирует будущие обновления.", "async", trace)
        form.add("multi_choice", "Доверенная identity", "Mini App получает initDataUnsafe.user и raw initData. Что требуется перед доступом к приватному профилю?", "typescript", ["server", "age", "ownership"],
                 options=[("server", "Проверить подпись raw initData на сервере по правилам платформы"),
                          ("age", "Проверить freshness/auth_date и защиту привязки от replay"),
                          ("ownership", "Применить серверные role/ownership проверки к данным"),
                          ("unsafe", "Авторизовать по user.id из initDataUnsafe, подпись необязательна")])
        form.add("choice", "Тип ошибки сети", "Как обработать 403 чужого профиля, timeout и успешный пустой список без смешения состояний?", "typescript", "states",
                 options=[("states", "Явные состояния forbidden/error/empty/success; отсутствие данных не маскировать под успешную авторизацию"),
                          ("allzero", "Во всех случаях показывать empty и считать запрос успешным"),
                          ("public", "При 403 повторить запрос без токена"), ("contact", "При timeout показать контакты из кеша другой компании")])
        form.add("choice", "Тест гонки", "Какой детерминированный тест проверяет защиту от stale response и unmount?", "testing", "controlled",
                 options=[("controlled", "Управляемые promises: resolve B, затем A, затем cleanup/late response; проверить отсутствие устаревшего обновления"),
                          ("realnetwork", "Один запрос к реальному серверу без управления временем"),
                          ("sleep", "Надеяться на случайный порядок ответов после sleep"), ("css", "Сравнить только цвет loading spinner")])


def _sql_fixture(grade: str, rng: random.Random) -> dict:
    base = rng.randrange(10, 101, 5)
    if grade == "junior":
        rows = [[1, "A", base, "paid"], [2, "A", base, "paid"], [3, "A", base * 9, "failed"],
                [4, "B", base * 3, "paid"], [5, "C", 0, "paid"], [6, "B", None, "paid"], [7, "D", base * 7, "pending"]]
        return {"schema": "CREATE TABLE sales(id INTEGER PRIMARY KEY, customer TEXT NOT NULL, amount INTEGER, status TEXT NOT NULL)",
                "tables": {"sales": {"columns": ["id", "customer", "amount", "status"], "rows": rows}}, "threshold": base * 2}
    orders = [[1, "A", base], [2, "A", base], [3, "B", base * 3], [4, "C", base * 8], [5, "B", 0]]
    items = [[1, 1], [2, 1], [3, 2], [4, 3], [5, 3], [6, 3], [7, 5]]
    return {"schema": "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer TEXT NOT NULL, amount INTEGER NOT NULL); CREATE TABLE items(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL)",
            "tables": {"orders": {"columns": ["id", "customer", "amount"], "rows": orders}, "items": {"columns": ["id", "order_id"], "rows": items}}}


def _data(form: _Form, grade: str, rng: random.Random) -> None:
    fixtures = [_sql_fixture(grade, rng) for _ in range(3)]
    public = fixtures[0]
    if grade == "junior":
        threshold = public["threshold"]
        # The business threshold is constant across hidden fixtures.
        for fixture in fixtures:
            fixture["threshold"] = threshold
        fixtures[1]["tables"]["sales"]["rows"].extend([[8, "E", None, "paid"], [9, "E", None, "paid"]])
        fixtures[2]["tables"]["sales"]["rows"].extend([[8, "F", threshold - 1, "paid"], [9, "F", 2, "paid"], [10, "F", threshold * 10, "failed"]])
        reference = f"SELECT customer, SUM(amount) FROM sales WHERE status = 'paid' GROUP BY customer HAVING SUM(amount) >= {threshold}"
        prompt = f"Напишите один SELECT: по customer суммировать только paid, NULL amount пропускать как SQL SUM. Оставить итог >= {threshold}. Две колонки: customer, сумма. Порядок строк не важен. Проверяется и на закрытых данных той же схемы."
        form.add("sql", "Практическая SQL-агрегация", prompt, "sql", reference, schema=public["schema"], dataset=public["tables"],
                 guidance="Разделите фильтрацию строк и итоговых групп; проверьте duplicate amounts и NULL.")
        form.oracle["q1"]["fixtures"] = fixtures
        base = public["tables"]["sales"]["rows"][0][2]
        form.add("trace", "NULL и COUNT", "По всей таблице sales верните [COUNT(*), COUNT(amount), SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)].", "sql", [7, 6, base * 5])
        form.add("choice", "WHERE против HAVING", "Нужны группы с общей paid-суммой выше порога, даже когда отдельные строки ниже порога. Где ставить условие суммы?", "data_modeling", "having",
                 options=[("having", "HAVING SUM(amount) >= threshold после GROUP BY; status проверять WHERE"),
                          ("whereamount", "WHERE amount >= threshold до GROUP BY"),
                          ("distinct", "Заменить SUM(amount) на SUM(DISTINCT amount)"),
                          ("limit", "Использовать LIMIT threshold вместо условия суммы")])
        form.add("multi_choice", "Семантика пропущенных значений", "Выберите верные свойства SQLite/SQL для этого набора.", "data_modeling", ["count", "sum", "zero"],
                 options=[("count", "COUNT(amount) не считает NULL"), ("sum", "SUM(amount) пропускает NULL"),
                          ("zero", "Нулевая сумма и отсутствие строк – разные случаи"), ("equals", "NULL = NULL возвращает true в обычном SQL сравнении")])
        form.add("choice", "Набор, обнаруживающий ошибку", "Какой пример выявит ошибочный SUM(DISTINCT amount)?", "testing", "equal",
                 options=[("equal", "Два разных оплаченных заказа одной компании с одинаковой суммой"),
                          ("one", "Один заказ"), ("empty", "Только пустая таблица"), ("unique", "Все суммы различны")])
    else:
        fixtures[1]["tables"]["orders"]["rows"].append([6, "A", 7])
        fixtures[1]["tables"]["items"]["rows"].extend([[8, 6], [9, 6]])
        fixtures[2]["tables"]["orders"]["rows"].append([6, "D", 0])
        fixtures[2]["tables"]["items"]["rows"].extend([[8, 6], [9, 1], [10, 2]])
        reference = "SELECT o.customer, COUNT(*), SUM(o.amount) FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id = o.id) GROUP BY o.customer"
        form.add("sql", "JOIN без удвоения", "Напишите один SELECT: по customer посчитать число заказов и сумму orders.amount, только для заказов с хотя бы одним items. Каждый заказ считать один раз, даже если items несколько. Три колонки: customer, число заказов, сумма. Порядок не важен. Закрытые данные той же схемы тоже проверяются.", "sql", reference,
                 schema=public["schema"], dataset=public["tables"], guidance="Единица учета – заказ, а не строка JOIN; одинаковые суммы разных заказов сохраняются.")
        form.oracle["q1"]["fixtures"] = fixtures
        base = public["tables"]["orders"]["rows"][0][2]
        form.add("trace", "Диагностика наивного JOIN", "Для SELECT SUM(o.amount) FROM orders o JOIN items i ON i.order_id=o.id верните [результат наивного JOIN, правильная сумма заказов с items].", "sql", [base * 12, base * 5])
        form.add("choice", "Корректный патч", "Выберите общий патч, устойчивый к нескольким items и одинаковым суммам разных заказов.", "data_modeling", "exists",
                 options=[("exists", "Использовать WHERE EXISTS для отбора заказов и агрегировать orders"),
                          ("distinctamount", "SUM(DISTINCT o.amount) над JOIN"),
                          ("divide", "Сумму JOIN всегда делить на 2"),
                          ("countdistinct", "Исправить только COUNT(DISTINCT o.id), оставить SUM(o.amount) над JOIN")])
        form.add("multi_choice", "Инварианты агрегации", "Какие утверждения верны для спецификации задания?", "data_modeling", ["item", "equal", "zero"],
                 options=[("item", "Добавление еще одного items существующему заказу не меняет итог"),
                          ("equal", "Два разных заказа с одинаковой ценой считаются дважды"),
                          ("zero", "Заказ с нулевой ценой и items входит в COUNT"),
                          ("orphan", "Заказ без items обязательно входит в итог")])
        form.add("choice", "Мутационный тест", "Какой набор одновременно отличит правильный алгоритм от JOIN-дублирования и SUM(DISTINCT amount)?", "testing", "both",
                 options=[("both", "Два заказа одинаковой цены; у первого 2 items, у второго 1 items"),
                          ("single", "Один заказ с одним items"), ("none", "Нет заказов"),
                          ("unique", "Все цены разные и у каждого заказа один items")])


def build_form(specialization: str, grade: str, seed: int | str) -> dict:
    if specialization not in SPECIALIZATIONS or grade not in GRADES:
        raise ValueError("Неизвестная специализация или грейд")
    digest, rng = _seed(seed)
    form = _Form(rng)
    {"backend_python": _backend, "frontend_ts": _frontend, "data_sql": _data}[specialization](form, grade, rng)
    assert len(form.questions) == 5 and sum(q["max_points"] for q in form.questions) == 100
    return {"version": VERSION, "form_id": f"{specialization}-{grade}-{digest[:16]}",
            "specialization": specialization, "grade": grade,
            "blueprint": {"questions": 5, "max_score": 100, "estimated_minutes": 25,
                          "coverage": list(dict.fromkeys(q["competency"] for q in form.questions)),
                          "validation_status": "author_reviewed_synthetic_not_human_calibrated",
                          "formats": list(dict.fromkeys(q["kind"] for q in form.questions)),
                          "limitations": "Ограниченная проверка навыков; Python/TS – патч и трасса, SQL – ограниченный SELECT. Не сертификат всей квалификации."},
            "rubric": {"pass_score": 65 if grade == "junior" else 75, "minimum_competency_percentage": 50,
                       "choice_points": "correct=20, incorrect=0", "trace_points": "точный результат=20, иначе 0",
                       "multi_choice_points": "20 × max(0,(correct_selected - incorrect_selected)/correct_total)",
                       "sql_points": "20 × доля пройденных независимых fixtures; полное совпадение строк/колонок каждого"},
            "questions": form.questions, "_oracle": form.oracle}


def _public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public_value(child) for key, child in value.items()
                if isinstance(key, str) and not key.startswith("_")
                and not any(marker in key.lower() for marker in ("expected", "answer", "oracle", "fixture", "checker", "solution", "reference_sql", "hidden", "private"))}
    if isinstance(value, list):
        return [_public_value(child) for child in value]
    return copy.deepcopy(value)


def public_form(form: dict) -> dict:
    """Explicit projection: adding a new secret field cannot expose it by default."""
    result = {k: copy.deepcopy(form[k]) for k in ("version", "form_id", "specialization", "grade")}
    result["blueprint"] = {k: _public_value(form["blueprint"][k]) for k in (
        "questions", "max_score", "estimated_minutes", "coverage", "validation_status", "formats", "limitations") if k in form["blueprint"]}
    result["rubric"] = {k: _public_value(form["rubric"][k]) for k in (
        "pass_score", "minimum_competency_percentage", "choice_points", "trace_points", "multi_choice_points", "sql_points") if k in form["rubric"]}
    allowed = ("id", "kind", "title", "prompt", "code", "max_points", "competency", "input_schema", "schema", "dataset")
    result["questions"] = []
    for q in form["questions"]:
        public = {k: _public_value(q[k]) for k in allowed if k in q}
        if "options" in q:
            public["options"] = [{"id": o["id"], "text": o["text"]} for o in q["options"]]
        result["questions"].append(public)
    return result


def _rows(query: str, fixture: dict) -> list[list]:
    if not isinstance(query, str) or not query.strip() or len(query.encode("utf-8")) > MAX_SQL_BYTES:
        raise ValueError("Допустим только ограниченный SELECT")
    connection = sqlite3.connect(":memory:", timeout=0.05)
    try:
        # VDBE progress hooks run between instructions: one concatenation can
        # allocate exponentially larger CTE values before the next hook. Bound
        # SQLite's intermediate values as well as its parser before any SQL.
        for category, limit in (
            (sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES),
            (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES),
            (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50),
            (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 10),
            (sqlite3.SQLITE_LIMIT_COLUMN, 100),
            (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100),
            (sqlite3.SQLITE_LIMIT_VDBE_OP, SQL_MAX_COMPILED_OPS),
        ):
            connection.setlimit(category, limit)
        connection.executescript(fixture["schema"])
        for name, table in fixture["tables"].items():
            columns = ",".join(table["columns"])
            placeholders = ",".join("?" for _ in table["columns"])
            connection.executemany(f"INSERT INTO {name} ({columns}) VALUES ({placeholders})", table["rows"])
        tables = frozenset(fixture["tables"])

        def authorize(action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None) -> int:
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and database == "main" and arg1 in tables:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in SQL_FUNCTIONS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        started = time.monotonic()
        ticks = 0

        def progress() -> int:
            nonlocal ticks
            ticks += 100
            return int(ticks > SQL_STEP_BUDGET or time.monotonic() - started > SQL_TIMEOUT_SECONDS)

        connection.set_progress_handler(progress, 100)
        cursor = connection.execute(query)
        rows, result_bytes = [], 0
        for row in cursor:
            if len(rows) >= SQL_MAX_ROWS:
                raise ValueError("Слишком много строк результата")
            # Do not fetch an entire batch of wide repeated CTE values. The
            # aggregate budget bounds retained Python objects, not only cells.
            value = list(row)
            if any(not isinstance(cell, (str, int, float, type(None))) for cell in value):
                raise ValueError("SQL результат должен содержать JSON-совместимые значения")
            result_bytes += len(_canonical(value).encode("utf-8"))
            if result_bytes > SQL_MAX_RESULT_BYTES:
                raise ValueError("Слишком большой результат SQL")
            rows.append(value)
        return rows
    except (sqlite3.Error, OverflowError, TypeError, MemoryError) as exc:
        raise ValueError("Запрос не прошел безопасную SQL-проверку") from exc
    finally:
        connection.close()


def _canonical(value: Any) -> str:
    # Exact JSON equality retains array order and distinguishes booleans/numbers.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _sql_equivalent(actual: list[list], expected: list[list]) -> bool:
    # SQL result column order matters; row order does not. Integers/floats with
    # equal value compare equal through Python tuple equality, but NULL remains.
    return sorted(actual, key=_canonical) == sorted(expected, key=_canonical)


def grade_form(form: dict, answers: dict) -> dict:
    if not isinstance(answers, dict) or any(not isinstance(k, str) for k in answers):
        raise ValueError("Ответы должны быть JSON-объектом question_id → answer")
    try:
        encoded = _canonical(answers)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Некорректные JSON-ответы") from exc
    if len(encoded.encode("utf-8")) > MAX_ANSWERS_BYTES:
        raise ValueError("Слишком большой ответ")
    known = {q["id"] for q in form["questions"]}
    if set(answers) - known:
        raise ValueError("Неизвестный question_id")
    competencies: dict[str, dict] = {}
    evidence, feedback = [], []
    for q in form["questions"]:
        answer, oracle = answers.get(q["id"]), form["_oracle"][q["id"]]
        expected = oracle["expected"]
        ratio = 0.0
        if q["kind"] == "choice":
            ratio = float(isinstance(answer, str) and answer == expected)
        elif q["kind"] == "trace":
            ratio = float(isinstance(answer, list) and _canonical(answer) == _canonical(expected))
        elif q["kind"] == "multi_choice":
            if isinstance(answer, list) and len(answer) <= 20 and all(isinstance(x, str) for x in answer) and len(set(answer)) == len(answer):
                available = {o["id"] for o in q["options"]}
                if set(answer) <= available:
                    correct = set(expected)
                    ratio = max(0.0, (len(set(answer) & correct) - len(set(answer) - correct)) / len(correct))
        elif q["kind"] == "sql" and isinstance(answer, str):
            successes = 0
            for fixture in oracle["fixtures"]:
                try:
                    successes += int(_sql_equivalent(_rows(answer, fixture), _rows(expected, fixture)))
                except ValueError:
                    pass
            ratio = successes / len(oracle["fixtures"])
        points = round(q["max_points"] * ratio, 2)
        competence = competencies.setdefault(q["competency"], {"score": 0, "max_score": 0})
        competence["score"] = round(competence["score"] + points, 2)
        competence["max_score"] += q["max_points"]
        evidence.append({"question_id": q["id"], "kind": q["kind"], "competency": q["competency"],
                         "score": points, "max_score": q["max_points"],
                         "status": "correct" if ratio == 1 else "partial" if ratio > 0 else "not_confirmed"})
        feedback.append({"question_id": q["id"], "message": "Проверка пройдена." if ratio == 1 else oracle["guidance"]})
    for competence in competencies.values():
        competence["percentage"] = round(100 * competence["score"] / competence["max_score"], 2)
        competence["verified"] = competence["percentage"] >= form["rubric"]["minimum_competency_percentage"]
    score = round(sum(item["score"] for item in evidence), 2)
    threshold = form["rubric"]["pass_score"]
    return {"version": VERSION, "form_id": form["form_id"], "specialization": form["specialization"], "grade": form["grade"],
            "score": score, "max_score": 100, "passed": score >= threshold and all(c["verified"] for c in competencies.values()),
            "threshold": threshold, "competencies": competencies, "evidence": evidence, "feedback": feedback}
