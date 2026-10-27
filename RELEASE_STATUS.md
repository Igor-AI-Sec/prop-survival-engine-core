# Core v0.1.0 release status

This is an experimental offline research source distribution. This document
describes its scope, evidence and limitations; it does not authorize making
the repository public. Repository visibility and publication remain separate
decisions for the owner.

## Included scope

Offline Core Python source, neutral execution and market-data contracts,
nine unchanged test modules, four fictional configurations and one synthetic
example. External SDK/platform integrations, research modules and private
CI are excluded.

## Evidence and limitations

The broader private validation reported:
- PURE500: 500 PASS.
- Research-stdlib: 106 PASS.
- Total: 606 PASS; remaining 494 cases NOT TESTED.

The nine included test modules correspond to the historical PURE500 scope.
The research-stdlib 106 cases are not included. These numbers are historical
evidence from broader private source validation, NOT test results from this
separately extracted Core distribution.

No tests, test collection, imports of project code, dependency installation,
package builds or native tools were run on the extracted Core artifact during
release preparation. Standalone runtime behavior remains unverified.

The original 75-file candidate underwent read-only static checks of Python
syntax, import targets, source inventory, privacy patterns, archive readback
and file hashes. Static checks do not prove dynamic behavior, security,
ownership or source authorship. Changes after that archive check require a
new comparison against the intended release inventory.

Historical native/platform, acquisition/qualification and real-symlink
coverage limitations remain unresolved. No historical market-data
qualification, profitability, provider permission or LIVE trading readiness
is claimed.

## Publication controls

The MIT LICENSE records the owner-approved copyright holder. The owner must
separately authorize any public visibility change after reviewing the exact
repository revision, source/licensing declaration and stated evidence limits.

GitHub Private Vulnerability Reporting is intended as the confidential
reporting channel. It can be enabled on a public repository; once public,
the owner should enable and verify the feature promptly. Until verified,
no active private reporting channel is claimed. See SECURITY.md.

This document is release documentation, not an authorization to publish.
