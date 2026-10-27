# Source provenance and third-party boundary

The owner states that this project was developed privately, outside employment,
with AI assistance and without knowingly copying third-party code. This is a
provenance declaration, not independent verification of authorship or legal clearance.
MIT applies only to material the owner has the right to license. LICENSE records
the owner-approved copyright holder; external components retain their own terms.

This Core candidate contains project Python source, test source, an offline example
and fictional JSON configurations. No confirmed copied third-party implementation
or third-party copyright/license header was identified in the scoped local review.
Absence of such a header is not proof of ownership. No NOTICE obligation has been
established; if reused material is identified, review its actual terms and preserve
required notices or exclude it before publication.

External components are prerequisites, not included components:

| Component | Role | Distribution boundary |
| --- | --- | --- |
| Python standard library | Runtime | Interpreter and libraries are not bundled |
| setuptools >=68 | Build backend | Not bundled; its own terms apply |
| pytest >=8.0 | Optional test dependency | Not bundled; its dependencies retain their own terms |

The broader private tests used pytest 9.1.1 and its separately reviewed dependencies.
This public candidate does not redistribute those packages or claim a new dependency
artifact audit. Dependency declarations do not relicense external software.

Databento, DBN, ProjectX, Nautilus, native HFT, NinjaTrader integrations and their
SDKs/platform binaries are excluded. No market-data samples, real account data or
vendor documentation are bundled. Instrument names and numeric mechanics in the
core are reference model inputs, not affiliation, trademark rights or vendor approval.
Their presence is not a license to redistribute third-party feeds or proprietary
catalogs. No online terms review was performed for this candidate.
