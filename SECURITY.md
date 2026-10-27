# Security

Prop Survival Engine Core is experimental offline research software. It has not been validated for live trading or for use with real brokerage accounts.

## Using it safely

Use synthetic inputs and simulated execution clients. Do not pass in broker credentials, real account objects, private market data or sensitive journal contents.

The repository does not include broker adapters or network transports, but that isn't a security boundary. If you plug in your own execution or market-data implementation, that code can make external calls or place real orders. Nothing here provides a sandbox or firewall.

Some code writes to local SQLite databases, journals or temporary files at paths chosen by the caller. The filesystem checks do not protect against every concurrent change. The OMS handles confidentiality in specific submission paths, but it is **not** a general-purpose redactor. Secrets could still appear in caller-provided metadata, exceptions, logs or journal entries, so keep them out of those inputs.

## Reporting a vulnerability

This public repository has **GitHub Private Vulnerability Reporting enabled**, as confirmed in the maintainer's repository settings. To report a potential security vulnerability privately, use **Security → Advisories → Report a vulnerability**.

If that option is unavailable, do not disclose sensitive vulnerability details in public issues. No alternative private email address or contact method is currently provided.

Please don't post credentials, private journals, sensitive reproduction data or vulnerability details in public issues.

## What the checks don't prove

Historical test passes, static source checks and focused local regression tests do not establish that the code is free of vulnerabilities. The complete Core package has not received full runtime or independent security validation.
