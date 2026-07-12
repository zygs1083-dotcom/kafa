insert into project
(id, project_id, schema_version, runtime_version, phase, current_cycle_id,
 status, scope_status, current_owner, revision, updated_at)
values (1, 'project-1', 27, '4.14.3', 'qa', 'CYCLE-current',
        'active', 'confirmed', 'project-manager', 7, 'now');

insert into delivery_cycles
values ('CYCLE-current', 'Current', 'Ship safely', 'active', 'qa', 'main',
        '__CANDIDATE__', 'now', '', 'now', 'now');
insert into requirements
(id, cycle_id, kind, body, status, updated_at)
values ('R1', 'CYCLE-current', 'functional', 'Preserve requirement', 'active', 'now');
insert into acceptance
(id, cycle_id, criterion, status)
values ('AC1', 'CYCLE-current', 'Preserve acceptance', 'active');
insert into failure_modes
(id, cycle_id, feature, scenario, trigger, expected_behavior, risk, status)
values ('FM1', 'CYCLE-current', 'Delivery', 'Failure', 'trigger', 'safe', 'high', 'identified');
insert into tasks
(id, cycle_id, task, owner, status, evidence, accepted_by, updated_at)
values ('T0', 'CYCLE-current', 'Dependency', 'developer', 'accepted',
        'verified dependency', 'root-controller', 'now');
insert into tasks
(id, cycle_id, task, owner, status, updated_at)
values ('T1', 'CYCLE-current', 'Preserve task', 'developer', 'review', 'now');
insert into requirement_acceptance values ('R1', 'AC1');
insert into failure_mode_acceptance values ('FM1', 'AC1');
insert into task_acceptance values ('T1', 'AC1');
insert into task_failure_modes values ('T1', 'FM1');
insert into task_dependencies values ('T1', 'T0');

insert into baselines
values ('B1', 'baseline',
        '{"requirements":[{"id":"R1","body":"Preserve requirement","tool_link":"BASELINE-SECRET"}]}',
        'old-digest', 7, 'controller', 'now');
insert into test_targets
(id, kind, command_template, description, gateable, stack_profile,
 result_format, created_at, updated_at)
values ('UNIT', 'unit', 'python3 -m unittest', 'unit', 1, 'python', 'regex', 'now', 'now');
insert into task_test_targets values ('T1', 'UNIT');
insert into evidence
(id, kind, summary, command, exit_code, stdout_sha256, artifact_path,
 source_tree_hash, target_id, executed_count, executed_count_source,
 result_format, semantic_status, policy_status, verified_by, created_at)
values ('EV1', 'command', 'verified', 'python3 -m unittest', 0,
        '__ARTIFACT_SHA__', '__ARTIFACT_PATH__', '__CANDIDATE__', 'UNIT', 1,
        'parsed', 'regex', 'pass', 'allowed', 'controller-legacy', 'now');
insert into validations
(id, cycle_id, candidate_sha, surface, acceptance_id, target_id, findings,
 result, residual_risk, source_tree_hash, created_at)
values ('V1', 'CYCLE-current', '__CANDIDATE__', 'unit', 'AC1', 'UNIT', '',
        'pass', '', '__CANDIDATE__', 'now');
insert into validation_evidence values ('V1', 'EV1');
insert into validation_failure_modes values ('V1', 'FM1');
insert into tests values ('TEST1', 'unit', 'python3 -m unittest', 'pass', 'EV1', 'now');
insert into validation_tests values ('V1', 'TEST1');
insert into findings
values ('F1', 'unit', 'high', 'resolved', 'historical finding', 'EV1', 'now');
insert into quality_gates
(id, cycle_id, candidate_sha, gate, reviewed_commit, project_revision,
 reviewer_context, result, created_at)
values ('G1', 'CYCLE-current', '__CANDIDATE__', 'independent_qa',
        '__CANDIDATE__', 7, 'fresh', 'pass', 'now');
insert into quality_gate_findings values ('G1', 'F1');
insert into deliveries
(id, cycle_id, candidate_sha, scope, acceptance, created_at)
values ('D1', 'CYCLE-current', '__CANDIDATE__', 'scope', 'AC1', 'now');
insert into delivery_acceptance values ('D1', 'AC1');
insert into decisions values ('D-sentinel', 'keep', 'rollback sentinel', 'now');
insert into invalidations
(id, cycle_id, source_type, source_id, target_type, target_id, reason, created_at)
values ('I1', 'CYCLE-current', 'task', 'T1', 'validation', 'V1', 'superseded', 'now');
insert into migrations (from_version, to_version, applied_at) values (26, 27, 'now');
insert into events
(id, schema_version, type, source, target, payload_json, created_at)
values ('E1', 27, 'requirement_recorded', 'runtime', 'requirement:R1', '{}', 'now');

insert into adapter_actions
(id, tool, mode, artifact, action, payload_json, status, idempotency_key,
 created_at, updated_at)
values ('AA1', 'github', 'connector', 'artifact', 'create',
        '__RETIRED_SECRET__', 'pending', 'idem-1', 'now', 'now');
insert into agent_provider_sessions
(id, run_id, task_id, provider, status, input_json)
values ('PS1', 'RUN1', 'T1', 'host-codex', 'queued', '__RETIRED_SECRET__');
insert into runtime_snapshots
values ('RS1', 'retired', 0, '__RETIRED_SECRET__', 'now');
insert into command_log
values ('REQ1', 'legacy command', 'args-hash', '__RETIRED_SECRET__', 'now');
