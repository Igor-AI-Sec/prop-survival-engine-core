# Security and trust boundaries

Experimental offline research software; no LIVE trading readiness is claimed.
Use synthetic inputs and simulated clients. Do not supply broker credentials,
real account objects, private data or sensitive journal payloads.

Injected interfaces are not a network boundary: an implementation may execute
arbitrary operations. This source release supplies no external SDK adapters and
does not provide an OS sandbox or firewall. Local SQLite/journal paths and test
temporary directories are caller-owned; source code can write to configured paths.
Filesystem checks are not a guarantee against hostile concurrent mutation.

The OMS uses fixed submission classifications in selected confidentiality paths;
this is not a universal journal redactor. Caller-provided fields remain a boundary.
Do not put secrets into exception messages, metadata, logs or journal records.

## Planned confidential reporting channel

GitHub Private Vulnerability Reporting is the planned confidential channel.
It has not been activated as part of this candidate preparation. After creating
the target repository, the owner must enable and verify the feature before public
publication. Until activation, no operational private reporting channel is claimed.
Do not publish credentials, private journals or sensitive reproductions in public
issues. No email address or alternative contact is asserted here.

Neither the historical passing tests nor candidate static checks establish an
absence of vulnerabilities. The separated Core package has not been runtime-tested.
