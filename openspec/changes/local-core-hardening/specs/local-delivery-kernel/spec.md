## ADDED Requirements

### Requirement: Migration excludes active runtime operations

Schema migration SHALL coordinate with every file-backed Kafa database handle
through one cross-platform project operation lock. An operation that started
before migration SHALL finish before the source backup is read. An operation
that starts after migration is announced MUST fail closed without opening or
mutating the active database.

#### Scenario: Writer already active
- **WHEN** a fact transaction holds the project operation lock before migration begins
- **THEN** migration waits, then includes the committed fact in its verified backup and schema-30 result

#### Scenario: Operation starts during migration
- **WHEN** migration has created its sentinel and owns or is waiting for the project operation lock
- **THEN** a new read or write fails with `migration-in-progress` and cannot reach the fingerprint-to-replace window

#### Scenario: Migration exits abnormally
- **WHEN** migration raises after acquiring the project operation lock
- **THEN** the OS lock is released, the diagnostic sentinel is removed for a handled failure, and later normal operations can proceed

#### Scenario: Stale migration sentinel
- **WHEN** the diagnostic sentinel exists without an active owning migration
- **THEN** Kafa fails closed with the sentinel path and does not silently delete it or open SQLite

### Requirement: Migration rollback keeps projections coherent

Migration SHALL treat generated local projections as bounded derived artifacts
inside the rollback bundle. A failed post-activation validation MUST restore the
verified source database and the exact pre-migration projection state before it
reports rollback complete.

#### Scenario: Final doctor fails
- **WHEN** schema 30 is activated but final database doctor fails
- **THEN** Kafa restores the verified source DB without publishing schema-30 projections

#### Scenario: Projection render partially fails
- **WHEN** one or more schema-30 projections are written and a later renderer fails
- **THEN** Kafa restores every prior projection byte-for-byte, restores any retired view deleted as a render side effect, and removes files that did not exist before migration

#### Scenario: Projection restore fails
- **WHEN** the source DB is restored but any projection cannot be restored and verified
- **THEN** the manifest records `rollback-incomplete`, preserves both errors and artifact paths, and Kafa does not report a successful or complete rollback

#### Scenario: Migration succeeds
- **WHEN** final doctor and every projection render and verification pass
- **THEN** active DB and all generated views describe schema 30 and the manifest retains the verified pre-migration projection backup

### Requirement: High-risk review status is authoritative

High and critical delivery SHALL require an active quality gate whose explicit
review status is `reviewed-local`. Context identifiers are supplementary audit
metadata and MUST NOT promote `same-context-degraded` review into independent
review. Explicit risk acceptance MUST NOT waive this review-status requirement.

#### Scenario: Degraded review supplies distinct-looking IDs
- **WHEN** a high-risk gate is `same-context-degraded` but stores unequal producer and reviewer context strings and all risks are accepted
- **THEN** delivery remains `human-review-required`

#### Scenario: Independent review with accepted risks
- **WHEN** a high-risk gate is `reviewed-local`, producer and reviewer metadata are non-empty and distinct, structured execution is current, and every remaining risk is completely accepted or exempted
- **THEN** Kafa may use the procedural `accepted-risk` path

#### Scenario: Low or medium degraded review
- **WHEN** only low or medium risks apply and the quality gate is `same-context-degraded`
- **THEN** existing degraded local delivery behavior and labeling remain unchanged
