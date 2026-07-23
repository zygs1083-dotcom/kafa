## ADDED Requirements

### Requirement: Single workflow presentation authority

Kafa SHALL ship one versioned machine-readable workflow presentation contract
for owner boundaries, safeguards, Skill routes, stage dependencies, public
command examples, concise labels, and advanced-mode triggers. Every maintained
workflow overview, quickstart path, entry-Skill workflow block, trigger matrix,
full-flow checklist, and Skill-evaluation dependency list MUST be generated from
or mechanically checked against that contract. The contract MUST NOT replace
OpenSpec as normative specification, SQLite as fact authority, or the canonical
delivery prerequisite evaluator as executable gate authority.

#### Scenario: Generated views are current
- **WHEN** the workflow renderer runs in check mode on an unchanged repository
- **THEN** every bounded generated block matches the contract byte-for-byte and a second render produces no diff

#### Scenario: One derived document drifts
- **WHEN** a maintained workflow stage, dependency, route, safeguard, or command is edited only in a derived view
- **THEN** contract validation fails and identifies the file and generated block without changing runtime delivery facts

#### Scenario: Runtime gate is stricter than presentation
- **WHEN** presentation text omits or contradicts a delivery prerequisite
- **THEN** runtime readiness and recording continue to use the canonical prerequisite evaluator and fail closed independently of the text

#### Scenario: Legal partial ordering
- **WHEN** controller verification and task submission both complete before task acceptance but occur in either order
- **THEN** the workflow dependency contract accepts both orderings while continuing to reject acceptance before either dependency

### Requirement: Transactional delivery-plan setup

Kafa SHALL accept a closed versioned local delivery-plan document and atomically
create its generated requirement, acceptance, optional failure mode, task, test
target, acceptance-target qualification, and relation facts in one schema-31
transaction. It SHALL complete all semantic and identifier preflight before the
first write, leave the task planned and scope unconfirmed, and create no
execution, validation, gate, readiness, delivery, or Host lifecycle fact.

#### Scenario: Valid single-patch plan
- **WHEN** the root controller applies a valid version-1 delivery plan to an initialized active cycle
- **THEN** the complete linked graph commits once with generated IDs, the task remains planned, scope remains unconfirmed, and execution, validation, gate, and delivery counts remain zero

#### Scenario: Final relation is invalid
- **WHEN** any generated relation, target definition, or qualification fails validation after earlier plan fields have been parsed
- **THEN** the entire transaction rolls back with no graph row, audit event, project revision, or partial ID left behind

#### Scenario: Exact plan replay
- **WHEN** the same logical plan is applied again in the same active cycle
- **THEN** Kafa returns an exact no-op and leaves revisions, timestamps, event counts, and projection bytes unchanged

#### Scenario: Conflicting replay
- **WHEN** a plan reuses a generated ID with different semantic content
- **THEN** Kafa fails before mutation and requires a new plan ID rather than updating a subset of existing facts

#### Scenario: Dry-run
- **WHEN** the controller requests delivery-plan dry-run and JSON output
- **THEN** Kafa returns the generated IDs, validations, and planned mutations as one JSON object without initializing a project, writing SQLite, publishing projections, or appending events

#### Scenario: Closed cycle
- **WHEN** a plan is applied while the current cycle is delivered or archived
- **THEN** Kafa rejects it before mutation and directs the controller to start a new cycle

### Requirement: Explicit verified-patch result

Kafa SHALL expose a verified-patch convenience action that resolves the current
plan-generated acceptance, qualification, and target and reuses the existing
controller verification transaction. Its result SHALL be derived only from the
persisted immutable execution and validation and SHALL explicitly report the
current task, quality-gate, and delivery status. It MUST NOT create a separate
receipt authority or advance task, review, readiness, delivery, or Host state.

#### Scenario: Current verified patch
- **WHEN** the target reports a positive valid result against the current candidate and its qualification is current
- **THEN** Kafa records one immutable execution and validation and returns their IDs, target digest, candidate, task status, `gate_status=not-run`, and `delivery_status=not-run`

#### Scenario: Candidate or mapping is stale
- **WHEN** candidate source, acceptance revision, target definition, or qualification changes before verification commits
- **THEN** verified-patch fails closed and returns no passing verification result

#### Scenario: Result generation is read-only beyond verification
- **WHEN** Kafa builds the verified-patch envelope after a passing verification
- **THEN** task status, cycle phase, gate rows, delivery rows, and Native Host lifecycle remain unchanged

