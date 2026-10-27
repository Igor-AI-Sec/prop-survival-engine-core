# Core release candidate status

CANDIDATE ONLY. NOT APPROVED FOR PUBLICATION.

Scope: offline Core Python source, neutral execution/market-data contracts, nine
unchanged test modules, four fictional configurations and one synthetic example.
External SDK/platform integrations, research modules and private CI are excluded.

## Evidence

The broader private validation reported:
- PURE500: 500 PASS.
- Research-stdlib: 106 PASS.
- Total: 606 PASS; remaining 494 cases NOT TESTED.

The nine included test modules correspond to the historical PURE500 scope.
The research-stdlib 106 cases are not included. Counts are retained historical
evidence, not results from this newly extracted artifact. No tests, collection,
imports of project code, dependency installation, package builds or native tools
were run during preparation. Core artifact runtime behavior remains unverified.

Static checks cover Python syntax/import targets and symbols, the known child-code
literal, source byte preservation, explicit package inventory, privacy patterns and
ZIP readback. They do not prove all dynamic behavior or source authorship.

Historical native/platform, acquisition/qualification and real-symlink coverage
limitations remain historical gaps; this candidate does not claim to resolve them.
No historical market-data qualification, profitability or LIVE readiness is claimed.

## Decisions before publication

The owner-approved copyright holder is recorded in LICENSE.

1. After creating the target repository, activate and verify GitHub Private
   Vulnerability Reporting as described in SECURITY.md; it is not active yet.
2. Review the exact candidate files, rights declaration and stated evidence limits;
   approve the final public artifact separately. Any change requires updated hashes.

No public repository creation, upload or publication is authorized by this candidate.
