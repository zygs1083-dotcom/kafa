# Release supply-chain evidence

Kafa release evidence is build-only. It is not loaded by the local delivery
kernel, adds no business-runtime network dependency, and does not make a local
unsigned statement equivalent to a signed attestation.

The machine-readable pins live in `release-tooling.json`. They were checked on
2026-07-21 against primary project documentation and immutable GitHub releases:

- Anchore Syft `1.48.0`, source commit
  `3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6`, with every supported macOS,
  Linux, and Windows archive bound to the official release SHA-256;
- CycloneDX JSON `1.6`, selected explicitly as `cyclonedx-json@1.6` rather
  than following Syft's future default;
- `actions/attest` `v4.2.0`, pinned in workflow source to immutable commit
  `f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6`;
- in-toto Statement v1 and the stable SLSA provenance predicate URI
  `https://slsa.dev/provenance/v1` from the current SLSA 1.2 specification.

Primary references:

- [Syft output formats](https://oss.anchore.com/docs/guides/sbom/formats/)
- [Syft release verification](https://oss.anchore.com/docs/installation/verification/)
- [Syft v1.48.0](https://github.com/anchore/syft/releases/tag/v1.48.0)
- [GitHub artifact attestation usage](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
- [`actions/attest`](https://github.com/actions/attest)
- [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md)
- [SLSA provenance 1.2](https://slsa.dev/spec/v1.2/provenance)

The local rehearsal produces an unsigned in-toto/SLSA integrity statement. It
binds exact source and artifact bytes and can detect later tampering, but it has
no independent signer identity. Only an explicitly authorized GitHub Actions
release run may use `actions/attest` to obtain signed GitHub/Sigstore build and
SBOM attestations. No tag, release, upload, or deployment occurs during local
generation or verification.

## Change-scoped checks and stable summaries

The release workflow resolves its base only from non-draft GitHub Release
metadata whose tag is an ancestor of the candidate. An unpublished intermediate
tag is not a baseline. Missing, malformed, unrelated, or unavailable release
metadata produces an unknown/blocking decision. `kafa.change_scope` then
classifies only the complete path set between two exact immutable Git object
IDs. The closed decision binds base, head, ordered paths, and a framed path-set
digest. Host integration, packaging, release tooling, Native evaluation, and
unknown paths require both real Native profiles. Documentation and
schema/runtime-only scopes may leave real Native evidence advisory, but
structure, unit, isolated-install, documentation, and supply-chain gates remain
mandatory. An unavailable selected profile stays blocked or not-run.

Wheel and sdist identity use one internal `name` / `kind` / `sha256` subject
model. Sidecars, tooling, release metadata, and source inputs are parsed and
hashed from one identity-bound regular-file descriptor snapshot; the artifact
body remains streaming. Existing SHA256SUMS, CycloneDX, in-toto,
isolated-install, rehearsal, and v1 manifest shapes remain compatible adapters;
no checksum, subject, source, tooling, or race check was removed. A rehearsal
summary is created only after the shared complete rehearsal validator accepts
doctor, migration, backup, Hook, discovery, uninstall, cache digest, exact
artifact, and no-external-effect facts.

`kafa.evidence_summary` creates a small sidecar for Native or no-publish
rehearsal detail. It records source, status, binary subjects, change scope,
timing, exact detail digest and byte count, currentness state, and retention.
CI detail is a 30-day artifact and its stable summary is shown in the workflow
review. Local detail remains an explicit opt-in. Current eligibility requires a
clean matching checkout, current source and Git status, the current
path-discovered Native binary, the current matrix, and the exact classifier
decision detail. Historical integrity deliberately makes no currentness claim.
Missing, stale, digest-mismatched, fixture-substituted, or falsely current detail
fails verification; a summary alone is never passing evidence.

Native detail semantics are evaluated only from one private source snapshot.
Current eligibility clones and detaches the clean committed HEAD without shared
object storage; historical integrity descriptor-copies the local validator
roots and rechecks their complete file set. Both paths reject links, reparse
points, and special files. The private source files and parent directories are
made read-only and identity-checked across the isolated child process; cleanup
restores permissions only while every captured path and ancestor still has its
original identity. Current validation also rechecks the source repository HEAD
and cleanliness after the child exits.

The repository-side `native-codex-live-summary.json` and
`native-codex-parallel-summary.json` bind the retained historical detail bytes
and explicitly label their dirty-worktree evidence `historical`. Newly
generated candidate detail is written to CI artifacts or an opt-in local path;
it does not silently overwrite these historical records or become current
without an independently matching clean source.

The committed `delivery-integrity-outcome-benchmark.json` remains a historical
v1 artifact referenced by earlier audits. Newly generated v2 benchmark reports
emit one `field_metrics_status=not-observed` sentinel when there is no bounded
field window. Real project outcome reports still retain all six metrics with
their numerator, denominator, window, and missing-data semantics. Neither form
claims field improvement without observations. The regression report closes its
top-level and source shapes and rederives status, counts, numerator, denominator,
window, and closure rate from the fixed scenario records; an extra self-reported
claim or fabricated zero aggregate is invalid.

These controls do not authorize or perform a tag, release, upload, deployment,
production migration, or user installation replacement.
