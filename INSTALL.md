# Installation

## Install As A Local Plugin

Point Codex at the `plugins/codex-project-harness` plugin directory according to your local Codex plugin workflow.

## Important

Install the whole plugin directory, not individual skill folders. The skills share plugin-level `scripts/`, `references/`, and `templates/` resources. Copying only `skills/` will break those shared resource paths.

## Verify

Run:

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
```

Expected result:

```text
OK: plugin structure is valid
```
