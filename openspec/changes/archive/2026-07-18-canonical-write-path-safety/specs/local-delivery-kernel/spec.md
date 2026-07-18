## ADDED Requirements

### Requirement: Canonical project paths remain inside pinned local authority

Kafa SHALL interpret every canonical runtime, database, projection, template,
execution-artifact, and migration path relative to one pinned project root. It MUST fail
closed before reading or writing when a relative path is lexically invalid or an existing
ancestor or target is a symbolic link, junction, reparse point, non-regular file,
hard-linked file, or cross-device ancestor.

#### Scenario: Descendant symbolic link redirects outside the project
- **WHEN** any ancestor or final target of a canonical project path is a symbolic link or junction, even if it currently resolves back inside the project
- **THEN** Kafa returns `unsafe-project-path` with the relative path and does not read, alter, delete, or chmod the linked target

#### Scenario: Existing target has another hard link
- **WHEN** a canonical file target has a link count greater than one
- **THEN** Kafa returns `unsafe-project-path: <relative>: hard-linked-target` before mutation and leaves every linked name unchanged

#### Scenario: Relative path has traversal or Windows redirection syntax
- **WHEN** a canonical operation receives an absolute path, `..`, a drive or UNC path, an alternate-data-stream component, reserved device name, or a component ending in a dot or space
- **THEN** Kafa rejects it as `invalid-relative-path` without opening a descendant path

#### Scenario: Project root is itself a symlink alias
- **WHEN** the caller selects a project through a root-level symlink
- **THEN** Kafa resolves that alias once, pins the resulting real project root, and applies no-follow rules to every descendant

#### Scenario: Platform cannot prove path safety
- **WHEN** the running platform cannot expose the required ancestor, reparse, file identity, or link-count checks
- **THEN** Kafa returns `platform-safety-unavailable` and performs no pathname-only fallback write

### Requirement: Canonical publication is atomic and identity checked

Kafa SHALL create, replace, and delete canonical project files relative to pinned parent
handles. It SHALL recheck path identity before publication and fsync the file and affected
directory so an interrupted or raced operation cannot be reported as a completed write.

#### Scenario: Target identity changes between audit and replace
- **WHEN** a target or ancestor is exchanged after initial validation but before atomic publication
- **THEN** Kafa reports `path-identity-changed`, does not follow the replacement, and does not report the operation successful

#### Scenario: Projection target is unsafe before mutation
- **WHEN** any projection that a mutation can publish is unsafe before the DB transaction commits
- **THEN** Kafa rejects the mutation before commit and no subset of new views is published

#### Scenario: Retired projection is a link
- **WHEN** cleanup encounters a linked or non-regular retired projection
- **THEN** Kafa fails closed without chmod, unlink, or mutation of its referent

#### Scenario: Initialization encounters an unsafe destination
- **WHEN** `.gitignore`, a generated view, DB-family path, or one of the three agent-template destinations is unsafe
- **THEN** initialization fails before its first project mutation and does not partially initialize the project

### Requirement: File-backed SQLite uses the same safe operation authority

Kafa SHALL make every file-backed Store connection, transaction, backup, operation lock,
and migration sentinel check use the pinned project filesystem authority for its complete
lifecycle. SQLite MUST NOT implicitly create a database through an unverified pathname,
and its main DB plus WAL, SHM, and journal family SHALL remain safe and identity-consistent.

#### Scenario: Database or sidecar is linked
- **WHEN** the main DB, WAL, SHM, or journal path is a symbolic link, reparse point, hard-linked target, or non-regular file
- **THEN** Kafa fails before SQLite opens the database and leaves the external object unchanged

#### Scenario: Operation lock is redirected
- **WHEN** the operation-lock path or an ancestor is unsafe
- **THEN** Kafa cannot acquire a different file through the redirect and reports an unsafe project path rather than entering the DB operation

#### Scenario: Sentinel is redirected or unreadable
- **WHEN** the migration sentinel path is linked, non-regular, or cannot be safely read
- **THEN** normal operations fail closed without opening SQLite or treating migration as absent

#### Scenario: SQLite file identity changes after connect
- **WHEN** the verified DB-family identity differs after connection, journal setup, or before close
- **THEN** Kafa closes the connection, reports `path-identity-changed`, and does not report the operation successful

#### Scenario: In-memory Store is used
- **WHEN** tests use `InMemoryStore`
- **THEN** no project filesystem lock or path check is required and existing in-memory semantics remain unchanged

### Requirement: Migration and rollback preserve safe filesystem authority

Schema migration SHALL create and retain its sentinel, operation lock, backup, staging,
manifest, failed-database, sidecar, and projection-rollback artifacts through the same
pinned project filesystem authority until migration succeeds or rollback is verified
complete.

#### Scenario: Unsafe migration path exists before activation
- **WHEN** any active, backup, staging, manifest, failed-DB, sidecar, or projection backup path is unsafe before schema 30 activation
- **THEN** migration fails before activation, preserves the source DB and external target bytes, and retains diagnostics when unchanged authority cannot be verified

#### Scenario: Rollback target becomes unsafe after activation
- **WHEN** DB or projection rollback cannot safely replace or delete a target after activation
- **THEN** the manifest and sentinel record `rollback-incomplete`, retain both original and restore errors plus diagnostic paths, and Kafa does not report success or complete rollback

#### Scenario: Failed schema sidecar is redirected
- **WHEN** quarantine encounters a linked, hard-linked, reparse, or non-regular failed WAL/SHM/journal path
- **THEN** rollback remains fail closed and does not open the restored source DB as authoritative

#### Scenario: Safe migration succeeds
- **WHEN** all canonical identities remain safe and database, doctor, projections, and manifest validation pass
- **THEN** schema 30 activation completes with the existing backup and rollback contract and the sentinel is removed only after verified success

### Requirement: Execution evidence cannot follow unsafe artifact paths

Controller execution SHALL safely create stdout and structured-result artifacts and
SHALL safely read or copy declared result bytes before they become immutable execution
evidence. Container result capture SHALL apply the same project-path policy.

#### Scenario: Structured result target is a link
- **WHEN** a declared project result path or its artifact destination is a symbolic link, junction, reparse point, hard-linked file, or unsafe ancestor
- **THEN** verification rejects the result before recording a passing execution and does not read or overwrite the referent

#### Scenario: Container artifact destination changes identity
- **WHEN** a container result destination is exchanged during capture
- **THEN** Kafa reports the path safety failure and records no passing validation from those bytes

#### Scenario: Verification command is not sandboxed
- **WHEN** a local target does not select the existing container policy
- **THEN** this filesystem requirement does not claim to sandbox the arbitrary command, while Kafa's own artifact operations remain safe

### Requirement: Doctor reports path safety before opening state

Runtime doctor and `kafa project doctor` SHALL audit canonical paths and migration
sentinel state before opening SQLite. The wrapper CLI SHALL delegate to the hardened
runtime contract instead of maintaining a weaker independent lock/SQLite path.

#### Scenario: Doctor finds an unsafe DB path
- **WHEN** doctor encounters an unsafe canonical DB, lock, sentinel, projection, or template destination
- **THEN** it reports the relative path and reason without opening SQLite or modifying the target

#### Scenario: Migration sentinel is active or stale
- **WHEN** doctor sees a safe migration sentinel
- **THEN** it preserves the existing `migration-in-progress` or recovery guidance and does not open SQLite

#### Scenario: Normal project is healthy
- **WHEN** all canonical paths are ordinary and existing schema 30 invariants and projections are valid
- **THEN** doctor preserves its existing output shape and successful result
