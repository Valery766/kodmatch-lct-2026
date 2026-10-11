# КОД.МЭТЧ – API версии 1

Описание API финального релиза `20261011T104017Z` (версия 0.1.0). Все маршруты имеют префикс `/api`; формат данных – JSON UTF-8. Идентификаторы – строки UUID, время – UTC.

Роли: `candidate`, `employer`. Специализации: `backend_python`, `frontend_ts`, `data_sql`. Уровни: `junior`, `middle`. Ошибки возвращаются в виде `{detail: string|object}` с кодами HTTP 400/401/403/404/409/422/429. Объект другого владельца возвращает 404; неизвестные схеме поля отклоняются.

## Авторизация

Начальный запрос `GET /auth/me` возвращает `{user:null|{id,email,role,email_verified,is_demo},csrf_token}`. Анонимному браузеру выдается предварительная cookie, после входа – непрозрачная серверная сессия. Cookies используют `HttpOnly`, `SameSite=Lax` и `Secure` на публичном HTTPS-сайте.

Каждый изменяющий запрос содержит `X-CSRF-Token` из `/auth/me` или ответа авторизации. Проверяется источник `Origin`; браузер отправляет cookie с `credentials:include`. Запросы GET сохраняют деловое состояние.

- `POST /auth/register {email,password,role,consent_version:"2026-10-06"}` → 201 `{message}`. Письмо подтверждения отправляется через очередь SMTP; токен остается на сервере.
- `POST /auth/confirm-email {token}` → `{message}`. Одноразовый токен действует 24 часа.
- `POST /auth/login {email,password}` → структура ответа `/auth/me`. Для входа требуется подтвержденный адрес.
- `POST /auth/logout {}` → `{message}`. Сессия отзывается.
- `POST /auth/forgot-password {email}` → одинаковое `{message}` для всех запросов, сохраняющее конфиденциальность существования аккаунта.
- `POST /auth/resend-confirmation {email}` → `{message}`. Создает новый токен подтверждения для обычного аккаунта. Демонстрационные профили используют начальные тестовые данные.
- `POST /auth/reset-password {token,password}` → `{message}`. Токен действует один час и используется один раз; смена пароля отзывает все сессии.

## Кандидат

- `GET /catalog` → `{specializations:[{id,label,grades}],grades:[...],work_formats:[...]}`.
- `GET/PUT /candidate/profile` доступны владельцу. Поля PUT: `{full_name,headline,bio,location,work_format,skills:string[],roles:string[],experience_years:number|null,soft_skills:string[],specialization,declared_grade,salary_min,salary_max,contacts:{phone,telegram,website},fsp_id,fsp_link,visible,consent_version}`. `roles` и `soft_skills` содержат до 20 элементов длиной от 1 до 80 символов; `experience_years` – число от 0 до 60 либо `null`. Самоописание сохраняется отдельно от подтвержденной квалификации. Зарплата в профиле допускает `null`. Сервер определяет `verified_grade` и `verified_skills`; ответ GET содержит историю и даты согласия. Поле `contacts.telegram` сохранено для совместимости данных, действующий канал уведомлений – MAX. Привязка MAX выполняется через `/integrations`.
- `POST /candidate/profile/import` принимает PDF в поле `file` размером до 5 МиБ → `{fields,warnings,provenance:"pdf_import",requires_confirmation:true,parser}`. Импорт создает предварительный просмотр; владелец подтверждает поля перед сохранением. Оригинал остается вне постоянного хранения приложения.
- `GET /candidate/profile/pdf` возвращает собственный PDF. Работодатель использует `/candidates/{id}/pdf` с проверкой своего разрешения на контакты.
- `GET /candidate/survey` → `{survey:null|{specialization,declared_grade,answers,history_id,created_at}}`. Возвращает последнюю сохраненную анкету текущего кандидата. Используется для восстановления формы при повторном открытии.
- `POST /candidate/survey {specialization,declared_grade,answers:object}` → `{specialization,declared_grade,history_id}`.
- `GET /candidate/history` → `{items:[{kind,created_at,payload}]}`.
- `GET /candidate/fsp` → `{status:"synthetic_demo"|"not_linked"|"unverified",achievements:[],source,verified:false}`. `POST /candidate/fsp/sync {}` обновляет записи демонстрационного реестра ФСП; происхождение записи сохраняется в ответе.
- `GET /assessments/catalog` возвращает доступные категории. `POST /candidate/attempts {specialization,grade}` → 201 `{id,form,status,expires_at}`. Форму создает сервер; эталонные данные остаются закрытыми. Повтор начала той же категории возвращает активную попытку. После неудачного Middle можно добровольно выбрать Junior.
- `GET /candidate/attempts` → `{items}`. `GET /candidate/attempts/{id}` доступен владельцу. `POST /candidate/attempts/{id}/submit {answers:object}` → `{id,status,result,verified_grade,cooldown_until}`. Повторная отправка идемпотентна. Повтор категории доступен через 90 дней; ранее подтвержденный уровень сохраняется.

