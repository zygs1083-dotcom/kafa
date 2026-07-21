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
