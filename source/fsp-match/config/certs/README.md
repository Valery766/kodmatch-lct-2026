# MAX TLS certificate bundle

`russian_trusted_bundle.crt` contains public Russian Trusted Root CA and Russian Trusted Sub CA certificates, reused from the owner's existing MAX project archive. It contains no private key. SHA256: `b1696a7fa10f6baf306178116ceb0424f174c4395fa4fd44e30f85882b3459a5`.

The current [official MAX API](https://dev.max.ru/docs-api) requires the certificates of the Ministry of Digital Development. Official distribution: [Gosuslugi](https://www.gosuslugi.ru/crt), [root](https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer), [intermediate](https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer). Live verification and comparison results are recorded in `validation/max-ca-provenance.json` outside the source package.

Compose mounts this exact public file read-only into this project's API and worker. `MAX_CA_BUNDLE` is consumed only by MAX's `httpx` SSL context with certificate and hostname verification enabled. It changes neither global trust nor SMTP/LLM TLS settings. Root certificate expires in 2032, intermediate in 2027: maintain this project file when the official chain changes. Never disable TLS verification as a fallback.