#### Scenario: Cancelled task has verification
- **WHEN** a verified-patch execution exists but the only acceptance-linked task is cancelled
- **THEN** delivery remains blocked as missing accepted-task coverage

### Requirement: Concise and complete operator output

Human-readable `status`, `doctor`, and `quickstart status` SHALL render one
shared report concisely by default: current operator state, the highest-priority
blocker or none, and one primary legal next action or none. `--verbose` SHALL
retain the complete human detail and `--json` SHALL emit the complete structured
report as exactly one valid JSON object. Internal phase and full checklists MUST
NOT appear in the default card unless they are the top actionable blocker.

#### Scenario: Multiple blockers
- **WHEN** the current project has several delivery blockers
- **THEN** default output shows only the first canonical blocker and one legal action while verbose and JSON output retain every blocker and detail

#### Scenario: Healthy project
- **WHEN** doctor finds no issue
- **THEN** default output reports a healthy state with blocker and next action set to none

#### Scenario: JSON failure
- **WHEN** a JSON-mode status or doctor operation fails before opening valid state
- **THEN** stdout remains one parseable error object and human diagnostics do not corrupt it

#### Scenario: Recovery is required
- **WHEN** a migration recovery sentinel exists
- **THEN** concise output selects recovery as the top blocker and preserves the existing do-not-remove guidance in verbose and JSON details without recommending init

### Requirement: Derived authoritative delivery narrative

Kafa SHALL derive authoritative delivery narration at read and projection time
from current or historical structured requirement, acceptance, task,
qualification, execution, validation, failure-mode, finding, gate, trust, cycle,
and candidate facts. Delivery recording SHALL populate its acceptance relation
from the complete active acceptance set proven by the prerequisite evaluator.
Legacy prose inputs SHALL remain auditable only as clearly labelled
supplemental notes and MUST NOT add, remove, or override authoritative facts.

#### Scenario: Scope-only delivery recording
- **WHEN** all delivery prerequisites pass and the controller records delivery with scope and handoff but no repeated acceptance or validation prose
- **THEN** Kafa links every active proven acceptance and renders the complete authoritative validation, coverage, gate, trust, cycle, and candidate facts

#### Scenario: Contradictory prose
- **WHEN** legacy validation, QA, quality-gate, or acceptance prose contradicts persisted structured facts
- **THEN** the derived authoritative section remains unchanged and the prose appears only under a supplemental label

#### Scenario: Judgment-only validation
- **WHEN** a validation judgment has no eligible immutable execution
- **THEN** derived narrative does not describe it as execution-backed passing evidence

#### Scenario: Changed files are not derivable
- **WHEN** a cycle has no valid Git base reference or uses content identity without a comparable base
- **THEN** authoritative changed-file narration says unknown or not derivable and does not fabricate an empty change set

#### Scenario: Projection is rebuilt
- **WHEN** identical persisted facts are projected twice
- **THEN** authoritative and supplemental narrative bytes are identical and historical narrative remains bound to its historical cycle

#### Scenario: Recorded decision contradicts derived trust
- **WHEN** a delivered row's decision status no longer matches the canonical current or historical trust decision
- **THEN** delivery consistency and projection trust fail closed with a decision/trust mismatch instead of upgrading accepted risk to ordinary reviewed delivery

#### Scenario: Delivery row lacks cycle-unique corroboration
- **WHEN** a delivered cycle contains more than one delivery row or a delivery ID has no unique immutable delivery-recorded event
- **THEN** validation blocks and every affected narrative reports human review required rather than inheriting trust from another row in the cycle

#### Scenario: Existing schema remains active
- **WHEN** the derived narrative behavior is installed or used by an existing schema-31 project
- **THEN** no table, column, migration, or fabricated authority fact is added and schema 31 remains exactly 30 product tables

### Requirement: Unified project entrypoint and distribution inventory

Ordinary business-project operations SHALL be reachable through `kafa project
...` without requiring a plugin-internal Python path. One versioned distribution
manifest SHALL be the inventory authority for retained Skills, Hooks and hook
events, templates, schemas, core files, scripts, and public runtime domains.
Source validation, installed-cache validation, structure checks, evaluators,
and install tests MUST read the manifest belonging to the plugin authority they
inspect rather than maintain independent hard-coded inventories.

#### Scenario: Ordinary runtime command
- **WHEN** an installed user invokes a supported project runtime domain through `kafa project`
- **THEN** Kafa delegates to the installed local runtime with the same arguments and exit result without requiring a source checkout path

