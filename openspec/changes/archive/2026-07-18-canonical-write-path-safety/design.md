## Context

Kafa anchors business-project authority in `.ai-team/state/harness.db` and publishes
derived files beneath `.ai-team/`, `docs/harness/`, and `.codex/agents/`. Candidate
identity rejects unsafe source paths, but canonical runtime paths are still opened with
`Path.open`, `Path.write_*`, `sqlite3.connect`, `shutil.copyfile`, and `os.replace` in
multiple modules. Checking `resolve()` or `is_symlink()` immediately before those calls
does not close the check-to-use race and does not provide Windows reparse-point parity.

The change is security-sensitive but must preserve the schema 30 facts, operation-lock
semantics, migration rollback authority, public CLI/output contracts, in-memory Store,
and stdlib-only packaging. The supported threat model includes a repository prepared
with malicious filesystem objects before Kafa runs and bounded path replacement races
during an operation. A continuously active attacker with the same OS user authority is
outside the guarantee; such repositories require an isolated OS user or container.

## Goals / Non-Goals

**Goals:**

- Give every canonical project path one validation, open, atomic replace, and unlink
  policy on POSIX and Windows.
- Pin the trusted project root and filesystem identity for the complete operation.
- Reject unsafe targets before opening SQLite or mutating DB/projection authority.
- Preserve migration diagnostics and report rollback incomplete when a safe restore
  cannot be proved.
- Keep normal-path and in-memory behavior compatible and within existing size/time
  budgets.

**Non-Goals:**

- Sandboxing arbitrary commands registered with `verify run`.
- Defending against a privileged or continuously racing attacker with the same OS user.
- Following symlinks below the pinned project root, even when they currently point back
  into the project.
- Changing schema 30, public CLI domains, Host ownership, trust policy, or adding a
  third-party dependency.

## Decisions

### 1. One internal `ProjectFS` owns canonical path safety

Add `core/project_fs.py` with an internal `ProjectFS.open(root)` factory. The factory
resolves an optional root symlink alias once, opens/pins the resulting root directory,
records its stable filesystem identity, and exposes only relative-path operations:

```python
class ProjectFS:
    @classmethod
    def open(cls, root: Path) -> "ProjectFS": ...
    def audit(self, paths: Iterable[Path], *, allow_missing: bool = True) -> None: ...
    def read_bytes(self, relative: Path) -> bytes: ...
    def atomic_write(self, relative: Path, data: bytes, *, mode: int = 0o600) -> None: ...
    def create_exclusive(self, relative: Path, data: bytes, *, mode: int = 0o600) -> None: ...
    def open_lock_fd(self, relative: Path, *, mode: int = 0o600) -> int: ...
    def unlink_regular(self, relative: Path, *, missing_ok: bool = False) -> None: ...
    def create_unique_directory(self, parent: Path, prefix: str) -> Path: ...
    def sqlite_path(self, relative: Path, *, access: str, create: bool = False) -> Path: ...
```

The seam is not exported through `core.api`. Callers pass fixed project-relative paths;
arbitrary external source files such as packaged agent-template inputs remain ordinary
read-only files, while their project destinations use `ProjectFS`.

Alternative rejected: scattered `is_symlink()`/`resolve()` checks. They leave races,
hard-link redirection, Windows junctions, and newly added call sites inconsistent.

### 2. Relative paths use a closed lexical and filesystem policy

Reject absolute paths, empty/dot traversal, `..`, Windows drives/UNC/ADS, reserved device
names, and components ending in a dot or space. Every existing ancestor below root must
be a real directory and not a symbolic link or reparse point. Existing file targets must
be regular, non-reparse, and have link count one. POSIX ancestors must remain on the
root device. Missing ancestors may be created one component at a time only after their
parent handle is pinned and rechecked.

Stable failures use:

```text
unsafe-project-path: <relative>: <reason>
```

Reasons are closed to `invalid-relative-path`, `unsafe-ancestor`, `unsafe-target`,
`hard-linked-target`, `cross-device-ancestor`, `path-identity-changed`, and
`platform-safety-unavailable`.

Alternative rejected: allowing linked targets that resolve inside root. The link itself
is mutable authority and makes safe replacement/rollback unverifiable.

### 3. POSIX operations are descriptor-relative; Windows operations are handle-pinned

On POSIX, `ProjectFS` keeps a root `dir_fd`, walks with `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`,
creates temporary files with `O_CREAT|O_EXCL|O_NOFOLLOW`, and performs replace/unlink
relative to pinned parent descriptors. It fsyncs written files and affected directories
and rechecks `(st_dev, st_ino, mode, nlink)` before publication.

