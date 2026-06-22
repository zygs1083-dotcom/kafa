# Installation

## Option 1: Install As A Local Plugin

Point Codex at the `plugins/codex-project-harness` plugin directory according to your local Codex plugin workflow.

## Option 2: Copy Skills Directly

Copy every folder under:

```text
plugins/codex-project-harness/skills/
```

into one of your Codex skills directories, for example:

```text
~/.agents/skills/
```

or into a project-local directory:

```text
<repo>/.agents/skills/
```

## Verify

Run:

```bash
python3 plugins/codex-project-harness/scripts/validate_structure.py plugins/codex-project-harness
```

Expected result:

```text
OK: plugin structure is valid
```
