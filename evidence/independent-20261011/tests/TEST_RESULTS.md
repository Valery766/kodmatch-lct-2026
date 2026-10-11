# КОД.МЭТЧ – независимый повтор автоматических проверок (r2)

Дата: 11.10.2026, 14:39–14:52 UTC (17:39–17:52 MSK). Workstream T04/Q01/Q02/P01 финального ревью.
Машина: macOS (Darwin 25.6.0, arm64). Python VENV 3.12.6 (pip 24.2); системный `python3`/`python` = 3.11.12; Node v24.14.0 / npm 11.9.0; Docker и PostgreSQL отсутствуют.
Объект проверки: копия `delivery-20261011/source` → `SCR/tests/src` (SHA256SUMS: 118/118 OK). Оригиналы в `delivery-20261011`, `fsp-match`, `research` не изменялись; секреты не открывались и не выводились; к production выполнялись только публичные GET/HEAD.

Машиночитаемо: [tests.json](tests.json) (43 записи: 34 PASS, 2 FAIL, 7 NOT_RUN). Логи: [logs/](logs/). Хэши локального dist: [frontend-dist-sha256.txt](frontend-dist-sha256.txt).

## 1. Сводка

| Проверка | Результат | Сравнение с прежними доказательствами |
| --- | --- | --- |
| VENV `pip freeze` vs `backend/requirements-dev.lock` | PASS: 56 = 56, расхождений 0 | – |
| Backend pytest | **186 passed, 3 skipped, 0 failed** (189 collected, 1 warning, 22.7 с) | совпадает с source-test-results.json (186+3) |
| Frontend `node --test tests/*.test.mjs` | **12/12 pass** | совпадает (12) |
| `npm ci` / `npm run typecheck` / `npm run build` | PASS / PASS / PASS | – |
| `npm audit --omit=dev` (и полный `npm audit`) | 0 vulnerabilities | – |
| Production JS/CSS vs локальный build | **побайтно равны 2/2**; все 22 файла dist равны 22/22; HTML `/` и `/mini/max` = локальный `dist/index.html` | совпадает с runtime-final.json (те же SHA256) |
| `/api/health`, `/api/ready`, `/api/version` | 200 / 200 / 200; release **`0.1.0-20261011T104017Z`** | совпадает |
| TLS / redirect | TLSv1.3, Let's Encrypt YE1, до **Jan 4 10:04:16 2027 GMT**; http→https **301** | совпадает |
| Offline validation | **78 checks, 0 failed**; 120 форм, 720 оценок | идентично offline-validation-fresh.json (кроме времени/runtime) |
| pip-audit `-r backend/requirements.lock` | No known vulnerabilities (и dev-lock тоже) | – |
| Запуск по README | PASS, кроме 1 реальной находки (worker без схемы) и 1 средовой (Python 3.11) | новые находки, см. §7 |

Расхождений с прежними числами (186 PASS + 3 SKIP; 12; 78 checks; JS/CSS byte-equal) нет.

## 2. Backend

Команда (из `SCR/tests/src/fsp-match`, `FSP_TEST_DATABASE_URL` не задан):
`PYTHONPATH=backend VENV/bin/python -m pytest backend/tests -q -rs --durations=10 -p no:cacheprovider` → exit 0, `186 passed, 3 skipped, 1 warning in 22.71s`. Точная команда README (`.venv/bin/python`, `.venv` → симлинк на VENV) дала то же: 186/3.

SKIP (все три – ожидаемые, PostgreSQL здесь нет, не считаются PASS):
- `test_postgres_integration.py:93` – test_postgres_eight_parallel_starts_keep_single_attempt_and_cooldown
- `test_postgres_integration.py:116` – test_postgres_parallel_submit_retains_categories_and_higher_grade_evidence
- `test_postgres_integration.py:154` – test_postgres_parallel_invite_accept_contact_grant_and_company_privacy
- причина: `Dedicated PostgreSQL FSP_TEST_DATABASE_URL not configured`.

