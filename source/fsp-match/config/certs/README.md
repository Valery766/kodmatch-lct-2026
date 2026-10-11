# КОД.МЭТЧ – сертификаты TLS для MAX

`russian_trusted_bundle.crt` содержит публичные сертификаты Russian Trusted Root CA и Russian Trusted Sub CA. Контрольная сумма SHA256: `b1696a7fa10f6baf306178116ceb0424f174c4395fa4fd44e30f85882b3459a5`.

Источники: [документация API MAX](https://dev.max.ru/docs-api), [сертификаты на Госуслугах](https://www.gosuslugi.ru/crt), [корневой сертификат](https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer), [промежуточный сертификат](https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer).

Docker Compose подключает файл к API и обработчику очереди только для чтения. `MAX_CA_BUNDLE` применяется к SSL-контексту `httpx` для MAX с проверкой сертификата и имени узла. Настройки доверия операционной системы, SMTP и языковой модели сохраняются отдельно.

Корневой сертификат действует до 2032 года, промежуточный – до 2027 года. При изменении официальной цепочки обновляется только этот проектный файл; проверка TLS остается включенной.