## Работодатель: компания и потребности

- `GET/PUT /employer/company`: `{name,description,website,inn,contact_method}`. Статусы источника проверки: `unverified`, `synthetic_demo`; в интерфейсе отображается соответствующая отметка.
- `GET/POST /employer/needs`; `GET/PUT /employer/needs/{id}`. Потребность: `{title,description,specialization,grade,required_skills:[],preferred_skills:[],salary_min:int,salary_max:int,work_format,location,require_fsp:bool}`. Обязательная зарплатная вилка: `salary_min>0`, `salary_max>=salary_min`, рубли за месяц до вычета НДФЛ.
- `POST /employer/needs/parse {text}` → `{fields,warnings,parser,requires_confirmation:true}`. Модель предлагает структуру потребности; сервер проверяет ответ, работодатель подтверждает поля. При необходимости используются локальные правила.
- `GET /employer/needs/{id}/search` → `{items:[{id,profile,score,explanation}],formula_version}`. Записывается история поиска. До разрешения конкретной компании проекция скрывает email, телефон, полное имя, сайт и идентификатор ФСП; имя отображается в сокращенном виде.
- `GET /employer/search-history` → `{items}`.
- `POST /employer/shortlist {need_id,candidate_id}`, `DELETE /employer/shortlist/{id}`, `GET /employer/shortlist` управляют коротким списком.

## Объяснение подбора и языковая модель

`POST /employer/needs/{need_id}/candidates/{candidate_id}/explanation {}` → `{source:"llm_grounded"|"deterministic_fallback",provenance:"llm_fact_selection"|"trusted_matching_rules",explanation:string[],selected_fact_ids:string[],facts:[{id,text}],warnings:string[],formula_version,score,changes_matching:false,cached:boolean}`.

Объяснение запрашивает работодатель, владеющий потребностью. Кандидат должен быть видимым, активным, с подтвержденной почтой и соответствовать текущему подбору. Недоступный объект возвращает 404, другая роль – 403, изменение исходных данных во время обработки – 409. Дополнительные поля запроса отклоняются.

Модель выбирает порядок и подмножество из максимум пяти идентификаторов фактов. Ей передаются только категории и числовые параметры совпадения. ФИО, контакты, UUID, идентификатор ФСП, исходный профиль и текст потребности в этот запрос не входят. Итоговые предложения формирует сервер из доверенных фактов; баллы и квалификация определяются алгоритмом подбора. Обычный поиск GET работает без обращения к модели.

На разбор потребности и новое объяснение действует общий бюджет: 20 действий на работодателя за 3600 секунд. При исчерпании возвращается 429 и `Retry-After`. Повторный ответ из кэша использует актуальную проверку доступа и данных, сохраняя бюджет. Кэш процесса содержит до 256 записей; срок успешного результата и отсутствующей настройки – 600 секунд, временного резервного результата – 30 секунд. Ключ включает компанию, потребность, кандидата, исходные данные и провайдера.

Базовые настройки кода: два одновременных обращения и 12 секунд ожидания; допустимые диапазоны – 1–16 и 1–120 соответственно. На публичном экземпляре Qwen3 4B Instruct 2507 Q4_K_M через приватную Ollama обрабатывает один запрос одновременно с ожиданием до 35 секунд. При тайм-ауте, ошибке, превышении размера или неверной структуре ответа применяются детерминированные локальные правила.

Интерфейс модели совместим с OpenAI: базовый URL с `/v1`, маршрут `/chat/completions`, авторизация Bearer, `response_format:{type:"json_object"}`, `temperature:0`, `max_tokens:1600`. Поле `choices[0].message.content` содержит JSON-строку. Ответ содержит до 100000 байт, текстовое содержимое – до 16000 символов. Перенаправления и сжатые ответы отклоняются. После ответа модели повторно проверяются видимость, права владельца и состояние исходных данных.

## Банк кандидатов и приглашения

