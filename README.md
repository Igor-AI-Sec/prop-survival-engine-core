# Prop Survival Engine Core

This repository contains the offline Python core of Prop Survival Engine. It explores risk rules, simulated trading challenges and order management without connecting to a live broker or market-data service.

It's research software, not a trading bot or a system ready to trade real accounts.

## What it does

- **Risk:** models instruments, position sizing, contract limits, policies and reservations.
- **Prop rules:** evaluates fictional challenge rules, account limits and trading calendars.
- **Simulation:** runs challenge and reach-avoid simulations with reproducible inputs, seeds and simulated execution.
- **Order management (OMS):** includes event ingestion, journal replay, recovery logic and reservation tracking.
- **Persistence:** provides in-memory and SQLite implementations.

The repository also includes neutral interfaces for execution and market data. These define how another component could interact with the core; they do not connect to any trading platform. A JSON bridge-protocol validator is included because the OMS replay code imports it. There is no bridge transport or external platform binding.

The four example prop-plan configurations are fictional. Some deliberately have missing or conflicting provenance information to exercise those cases. They are not real providers' approved rules. Instrument specifications are model inputs, not an up-to-date exchange reference.

## What it doesn't do

There is no real broker adapter, live market-data feed, historical-data acquisition pipeline, native trading-platform integration or production trading setup in this repository. The separate research modules and private validation infrastructure are not included either.

Nothing here establishes that a strategy is profitable, that the code follows a particular prop firm's current rules, or that any provider has approved its use. Don't connect it to a real account. Although the bundled components are intended for offline work, a caller-supplied execution implementation could still perform real operations. Stick to simulated clients and synthetic data.

## Running the synthetic example

You need Python 3.11 or newer. The core has no third-party runtime dependencies. Its code lives under `src/`, so imports need either an installation or that directory on your Python path.

From the repository root, the following PowerShell commands illustrate how to run the synthetic example:

```powershell
$env:PYTHONPATH = (Join-Path $PWD 'src')
python -B examples/benchmark_reach_avoid.py
```

The synthetic example was successfully run on Linux with Python 3.13.17 (1,000 simulated trials). Its results use fictional inputs and do not indicate trading performance. Package installation, wheel building and cross-platform behavior remain unverified. The build configuration requires `setuptools>=68`; the tests need `pytest>=8.0`. The test files and fictional JSON plans are part of this source tree, but are not guaranteed to be included in an installed wheel.

## Tests and what the results mean

Earlier private validation of the larger project reported **500 PURE500 passes** and **106 research-stdlib passes**, or **606 passes in total**. Another **494 cases were not tested**. Nine inherited test modules come from the PURE500 scope; the 106 research-stdlib cases are not included.

Those results are historical evidence for the larger private source, **not test results for this extracted Core repository**. The current Core revision passed its **full repository pytest suite: 526 passed, 0 failed, 0 skipped, 0 errors** on Linux with Python 3.13.17 and pytest 9.2.0. This includes **26 focused offline regression tests** for journal-write failures, Decimal fingerprints, order quantities, in-memory journal isolation and exact rational risk targets. Those 26 regressions had also passed separately on Python 3.12.7. The 526 current-revision results are independent of, and must not be added to, the historical 606 passes. Package installation, wheel builds and wider platform compatibility remain unverified. See [Release status](RELEASE_STATUS.md) for the scope and limits.

The included tests can write temporary files, and one reproducibility test launches local Python child processes. There is no CI workflow, OS network sandbox or firewall provided here. Running pytest alone does not isolate the network, so review the code and run it in an environment you control.

## License and security

The project uses the [MIT license](LICENSE) for material the owner has the right to license. The owner describes the code as privately developed outside employment, with AI assistance and without knowingly copying third-party source. That account has not been independently verified. See [Third-party notes](THIRD_PARTY.md) for the boundaries of that declaration.

For safe-use guidance and how to report a security issue, see [Security](SECURITY.md).
