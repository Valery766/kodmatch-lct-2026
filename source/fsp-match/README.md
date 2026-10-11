# КОД.МЭТЧ – локальный запуск и проверка

Источник приложения – финальный опубликованный релиз `20261011T104017Z` (0.1.0). Исходная копия от 11.10.2026 сохраняет backend/app, frontend/src, frontend/public, Dockerfile и бизнес-механику побайтно. Это самостоятельный веб-сервис обратного найма с двумя ролями, шестью тестовыми категориями, объяснимым подбором, приглашениями, защищёнными контактами, вакансиями и короткими заданиями.

## Быстрая независимая проверка: Compose + Mailpit

Нужны Python 3.12 для генерации private runtime, Docker Engine и Docker Compose v2. Команды выполняются из `source/fsp-match`. Веса LLM и production доступы для базовой проверки не нужны. Образы/пакеты могут потребовать интернет при первой сборке; наличие исходника и повторные unit-тесты не означают новой чистой Docker-установки.

1. В новом отдельном каталоге выполните `python3 scripts/init_runtime.py`. Он создаёт `.secrets/runtime.env` и `.secrets/integrations.env` с правами0600 и никогда не заменяет существующие файлы. Не используйте production env, ключи, БД или volumes.
2. В локальном `.secrets/runtime.env` установите отдельные имена и предварительно свободные порты:

```dotenv
FSP_COMPOSE_PROJECT=kodmatch-jury-local
FSP_NETWORK_NAME=kodmatch-jury-local-private
FSP_VOLUME_NAME=kodmatch-jury-local-postgres
FSP_PORT=18094
FSP_MAILPIT_PORT=18025
SEED_DEMO=false
```

3. В локальном `.secrets/integrations.env` заполните только обозначенные локальные поля:

```dotenv
LEGAL_OPERATOR_NAME=Локальная проверка прототипа
LEGAL_OPERATOR_CONTACT=demo@example.org
```

Эти подписи предназначены только для изолированного development-экземпляра с тестовыми данными. Они не являются реквизитами настоящего оператора публичного сервиса. Production требует действительных владельческих данных и защищённого внешнего SMTP. Все внешние токены остаются пустыми: compose.dev отключает внешние мессенджеры и отправляет письма только в собственный Mailpit.

4. Проверьте состав и запустите **новый изолированный** экземпляр:

```bash
docker compose --env-file .secrets/runtime.env --env-file .secrets/integrations.env -f compose.yaml -f compose.dev.yaml config --quiet
docker compose --env-file .secrets/runtime.env --env-file .secrets/integrations.env -f compose.yaml -f compose.dev.yaml up -d --build --wait
```

5. Откройте `http://localhost:18094`, а тестовую почту – `http://localhost:18025`. Зарегистрируйте две новые тестовые роли. Письмо появляется в Mailpit; откройте ссылку, затем явно подтвердите адрес на странице приложения. Письма не отправляются внешним получателям. При изменении FSP_PORT ссылка использует этот порт.
6. Кандидат: сохраните профиль/анкету, пройдите тест, разрешите видимость. Работодатель: сохраните компанию и потребность с зарплатой, получите подборку, направьте приглашение. Кандидат принимает; только компания-пригласитель получает контакты. По возможности проверяйте роли в разных профилях браузера.

Смена подтверждённой категории ограничена: тот же тестовый уровень нельзя сбросить ради демонстрации; новая попытка учитывает серверный срок45мин и cooldown90дней. Для повторного чистого прогона используйте новые тестовые аккаунты или другую категорию, а не меняйте записи вручную.

Если хотите готовые синтетические профили, установите SEED_DEMO=true до первого запуска и используйте уникальный >=12-символьный DEMO_PASSWORD, сгенерированный в private runtime. Seed идемпотентен и не заменяет действующую регистрацию/историю. Основная регистрационная дорожка выше использует SEED_DEMO=false.

Не выполняйте `down -v`, prune или массовую очистку на общей машине. Остановка собственного локального экземпляра: та же команда Compose с `stop`; его volume сохраняется. На сервере с действующими проектами порты/имена выбирайте после проверки занятости.

## Без Docker

Python3.12 и Node22; создайте **новую** `.venv` в этом `fsp-match` и установите `backend/requirements.lock`, frontend зависимости – через `npm ci` из `frontend`. Это инструкция будущего запуска, а не действие текущей упаковки. `scripts/run_local.py api` и `scripts/run_local.py worker` используют свой SQLite runtime и локальный SMTP127.0.0.1:1025; для писем нужен отдельный Mailpit. `npm run dev` даёт localhost5173 с proxy на API8100. Не запускайте эту ветку одновременно с чужими процессами на тех же портах.

## Тесты и offline-валидация

Из `source/fsp-match` в своей среде:

```bash
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
node --test frontend/tests/navigation-assessment.test.mjs
```

Последняя команда не вызывает backend или сеть. Из `source` запускается `python research/jury-validation-20261007/run_validation.py`; запускайте в рабочей копии, поскольку метод обновляет свой JSON-результат. Все authored fixtures включены. PostgreSQL concurrency tests требуют отдельного FSP_TEST_DATABASE_URL; без него3проверки SKIP и не считаются PostgreSQL PASS. Они используют только отдельную случайную схему тестовой БД.

## Production и необязательная LLM

https://fsp.digital-projects.tech – отдельный existing production deployment. Эта исходная копия не меняет его env/volume/SSH/nginx. Production использует HTTPS, реальные данные оператора, внешнюю подтверждённую почту и единственный MAX-бот; доступы не вложены в код.

Локальная Qwen3 4B Instruct2507 Q4_K_M доступна приложению только в приватной сети Ollama; публичного порта нет. `config/compose.local-llm.yaml` – **опциональный** overlay для уже отдельно установленной модели/сети, его не надо добавлять к быстрому Mailpit-запуску. Код не скачивает веса. Deployment overlay задаёт1concurrent request и deadline35s; кодовые defaults отдельно описаны в API. Без модели подбор работает через детерминированный fallback. `LLM_PERSONAL_DATA_ALLOWED=false`: резюме обрабатывается локальным PDF-parser, контакты/сырой профиль не отправляются модели. LLM не присваивает грейд/балл и не раскрывает контакты.

## Документы и происхождение

[API](backend/API_CONTRACT.md), [backend](backend/README.md), [frontend](frontend/README.md), [финальная документация](../../documents/КОД.МЭТЧ_Техническая_документация.pdf), [инструкция жюри](../../documents/КОД.МЭТЧ_Инструкция_для_жюри.pdf), [внешние датированные доказательства](../../evidence/), [source manifest](../source-manifest.json). Старые рабочие ledgers, закрытое ТЗ/переписка, dossier и исторические QA-PDF не нужны для запуска и в исходник не включены.
