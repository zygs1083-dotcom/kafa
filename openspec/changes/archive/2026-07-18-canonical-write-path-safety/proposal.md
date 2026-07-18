## Why

Kafa currently validates candidate source paths aggressively, but several canonical
project-state writes still reach pathname-based filesystem and SQLite APIs directly.
A malicious or accidentally linked project path can therefore redirect a database,
lock, projection, migration backup, or execution artifact outside the anchored project
root despite the local-only trust boundary.

## What Changes

- Introduce one internal, cross-platform, handle-backed project filesystem seam for
  canonical project state and derived artifacts.
- Fail closed on path traversal, unsafe ancestors, symbolic links, junctions, reparse
  points, hard-linked targets, cross-device ancestry, and path identity changes.
- Route file-backed SQLite, operation-lock, migration/recovery, projection, template,
  structured-result, and container-artifact operations through the same safety policy.
- Preserve the existing root symlink alias only by resolving it once into a pinned
  project root; never use descendant `resolve()` as an authorization check.
- Keep schema 30, the public CLI, seven Skills, three Hooks, three templates, local-only
  runtime, and Native Host lifecycle ownership unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `local-delivery-kernel`: canonical project reads and writes become fail-closed against
  path redirection and filesystem identity races on POSIX and Windows.

## Impact

- Adds an internal filesystem safety module and cross-platform tests.
- Changes Store/SQLite open paths, projection publication, migration backup and rollback,
  execution artifact capture, project initialization, and project doctor preflight.
- Adds no runtime network dependency, external Connector, schema table, public lifecycle,
  or third-party Python dependency.
