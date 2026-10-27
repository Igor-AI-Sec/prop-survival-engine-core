# Security

Prop Survival Engine Core is experimental offline research software. It has not been validated for live trading or for use with real brokerage accounts.

## Using it safely

Use synthetic inputs and simulated execution clients. Do not pass in broker credentials, real account objects, private market data or sensitive journal contents.

The repository does not include broker adapters or network transports, but that isn't a security boundary. If you plug in your own execution or market-data implementation, that code can make external calls or place real orders. Nothing here provides a sandbox or firewall.

Some code writes to local SQLite databases, journals or temporary files at paths chosen by the caller. The filesystem checks do not protect against every concurrent change. The OMS handles confidentiality in specific submission paths, but it is **not** a general-purpose redactor. Secrets could still appear in caller-provided metadata, exceptions, logs or journal entries, so keep them out of those inputs.

## Reporting a vulnerability

Once this repository is public **and** GitHub Private Vulnerability Reporting has been enabled, use **Security → Advisories → Report a vulnerability** to send details privately to the maintainers.

That reporting feature has not yet been verified as active. Until it is, this repository does not claim to have an operational confidential reporting channel. No alternative email address or contact method is provided.

Please don't post credentials, private journals, sensitive reproduction data or vulnerability details in public issues.

## What the checks don't prove

Historical test passes and static source checks do not establish that the code is free of vulnerabilities. This separated Core repository has not been independently runtime-tested.