- `GET /employer/candidates?specialization=...&grade=...&skill=...&fsp=true&page=1&page_size=12` → `{items:[profile],total,page,pages,page_size,filter_evidence}`. Фильтрация SQL выполняется до пагинации. `page>=1`, `page_size` от 1 до 100, по умолчанию 12. Порядок стабилен по идентификатору пользователя. Параметр `fsp=true` выбирает записи с достижениями, `false` – без них; отсутствие параметра показывает все. Фильтры уровня и навыка используют подтвержденные данные. Для профиля без подтвержденной категории возвращается `category:not_verified`. Контакты определяются текущим разрешением компании.
- `GET /candidates/{id}` возвращает проекцию профиля для работодателя; `GET /candidates/{id}/pdf` использует те же права.
- `POST /employer/invitations {need_id,candidate_id,message,contact_method,salary_min,salary_max}` → 201. Предложение направляется без обязательной публикации вакансии. Уникальная пара потребность–кандидат делает повторную отправку идемпотентной.
- `GET /employer/invitations`, `GET /candidate/invitations` → `{items}`. Кандидату видны компания, описание, зарплатная вилка и способ связи.
- `POST /candidate/invitations/{id}/view {}` переводит `sent` в `viewed`. `POST /candidate/invitations/{id}/respond {decision:"accepted"|"rejected"}` сохраняет решение. Только `accepted` открывает контакты отправившей приглашение компании; окончательное решение защищено от противоречащего повтора.
- `GET /candidate/contact-grants` → `{items:[{id,company_id,company_name,source,created_at,revoked_at}]}`. `DELETE /candidate/contact-grants/{identifier}` отзывает собственное разрешение; чужой идентификатор возвращает 404. После отзыва новые ответы API и PDF снова скрывают контакты. Ранее полученные компанией копии остаются вне управления этим маршрутом.

## Вакансии, отклики и короткие задания

- `GET /vacancies`, `GET /vacancies/{id}` доступны авторизованным пользователям. `POST /employer/vacancies`, `PUT/DELETE /employer/vacancies/{id}` управляют собственными вакансиями. Поля соответствуют потребности с дополнительным `published:bool`. DELETE закрывает вакансию с сохранением истории.
- `POST /candidate/vacancies/{id}/respond {message}` → 201. Явный отклик дает разрешение на контакты только компании – владельцу вакансии. `GET /candidate/responses`, `GET /employer/responses` возвращают отклики своей стороны.
- `GET /microtasks` → `{items}`. `POST /employer/microtasks {title,description,specialization,grade,expected_answer,due_at?}` создает задание с закрытым эталоном. `POST /candidate/microtasks/{id}/submit {answer}` → `{id,score,feedback,changes_verified_grade:false}`. Каждое участие сохраняет отдельную историю. Работодатель видит результаты собственных заданий с проекцией профиля по разрешению. `GET /candidate/microtask-history`, `GET /employer/microtasks` возвращают соответствующие истории и задания.

## Уведомления и интеграции

- `GET /notifications` → `{items:[{id,kind,payload,created_at,read_at}]}`; `POST /notifications/{id}/read {}` отмечает прочтение.
- `GET /integrations/status` возвращает состояние каналов без секретов. `POST /integrations/messenger/link-code {platform:"max"}` → `{code,expires_at,platform,url,instruction}`. Код MAX одноразовый, действует 600 секунд. Сервер привязывает пользователя по проверенному подтверждению платформы.
- `POST /feedback/report {subject_type,subject_id,reason}` → 201. Сохраняются автор обращения и предмет жалобы; обработку выполняет оператор сервиса.
- `GET /health` проверяет доступность процесса; `GET /ready` – подключение базы; `GET /version` – версии сборки и алгоритмов.

Очередь доставки хранит `{id,user_id,channel,kind,payload,status,attempts,next_attempt_at,last_error,external_id,created_at}`. Активные каналы – `email` и `max`. Уведомления используют безопасные поля без контактов кандидата. Обработчик вызывает `app.integrations.messengers.deliver(platform,recipient_id,payload,settings)` → `{delivered,external_id?,retry_after?,error?}`. Ошибки доставки повторяются по настройкам очереди и сохраняют решения по приглашениям и права на контакты.

Почтовый токен хранится только в базе и передается в письме; API владельца и журналы используют безопасные проекции. `EMAIL_FROM` и `EMAIL_REPLY_TO` отображаются в `SMTP_FROM` и `SMTP_REPLY_TO`. Reply-To принимает один адрес без имени; пустое значение исключает заголовок. Управляющие символы, CRLF и несколько адресов отклоняются. SSL/STARTTLS использует доверенные сертификаты. Команда подтвердила получение писем и работу MAX на реальных аккаунтах.

## Настройки локального примера

Начальные профили создаются с `SEED_DEMO=true` и `DEMO_PASSWORD`: `candidate@demo.example.com`, `employer@demo.example.com`, `second@demo.example.com`. Происхождение демонстрационных записей сохраняется в профиле, компании и достижениях.

`GET /catalog` также возвращает `registration_enabled`, `demo_mode`, `legal_operator:{configured,name,contact}`, `consent_version`. На публичном сайте используются cookies `__Host-fsp_session` и `__Host-fsp_preauth`. Обычный вход использует `SameSite=Lax`; проверенный подписанный запуск мини-приложения – `Secure` и `SameSite=None`.
