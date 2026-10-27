# Core v0.1.0: scope and test evidence

This is an experimental offline Python core for risk research, simulation and order management. The code is available as a source tree, but this document is not a claim that it has been tested as a standalone package or is ready for live trading.

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

The **26 focused regression tests passed** locally on Python 3.12.7 using unittest, synthetic execution doubles and temporary SQLite databases. This is separate evidence from the historical 606 passes. No dependencies were installed. The existing pytest suite was **NOT RUN** because pytest was unavailable in the base interpreter; package installation/build, the example and cross-platform behavior remain unverified for this revision.

During the focused tests, Python audit hooks blocked network, process and link operations and restricted Python-level filesystem writes to owned temporary storage. No guard denials occurred. These controls are not an OS sandbox or native-code firewall. The host retained site initialization despite the requested -S option; trusted interpreter startup remains outside the test guard boundary.

## What remains unknown

These checks do not prove that all runtime paths work, that the project is secure, or that its source and licensing claims are independently verified. Earlier gaps in native/platform coverage, historical market-data qualification, acquisition workflows and real Windows symlink testing have not been closed by this release.

No profitability, current provider-rule compliance, permission to use third-party services, or live-trading readiness is claimed.

The code is licensed under [MIT](LICENSE) to the extent the owner has the rights to license it. See [Third-party notes](THIRD_PARTY.md) and [Security](SECURITY.md) for additional boundaries and precautions.