#### Scenario: Inventory changes in one place
- **WHEN** an approved distribution item is changed in the manifest and the matching artifact is updated
- **THEN** every structure, source, cache, evaluation, and install consumer observes the same inventory without editing a second list

#### Scenario: Installed plugin has extra surface
- **WHEN** an installed plugin contains an undeclared Skill, Hook, template, schema, core file, script, or public runtime domain
- **THEN** validation fails closed and reports the inventory difference

#### Scenario: Runtime stays local-only
- **WHEN** any project command is delegated through the unified entrypoint
- **THEN** it uses only the installed local runtime and does not introduce a Connector, token, remote API, or Host worker

### Requirement: Advanced modes are trigger-selected

The default single-producer workflow SHALL use only goal, acceptance, allowed
files, exact test, and escalation trigger in its delegation packet. The full
delegation matrix, harness audit, retrospective, live-host compatibility, and
release rehearsal SHALL be selected only when their closed workflow triggers
apply or the user explicitly requests them. Trigger selection MUST NOT remove
or relabel any required verification result.

#### Scenario: Small single-producer patch
- **WHEN** one bounded producer has exclusive files and an exact test
- **THEN** the default workflow does not load or require the full parallel delegation matrix, audit, retrospective, live-host, or release-rehearsal material

#### Scenario: Parallel shared-file work
- **WHEN** producers fan out in parallel or share integration files
- **THEN** the full delegation and root-integration obligations become active before work is delegated

#### Scenario: Runtime or schema change
- **WHEN** a change affects schema, migration, trust, delivery gates, runtime ownership, or release surfaces
- **THEN** the corresponding deep review, audit, and evidence triggers remain mandatory

#### Scenario: Advanced evidence is unavailable
- **WHEN** a triggered advanced check is blocked, skipped, not run, or unavailable
- **THEN** Kafa reports that exact state and does not substitute a fixture or omit the obligation

### Requirement: Change-scoped release evidence pressure

Kafa SHALL classify release-candidate changes using a closed conservative local
scope contract. Real Native Codex compatibility SHALL be blocking for Host
integration, packaging, release-tooling, and Native-evaluator changes and MAY be
advisory or explicitly manual for other scopes. Unknown paths MUST select the
blocking class. Selected unavailable evidence MUST remain blocked or not-run,
never pass.

#### Scenario: Host integration change
- **WHEN** a release candidate changes Native Host integration or evaluator code
- **THEN** the real Native single and parallel profiles are required blocking evidence

#### Scenario: Ordinary documentation projection change
- **WHEN** a release candidate changes only a generated workflow documentation view and its contract remains valid
- **THEN** the live profile may be advisory/manual while deterministic documentation and regression gates remain required

#### Scenario: Unknown changed path
- **WHEN** the classifier encounters a path outside its declared categories
- **THEN** it chooses the stricter blocking live-evidence class

#### Scenario: Fixture profile passes
- **WHEN** a blocking real Native profile is unavailable but fixture or stability profiles pass
- **THEN** the release evidence remains blocked or not-run and is not relabelled pass

### Requirement: Stable evidence summary and compact absent metrics

Kafa SHALL keep a stable digest-bound evidence summary in the main review
surface and MAY store volatile detailed Native/rehearsal proof as explicitly
generated local or CI artifacts. A summary MUST identify the evidence state and
artifact digest and MUST NOT replace a required unavailable detail with fixture
evidence. When no bounded field-observation window exists, outcome reporting
SHALL emit one explicit absent-field-metrics sentinel instead of expanding
multiple null or not-run field metrics.

#### Scenario: Detailed proof is generated
- **WHEN** a Native or rehearsal evidence run completes
- **THEN** the stable summary records its exact state, source identity, artifact location or retention class, and digest without requiring the full volatile bundle in the primary diff

#### Scenario: Required detail is missing
- **WHEN** a stable summary refers to missing, stale, or digest-mismatched required proof
- **THEN** evidence validation fails and does not infer success from the summary

#### Scenario: No field observation window
- **WHEN** outcome reporting finds no bounded field observations
- **THEN** it emits `field_metrics_status=not-observed` once and makes no zero, pass, or improvement claim for individual field metrics

#### Scenario: Field observations exist
- **WHEN** a valid bounded field window contains observations
- **THEN** every applicable metric retains its numerator, denominator, window, and missing-data semantics and the absent sentinel is not used
