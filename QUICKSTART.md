# Quick Start

## Install Locally

From the repository root:

```bash
python3 -m pip install -e .
kafa plugin install --repo .
kafa doctor --repo .
```

This creates the repo-scoped Codex marketplace entry at `.agents/plugins/marketplace.json` without copying plugin files. Restart Codex, open the plugin directory, choose the `kafa-local` marketplace, and install `codex-project-harness`.

`kafa doctor --repo .` also verifies the control-plane contract so installation does not drift from the Skill/Plugin/Hooks/Host/Kernel/Connector/Eval boundary model.

Use `kafa doctor --repo <kafa-repo>` for the Kafa source checkout. For an ordinary business project, use project doctor instead:

```bash
kafa project doctor --repo /path/to/business-project
```

Inside a business project, initialize and inspect the guided checklist:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/scripts/harness.py --root . init
python3 /path/to/kafa/plugins/codex-project-harness/scripts/harness.py --root . quickstart status
```

For a tiny project that already has a real test command, the minimal loop can produce a full local delivery cycle:

```bash
python3 /path/to/kafa/plugins/codex-project-harness/scripts/harness.py --root . quickstart minimal \
  --id SMOKE \
  --goal "Keep the current behavior working" \
  --acceptance "The existing test command passes" \
  --task "Verify the current behavior" \
  --test-command "python3 -B -m unittest discover -s . -p 'test_*.py'" \
  --execute
```

`quickstart minimal --execute` still uses controller-local command evidence and the normal delivery gate. A `validation record` without evidence is audit-only and will not satisfy delivery readiness.

For long-running projects, check the current Delivery Cycle before recording new delivery evidence:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . cycle status --json
```

Before splitting work into Host Codex or Spark-capable subagent tasks, ask the runtime for route advice:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch plan --scope "next implementation slice"
python3 plugins/codex-project-harness/scripts/harness.py --root . dispatch route-advice --run-id <run-id> --json
```

Only tasks reported as `host-codex-spark` should be considered for Spark execution, and Spark still requires explicit `HARNESS_CODEX_MODEL_POLICY=spark-deterministic` plus Host Codex Provider start. All other tasks remain with the main model, manual review, or default Host Codex.

For real connector writes, bind the project to existing external targets first. This does not create external workspaces, projects, channels, files, or repos:

```bash
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile set \
  --project-key my-project \
  --github-repo owner/repo \
  --notion-parent PAGE_ID \
  --slack-channel C123456
python3 plugins/codex-project-harness/scripts/harness.py --root . connector profile status --json
```

For user-scope installation:

```bash
python3 -m pip install -e .
kafa plugin install --scope user --repo .
codex plugin add codex-project-harness@kafa-local
kafa doctor --scope user --repo .
```

`kafa doctor --scope user` reports an error until Codex lists the plugin as installed and enabled and its cache matches the managed copy; a copied plugin directory or marketplace entry alone is not enough. Doctor performs static checks and does not execute untrusted checkout or hook code.

Use `kafa plugin upgrade --repo .` after pulling a new release and `kafa plugin uninstall --repo .` to remove only the marketplace entry. See `INSTALL.md` for Windows, migration, uninstall, and troubleshooting details.

## Full Project

Say:

```text
我要开发一个微信小程序，用于管理亲友关系、生日提醒和关系图谱。
```

The `project-harness` skill should:

1. inspect the workspace,
2. bootstrap git, harness files, and useful GitHub/Linear/Notion/Figma/Slack mappings,
3. update runtime phase and task state through `project-runtime`,
4. clarify requirements,
5. ask for baseline confirmation when needed,
6. initialize `.ai-team/` and `docs/harness/`,
7. create acceptance criteria and failure-mode IDs for risky behavior,
8. generate the project team architecture,
9. dispatch implementation and review work,
10. record validation and an independent quality gate,
11. prepare code delivery evidence,
12. run a retrospective.

It should decide which collaboration tools are useful from context. Local harness files are the fallback. It should stop at verified code handoff. Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation are outside this harness.

See `examples/full-project-flow.md` for a full request-to-delivery walkthrough.

## Narrow Tasks

Use smaller skills when you do not need the whole project operating system:

```text
$project-bootstrap
检查并初始化当前项目的 git、.ai-team、docs/harness，以及需要使用的 GitHub/Linear/Notion/Figma/Slack 映射。
```

```text
$project-runtime
更新项目阶段、任务、决策、QA 和交付记录，并校验本地 harness 状态。
```

```text
$requirement-baseline
帮我把这个需求问清楚，形成可验收的需求基线，并列出关键失败模式。
```

```text
$minimal-safe-change
用最小改动完成这个字段兼容，不要做无关重构。
```

```text
$independent-quality-gate
独立验收当前实现，重点检查 API 返回、前端类型和数据库字段是否一致。
```

```text
$delivery-readiness
整理本次代码交付证据，包括验收映射、失败模式覆盖、变更文件、测试结果、质量门结论和遗留风险。
```