On Windows, the backend uses stdlib `ctypes` bindings to `CreateFileW` with
`FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_BACKUP_SEMANTICS`, holds ancestor handles without
`FILE_SHARE_DELETE`, rejects reparse tags, compares volume/file IDs and link count, and
uses create-new plus write-through replacement. If required APIs or identity fields are
unavailable, the operation fails with `platform-safety-unavailable`; it does not fall
back to pathname-only mutation.

Alternative rejected: POSIX-only hardening with skipped Windows tests. Kafa publishes a
three-platform contract, so unsupported safety must fail closed rather than silently
weaken it.

### 4. SQLite receives a verified pathname plus identity rechecks

Python's `sqlite3` cannot connect to an already-open file descriptor. `ProjectFS`
therefore safely precreates or validates the main DB and its `-wal`, `-shm`, and
`-journal` family, returns a pinned absolute pathname for SQLite URI
`mode=ro|rw` (never implicit create), and verifies identity immediately after connect,
after journal-mode setup, and before close. New DB creation is a separate exclusive
safe operation before `mode=rw` connect.

The operation-lock registry key is `(root filesystem identity, fixed lock relative
path)`, not `realpath(lock_path)`. Lock and sentinel reads use the same `ProjectFS`
instance held for the complete DB operation. `InMemoryStore` remains unchanged.

Alternative rejected: a custom SQLite VFS, which would add a native dependency and a
larger correctness surface than this threat model requires.

### 5. Writers preflight the whole authority set and publish atomically

Init audits DB family, lock/sentinel, `.gitignore`, all 13 projections, retired
projection, and three template destinations before its first write. Normal mutations
audit every projection they can publish before the DB transaction commits. Projection
writes use safe temporary files and atomic replacement; retired projection deletion
uses `unlink_regular` and never follows a link.

Execution stdout, structured-result capture, and container result artifacts use safe
read/write/copy methods. An unsafe structured-result path is rejected before its bytes
can become execution evidence. Arbitrary verification commands remain unsandboxed unless
the existing container policy is selected.

Alternative rejected: checking each view only when its renderer runs, because a later
unsafe target could leave DB facts committed with only a partial projection set.

### 6. Migration keeps path safety inside rollback authority

Migration creates the diagnostic sentinel exclusively through `ProjectFS`, acquires the
same safe operation lock, then audits active DB family, staging, backup, manifest,
projection backup, failed DB, quarantine, and restore paths. These handles/identities
remain authoritative through activation or verified rollback.

An unsafe path before activation leaves the active DB unchanged. After activation, DB
and projection restore must both complete through safe atomic operations. If any target
becomes unsafe, the manifest and sentinel record `rollback-incomplete`, retain original
and restore errors plus diagnostic paths, and never report schema 30 success or complete
rollback.

### 7. Doctor is a no-open safety preflight

Runtime doctor collects canonical path issues before SQLite. `kafa project doctor`
delegates to that hardened runtime preflight instead of opening the lock and DB through
an independent implementation, while preserving its current JSON/report shape and
`migration-in-progress` guidance.

## Risks / Trade-offs

- **Windows API complexity** → isolate the backend, test real junction/hardlink behavior
  on Windows CI, and test deterministic error mapping on every platform.
- **SQLite pathname gap** → exclusive safe precreation plus three identity checkpoints;
  document that same-user continuous replacement is outside the guarantee.
- **More syscalls on each mutation** → audit only the fixed bounded canonical inventory;
  retain mutation ≤0.050s, DB ≤320 KiB, and plugin ≤1 MiB budgets.
- **Existing linked project state stops working** → fail with the exact relative path and
  reason; do not auto-copy, unlink, or rewrite user-controlled external targets.
- **Rollback encounters newly unsafe paths** → retain sentinel/manifest and report
  `rollback-incomplete`; never trade recoverability for apparent availability.

## Migration Plan

1. Add deterministic red tests for each path class and prove current production fails.
2. Add `ProjectFS` and backend unit tests without routing production callers.
3. Route Store/SQLite and doctor, then projections/init, then execution artifacts, then
   migration/recovery; run targeted checkpoints after each slice.
4. Run complete local regression, benchmark, isolated artifacts, and exact-head CI on
   Ubuntu, macOS, and Windows.
5. Archive this change into the canonical spec only after independent filesystem and
   migration/recovery QA has no open Critical/High/Medium finding.

Rollback is an ordinary code revert because schema and stored facts do not change.
Projects rejected for unsafe paths remain byte-for-byte untouched.

## Open Questions

None. The active same-user attacker boundary and fail-closed Windows fallback are
explicitly accepted design constraints.
