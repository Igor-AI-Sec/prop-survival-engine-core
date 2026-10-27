# Prop Survival Engine Core

An experimental offline foundation for risk research, simulation and order management (OMS).

## Scope

Core provides domain models, deterministic risk sizing, fictional prop-rule models,
Monte Carlo challenge/reach-avoid simulation, simulated execution and market-data
interfaces, OMS recovery, and in-memory/SQLite persistence and reservation ledgers.
A neutral JSON protocol validator is retained because OMS journal replay imports
it; no bridge transport, platform binding or connection implementation is included.
Instrument mechanics are model inputs, not a current exchange specification feed.
The four bundled configurations are explicitly fictional and include deliberately
incomplete/conflicting provenance cases. They are not provider-approved plans.

This is not a ready LIVE trading system. It guarantees neither financial results
nor compliance with any prop firm's rules. Real brokers, external SDK adapters,
historical-data acquisition/replay and native platform integrations are excluded.
Do not inject real execution clients: a caller-supplied implementation can perform
real operations. Use only local simulated clients and synthetic inputs.

## Candidate status and evidence

The broader private validation reported 500 PURE500 passes plus 106 research-stdlib
passes (606 total); 494 cases remained NOT TESTED. Research modules are excluded here.
This newly separated Core artifact has NOT been independently executed or tested.
Static import and archive checks do not establish runtime correctness, profitability,
security certification or production readiness. See RELEASE_STATUS.md.

## Local prerequisites and layout

Python 3.11+; the runtime source declares no third-party dependencies. Tests require
pytest >=8.0; historical PURE500 validation used pytest 9.1.1 on Windows/Python 3.12.
Packaging requires setuptools >=68. No interpreter, packages, SDKs or binaries are
bundled. Installation, package build and cross-platform behavior of this artifact
have not been verified. Test sources and fictional configurations are supplied in
the source archive; they are not promised as installed-wheel resources.

The `src/` layout requires an approved installation or an explicit local source
path. From the source root, a future locally approved synthetic example is:

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'src')
python -B examples/benchmark_reach_avoid.py
```

This is a future-use example, not a command executed during candidate preparation.
The included nine test modules retain their original content. They write temporary
files; one reproducibility test starts local Python children for hash-seed checks.
No CI workflow or OS network sandbox is bundled. Pytest switches alone do not
isolate the network. Review the local environment before any future execution.

## License and security

MIT is selected for code the owner has the right to license. The approved copyright
holder is specified in LICENSE. Owner-reported provenance is
private development with AI assistance and no knowing copying of external code;
this is not independent proof of authorship. See THIRD_PARTY.md and SECURITY.md.