Warning: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` (информационно).
Самые долгие: test_database_pool (2.86 с, только конструирование engine без подключения), slow-drip LLM deadline (1.00 с), PDF export/import (0.44 с).

### Карта покрытия (189 тестов)

| Область | Файлы и тесты | Характер проверок |
| --- | --- | --- |
| Auth, email, сессии, CSRF | test_api.py: registration_confirmation_and_token_replay, confirmation_expiration_and_forgot_non_enumerating, password_reset_revokes_all_sessions, csrf_origin_roles_and_logout, production_cookies_and_closed_registration, invalid_payload_does_not_echo_password, rate_limit_does_not_commit_business_changes, employer_cannot_start_or_read_candidate_test_after_same_browser_role_change; test_email_recovery.py (22); test_email_reply_to.py (10) | HTTP через FastAPI TestClient + реальная SQLite; replay/expiry/enumeration/CSRF/RBAC, header injection, SMTP cooldown/retry |
| Assessment (тесты навыков) | test_domain_assessment.py (18): утечка oracle, мутанты, copied answer, multi-select «всё выбрать», SQL side-effects/ресурсы/CTE-лимиты, reproducible seed; test_api.py: attempt_ownership_oracle_hidden_cooldown_and_voluntary_junior, failure_never_downgrades_existing_grade_and_expiry, parallel_starts_keep_one_attempt_and_cooldown, junior_pass_preserves_higher_grade…, visibility_and_self_report_cannot_change_grade, saved_survey_resumes_only_own…; PG: 2 SKIP | много негативных сценариев (мутанты и обход) |
| Matching | test_domain_matching.py (9): детерминизм, missing≠100, self-report/keyword stuffing не выигрывает, нет контактов в shortlist, salary/format/location/grade, FSP-бонус; test_domain_validation.py (2): 480 меток, знаменатель метрик; test_api.py: bank_filters…pagination, batch_profile_projection…, disabled_candidate_suppressed_in_saved_shortlist… | доменные контракты + API |
| Приглашения / contact grants | test_api.py: contact_privacy_before_accept_other_company_and_revoke, invitation_range_idempotency_owner_and_terminal_status, hidden_profile_not_exposed_through_existing_invitation, invitation_snapshot_survives_need_edit; PG: 1 SKIP | IDOR/чужая компания, отзыв, терминальные статусы |
| Вакансии | test_api.py::test_vacancy_response_grants_only_owner_and_closed_rejected | контакты получает только компания-владелец вакансии, закрытая вакансия → отказ |
| Microtasks | test_api.py::test_microtasks_history_no_grade_mutation_and_oracle_hidden | не меняет грейд, oracle скрыт |
| PDF import/export | test_api.py::test_pdf_export_privacy_and_import_preview, …retired_contact…absent_from_granted_contact_and_pdf; test_profile_fields.py (12) | приватность в PDF, preview без авто-применения, лимиты self-report |
| LLM fallback / privacy | test_grounded_explanations.py (25): галлюцинированные поля/ID/проза → fallback без эха, redirect/gzip/429, timeout, slow-drip deadline, oversized body, кэш не смешивает компании, перепроверка ролей/скрытия/изменения evidence; test_api.py: llm_timeout_and_invalid_structure_fallback, llm_resume_personal_data_gate_and_need_redaction | провайдер подменён через httpx.MockTransport (monkeypatch); реальная LLM не вызывается |
| MAX / мессенджер / бот | test_messengers.py (18): HMAC/signature, возраст initData, identity, pinned host, Retry-After, constant-time secret, нет group linking, транспорт выведенного из продукта стороннего мессенджера недоступен; test_messenger_public_urls.py (12); test_bot_reply_quota.py (10); test_api.py: webhook_authentication_replay_link_identity_and_unlink, signed_launch_requires_link_email…, production_messenger_cookie…, queued_notification_cancelled_after_unlink, contact_in_name_and_max_link_redacted, выведенный сторонний мессенджер ×8 | транспорт MAX замокан, реальный MAX не проверяется |
| Уведомления / outbox / worker | test_api.py: notifications_are_owned_and_delivery_failure_independent, worker_lease_recovery_and_confirmation_smtp; test_email_recovery.py | at-least-once, lease recovery |
| ФСП provenance | test_fsp_demo_provenance.py (4) | seed/manual/real источники |
| БД-пулы | test_database_pool.py (2) | только конфигурация engine |

Оценка: test_api.py делает ~118 вызовов реальных маршрутов API (≈90 обращений к status_code, 45 явных 4xx-литералов: 401/403/404/409/422/429 и т.п.) – тесты проверяют настоящие HTTP-контракты и негативные сценарии, а не только happy path. Ограничения: PostgreSQL-конкурентность только SKIP; SMTP/MAX/LLM замоканы; во frontend нет рендер/e2e-тестов (12 тестов – чистые функции navigation.ts и assessmentDraft.ts: ролевые маршруты, identity сессии, черновик теста в sessionStorage).

## 3. Frontend

Копия `fsp-match/frontend` → `SCR/tests/frontend`.
- `npm ci --no-fund` (registry.npmjs.org, отдельный npm cache): added 21, audited 22, 0 vulnerabilities.
- `node --test tests/*.test.mjs`: tests 12, pass 12, fail 0. Из корня `fsp-match` без node_modules (`node --test frontend/tests/navigation-assessment.test.mjs`) – тоже 12/12.
- `npm run typecheck`: 0 ошибок. `npm run build` (`tsc -b && vite build`, vite 7.3.6): 40 modules; `dist/index.html` 0.83 kB, `dist/assets/index-D4eDyCLn.css` 68.44 kB, `dist/assets/index-BtiYZ8F9.js` 333.59 kB.
- `npm audit --omit=dev`: found 0 vulnerabilities (полный `npm audit` – тоже 0).
- Node: локально v24.14.0, README/Dockerfile – Node 22. На Node 22 не запускалось (NOT_RUN), но сборка на Node 24 побайтно совпала с production (собранной в node:22-alpine).

## 4. Production: паритет ассетов, API, TLS (только публичные GET/HEAD)

| Объект | Production | Локальный build | Итог |
| --- | --- | --- | --- |
| `/` HTML (829 B) | `c9be0d8c508ef9b20c68ce951df6164c511e751f67b416269767f3f69a4fcdba` | dist/index.html – тот же | равны |
| `/mini/max` HTML (829 B) | тот же SHA256 | dist/index.html | равны |
| `/assets/index-BtiYZ8F9.js` (333 587 B, application/javascript) | `f3eea81e5bf7f5d8a5c57c776d490c27d2d09117a748b9a73ea644ce1cc27571` | тот же | побайтно равны |
| `/assets/index-D4eDyCLn.css` (68 436 B, text/css) | `0a3e5e93207cb7ea57844c33e7a74ba0ccef69a5ebaee92359039c5b0ecfe920` | тот же | побайтно равны |
| Все 22 файла dist (brand PNG/SVG, шрифты, лицензии) | GET каждого | dist | 22/22 равны |

SHA256 JS/CSS совпадают с `delivery-20261011/evidence/runtime-final.json`. 7 backend-файлов source (assessment.py, matching.py, validation.py, candidates.py, worker.py, messengers.py, pdf.py) совпадают с `container_sources` из runtime-final.json (7/7).

API (14:43 UTC): `/api/health` 200 `{"status":"ok","service":"fsp-match"}`; `/api/ready` 200 `{"status":"ready","database":"ok"}`; `/api/version` 200 `{"version":"0.1.0-20261011T104017Z","assessment_version":"assessment-1.0.0","matching_version":"matching-v1"}`; `/privacy`, `/mini/max`, `/` – 200 text/html. CSP и HSTS (max-age=31536000) во всех ответах.
TLS (`curl -vI`): TLSv1.3 / AEAD-CHACHA20-POLY1305-SHA256, ALPN http/1.1, subject CN=fsp.digital-projects.tech, issuer C=US; O=Let's Encrypt; CN=YE1, действует с Oct 6 10:04:17 2026 GMT до Jan 4 10:04:16 2027 GMT, verify ok. Redirect: `http://…/`, `/api/health`, `/mini/max` → 301 на тот же https-путь.

## 5. Offline validation (research/jury-validation-20261007)

Команда (из `SCR/tests/src`): `PYTHONDONTWRITEBYTECODE=1 VENV/bin/python research/jury-validation-20261007/run_validation.py` → exit 0, 0.36 с, Python 3.12.6 / SQLite 3.45.3; сетевой guard включён.
Сравнение с `evidence/offline-validation-fresh.json` и с поставленным `validation-evidence.json`: **0 различий**, кроме `created_at_utc`, `runtime` (там 3.12.14 / SQLite 3.53.1) и `elapsed_seconds`. Перезапуск: [logs/offline-validation-evidence.rerun.json](logs/offline-validation-evidence.rerun.json); выжимка: [logs/offline-validation-metrics-extract.json](logs/offline-validation-metrics-extract.json). Статус в JSON: `TECHNICAL_SYNTHETIC_EVIDENCE_NOT_INDEPENDENT_HUMAN_VALIDATION`.

Счётчики: 6 категорий (backend_python, frontend_ts, data_sql × junior/middle) × 20 вариантов = **120 форм**; 360 уникальных оценок основных профилей; **720 оценок** всех 6 профилей + 720 повторных оценок (все идентичны); **180 пар** альтернативных форм для test–retest; **114** попыток переноса ответа; 120 независимых Python-проверок SQL-fixtures; **78 checks, 0 failed**.

### 5.1 Подбор (matching-v1.json: 40 кандидатов × 12 потребностей = 480 меток 0/1/2; 6 development + 6 holdout)

Определения (`app/domain/validation.py`):
- **P@5** = (число кандидатов с меткой ≥1 в top-5) / **5**; пустые позиции = 0; среднее по всем 6 запросам split.
- **Precision of returned** = релевантные / фактически возвращённые; запрос с 0 возвращённых исключается.
- **nDCG@5**: DCG = Σ(2^rel − 1)/log2(i+2) по top-5; IDCG – по 5 лучшим gold-меткам из 40; запрос без положительного gold (IDCG=0) исключается → holdout усредняется по **5** запросам (f-unmeasured исключён), P@5 – по 6.
- **Критические несоответствия** = число кандидатов с меткой 0 в top-5, сумма по запросам.

| Режим | Dev P@5 | Dev nDCG@5 | Dev prec. returned | Holdout P@5 | Holdout nDCG@5 | Holdout prec. returned | Крит. dev / holdout |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| evidence (подтверждённые доказательства + условия) | 1.0000 | 0.9854 | 1.0000 | 0.3333 | 1.0000 | 1.0000 | 0 / 0 |
| keyword_baseline (самозаявленные навыки/грейд) | 0.6667 | 0.5574 | 0.6667 | 0.2333 | 0.3794 | 0.2333 | 10 / 23 |
| without_fsp (без бонуса ФСП) | 1.0000 | 0.9838 | 1.0000 | 0.3333 | 1.0000 | 1.0000 | 0 / 0 |
| without_assessment (без подтверждения тестом) | 0.8000 | 0.8251 | 0.8000 | 0.2667 | 0.9106 | 0.6667 | 6 / 10 |

Holdout evidence по запросам (возвращено / P@5): b-office 1 / 0.2; b-full 4 / 0.8; f-office 1 / 0.2; f-unmeasured 0 / 0.0 (нет положительного gold, nDCG = null); d-office 1 / 0.2; d-full 3 / 0.6 → 2.0/6 = 0.3333. Dev evidence nDCG: 5 запросов = 1.0, b-junior = 0.9125 → 0.9854.
Дополнительно @10 (P@10 / nDCG@10, dev → holdout; крит.): evidence 0.6667/0.9984 → 0.1667/1.0 (0/0); keyword 0.5333/0.6404 → 0.15/0.4643 (28/51); without_fsp 0.6667/0.9970 → 0.1667/1.0 (0/0); without_assessment 0.6667/0.9536 → 0.1667/0.9665 (10/15).
Сравнение с `matching-results-v1.json`: fixture SHA, версия `matching-1.0.0` и все метрики всех режимов совпали (также подтверждено CLI `python -m app.domain.validation`). external_blind_holdout = false, human_validation = NOT_CONDUCTED, independent_human_review = PENDING.

### 5.2 Тестирование навыков (пороги: Junior 65, Middle 75, каждая компетенция ≥ 50%)

| Контролируемый профиль | Балл (все 120 форм) | Junior подтверждён | Middle подтверждён |
| --- | ---: | ---: | ---: |
| weak (все ответы неверны) | 0 | 0/60 | 0/60 |
| borderline (ошибка трассы + 1 из 3 верных multi-choice) | 66.67 | 60/60 | 0/60 |
| strong (эталон) | 100 | 60/60 | 60/60 |
| below_middle_cutoff (ошибка трассы + 2 из 3) | 73.33 | 60/60 | 0/60 |
| above_middle_cutoff (только ошибка трассы) | 80 | 60/60 | 60/60 |
| competency_blindspot (провал единственного вопроса компетенции) | 80 | 0/60 | 0/60 |

- **Test–retest**: 180 пар (6 категорий × 3 основных профиля × 10 пар альтернативных форм); совпадение исхода подтверждения = 1.0 (100%) во всех; средняя абсолютная разница баллов = 0; SD баллов внутри профиля = 0; `score_correlation` и `cohen_kappa` = **null** (ряды константны, корреляция не определена – в JSON указана причина). Порядок weak < borderline < strong выполнен во всех 120 вариантах.
- **Различие содержания**: в каждой категории 20/20 публичных форм различны после удаления `form_id`; 1 blueprint на категорию; одинаковое распределение kind/competency/weight; 5 заданий/100 баллов; публичная проекция без oracle-полей 120/120.
- **Перенос ответа** (дословный эталон варианта 0 на 19 других форм; 114 попыток): **0 подтверждений**. Средний балл (диапазон): backend_python/junior 28.0726 (6.67–46.67); backend_python/middle 18.2484 (6.67–40); frontend_ts/junior 24.9147 (6.67–46.67); frontend_ts/middle 24.5642 (6.67–66.67); data_sql/junior 26.67 (6.67–60); data_sql/middle 40.0021 (26.67–60); общий 27.0787 (6.67–66.67).
- Эти значения совпадают с таблицами `VALIDATION_FOR_JURY.md` (там округлено до 2 знаков: 28,07 / 18,25 / 24,91 / 24,56 / 26,67 / 40,00).

## 6. Запуск документированных команд

| README | Команда | Результат |
| --- | --- | --- |
| fsp-match | `python3 scripts/init_runtime.py` (×2) | PASS: `.secrets` 0700, `runtime.env`/`integrations.env` 0600 (10 и 19 ключей), повтор «no changes», файлы не изменились; значения не выводились |
| fsp-match | `docker compose … config --quiet` / `up -d --build --wait` | NOT_RUN (нет Docker); YAML 4 compose-файлов парсится (Ruby Psych): compose.yaml – db/api/worker/web, compose.dev.yaml – api/worker/mailpit |
| fsp-match | `scripts/run_local.py --help` | у скрипта нет --help: печатает `Usage: run_local.py api\|worker`, exit 1 (ожидаемо; создаёт `runtime/`) |
| fsp-match | `scripts/run_local.py api` | PASS на порту 18110 (в scratch-копии заменён только порт: 8100 занят локальным UI-стендом): health/ready/version 200 |
| fsp-match | `scripts/run_local.py worker` | **FAIL на свежей БД**: `sqlite3.OperationalError: no such table: outbox`; после запуска api – PASS |
| fsp-match | `npm run dev` (5173) | NOT_RUN на 5173 (занят локальным UI-стендом); эквивалент `vite --port 15173 --strictPort` – PASS |
| fsp-match / backend | `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q` | PASS 186/3 (`.venv` → симлинк на VENV; в source `.venv` нет, его создаёт пользователь) |
| fsp-match / frontend | `node --test …navigation-assessment.test.mjs` | PASS 12/12 (из корня и из frontend) |
| fsp-match | `python research/jury-validation-20261007/run_validation.py` | с Python 3.12 – PASS; голый `python` этого Mac (3.11.12) – exit 1, 1/78 check (см. §7) |
| backend | `DATABASE_URL=sqlite:///./fsp-match.db ../.venv/bin/python -m app.migrate` | PASS, идемпотентно (2 запуска) |
| backend | `… -m uvicorn app.main:app --host 127.0.0.1 --port 8805` | PASS: /api/health, /api/ready, /api/version, /api/openapi.json, /api/auth/me – 200; остановлен |
| backend | `../.venv/bin/python -m app.worker` | PASS (после migrate; 6 с, без traceback) |
| frontend | `npm ci`, `npm run build`, `npm run typecheck`, `vite preview` (14173) | PASS |
| frontend / backend | Dockerfile, Dockerfile.tests, tools/run_pg_checks.py | NOT_RUN (нет Docker/PostgreSQL) |
| research | `PYTHONDONTWRITEBYTECODE=1 fsp-match/.venv/bin/python research/…/run_validation.py` | PASS (через VENV) |

Все запущенные серверы (8805, 18110, 15173, 14173) были мои, кратковременные и остановлены; 8100/5173 не трогались.

## 7. Находки и замечания

1. **FAIL (низкая серьёзность, запуск без Docker):** `scripts/run_local.py worker` на новом `runtime/local.db` падает с `no such table: outbox`: схему создаёт только API (`main.py` вызывает `migrate()` при старте), worker – нет. README «Без Docker» перечисляет api и worker без порядка. В Compose порядок обеспечен (`worker depends_on api: service_healthy`). Исправление: в README «сначала api» или `migrate()` в `worker.main()`.
2. **FAIL среды (не дефект продукта):** README в разделе «Тесты и offline-валидация» пишет `python research/...`; на этом Mac `python` = 3.11.12 → exit 1, провал `matching/current_metrics_equal_previous_reference`: 15 per-query nDCG отличаются в последнем знаке (~1e-16; в Python 3.12 `sum()` по float использует компенсированное суммирование), а проверка сравнивает метрики точным равенством. Все округлённые macro-метрики идентичны. Документы требуют Python 3.12 – с ним PASS. Лучше явно писать `python3.12`.
3. `/api/version.matching_version` = `matching-v1` (строка в main.py), а доменная версия `matching.VERSION` = `matching-1.0.0` – разные идентификаторы (информационно).
4. CSP (production и `frontend/nginx.conf`) всё ещё содержит разрешения (script-src, frame-ancestors) для доменов стороннего мессенджера, выведенного из продукта (hardening, информационно). На /api ответах заголовки безопасности дублируются (приложение + прокси: две CSP, два Cache-Control) – браузер применяет обе CSP, функционально безвредно.
5. `init_runtime.py` по умолчанию пишет `SEED_DEMO=true`, `REGISTRATION_ENABLED=false`, `FSP_PORT=8094` и секрет webhook выведенного стороннего мессенджера; README шаг 2 просит вручную поставить SEED_DEMO=false и другие порты – инструкция корректна, но требует ручной правки.
6. Node: README – 22, проверено на 24.14 (сборка всё равно побайтно равна production). PostgreSQL-тесты: 3 SKIP, PASS для PostgreSQL здесь не заявляется.

## 8. Аудит зависимостей

- `VENV/bin/pip-audit -r backend/requirements.lock` (pip-audit 2.10.1; cache/TMPDIR в scratch): **No known vulnerabilities found**, exit 0. Для `requirements-dev.lock` – тоже 0.
- `npm audit --omit=dev`: **found 0 vulnerabilities**.
