# Core v0.1.0: scope and test evidence

This is an experimental offline Python core for risk research, simulation and order management. The source tree has passed local offline tests, but installation and package building remain unverified. It is not ready for live trading.

## What's included

The core source, neutral execution and market-data interfaces, nine inherited test modules, one focused regression module, four fictional prop-plan configurations and one synthetic example are included. Broker and vendor SDK adapters, native platform integrations, research modules and private CI files are not.

## What was tested previously

The larger private project had these historical validation results:

- **PURE500:** 500 PASS.
- **Research-stdlib:** 106 PASS.
- **Total:** 606 PASS, with another 494 cases NOT TESTED.

The nine inherited test modules belong to the historical PURE500 scope. The research-stdlib tests are not part of this repository. The 606 passes therefore do **not** mean that the current Core repository has passed 606 tests.

## What was checked for this Core

The original 75-file candidate was reviewed without running its code. The checks covered the file inventory and hashes, Python syntax, static import references, fictional JSON files, archive integrity and common privacy or secret-leak patterns.

This revision fixes journal-write recovery gating, context-independent Decimal serialization, integer quantity validation, mutable in-memory journal aliases and Fraction target serialization. It adds a focused unittest module and updates the existing policy test for the stricter constructor. Earlier ZIP hashes do not describe this revised source; a future release manifest must use the final repository snapshot.

The **full repository pytest suite passed: 526 passed, 0 failed, 0 skipped, 0 errors**, across 10 test modules, on Linux with Python 3.13.17 and pytest 9.2.0. It includes the 26 focused regression tests, which also passed separately using standard-library unittest on Python 3.12.7. Pytest was installed with permission in a temporary virtual environment outside the repository; this was not a package installation or build. The 526 current-revision passes are separate from the historical 606 passes and must not be added to them. The synthetic example `examples/benchmark_reach_avoid.py` also completed successfully on Linux with Python 3.13.17, using 1,000 simulated trials with fictional inputs; these results say nothing about strategy performance. Package installation, wheel building and wider cross-platform compatibility remain unverified.

During the earlier focused Python 3.12.7 tests, Python audit hooks blocked network, process and link operations and restricted Python-level filesystem writes to owned temporary storage. No guard denials occurred; these controls were not an OS sandbox or native-code firewall, and the host retained site initialization despite the requested -S option. The later full pytest run used an isolated temporary directory, disabled pytest caching and bytecode writes, and directed HTTP(S) proxies to a non-listening local endpoint. Proxy settings are not an OS network firewall.

## What remains unknown

These checks do not prove that all runtime paths work, that the project is secure, or that its source and licensing claims are independently verified. Earlier gaps in native/platform coverage, historical market-data qualification, acquisition workflows and real Windows symlink testing have not been closed by this release.

No profitability, current provider-rule compliance, permission to use third-party services, or live-trading readiness is claimed.

The code is licensed under [MIT](LICENSE) to the extent the owner has the rights to license it. See [Third-party notes](THIRD_PARTY.md) and [Security](SECURITY.md) for additional boundaries and precautions.
