# Core v0.1.0: scope and test evidence

This is an experimental offline Python core for risk research, simulation and order management. The code is available as a source tree, but this document is not a claim that it has been tested as a standalone package or is ready for live trading.

## What's included

The core source, neutral execution and market-data interfaces, nine existing test modules, four fictional prop-plan configurations and one synthetic example are included. Broker and vendor SDK adapters, native platform integrations, research modules and private CI files are not.

## What was tested previously

The larger private project had these historical validation results:

- **PURE500:** 500 PASS.
- **Research-stdlib:** 106 PASS.
- **Total:** 606 PASS, with another 494 cases NOT TESTED.

The nine test modules here belong to the historical PURE500 scope. The research-stdlib tests are not part of this repository. The 606 passes therefore do **not** mean that the current 75-file Core repository has passed 606 tests.

## What was checked for this Core

The original 75-file candidate was reviewed without running its code. The checks covered the file inventory and hashes, Python syntax, static import references, fictional JSON files, archive integrity and common privacy or secret-leak patterns.

The source files and tests have not been changed since that review. The documentation has been edited, so the original ZIP's hashes no longer describe every file in this repository. The final repository snapshot should be checked again if an exact release manifest is needed.

No tests were collected or executed on the extracted Core. No project modules were imported at runtime, dependencies installed, packages built or native tools run. Its behavior as a standalone project remains unverified.

## What remains unknown

These checks do not prove that all runtime paths work, that the project is secure, or that its source and licensing claims are independently verified. Earlier gaps in native/platform coverage, historical market-data qualification, acquisition workflows and real Windows symlink testing have not been closed by this release.

No profitability, current provider-rule compliance, permission to use third-party services, or live-trading readiness is claimed.

The code is licensed under [MIT](LICENSE) to the extent the owner has the rights to license it. See [Third-party notes](THIRD_PARTY.md) and [Security](SECURITY.md) for additional boundaries and precautions.
