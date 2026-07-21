"""Single ordered migration ledger for the new workflow subsystem."""

from __future__ import annotations

import sqlite3


_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "workflow definitions and immutable versions",
        """
        CREATE TABLE workflow_definitions (
            workflow_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            CHECK (workflow_id LIKE 'workflow:%')
        );

        CREATE TABLE workflow_versions (
            workflow_id TEXT NOT NULL REFERENCES workflow_definitions(workflow_id),
            version INTEGER NOT NULL CHECK (version >= 1),
            definition_hash TEXT NOT NULL,
            dsl_version TEXT NOT NULL,
            ir_version TEXT NOT NULL,
            compiler_version TEXT NOT NULL,
            canonical_ir_json TEXT NOT NULL,
            source_format TEXT NOT NULL CHECK (source_format IN ('yaml', 'json', 'ui')),
            source_text TEXT,
            catalog_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            PRIMARY KEY (workflow_id, version),
            UNIQUE (workflow_id, definition_hash)
        );

        CREATE TRIGGER workflow_versions_no_update
        BEFORE UPDATE ON workflow_versions
        BEGIN
            SELECT RAISE(ABORT, 'published WorkflowVersion is immutable');
        END;

        CREATE TRIGGER workflow_versions_no_delete
        BEFORE DELETE ON workflow_versions
        BEGIN
            SELECT RAISE(ABORT, 'published WorkflowVersion is immutable');
        END;
        """,
    ),
    (
        2,
        "deterministic runtime projections and event store",
        """
        CREATE TABLE workflow_runs (
            run_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            workflow_version INTEGER NOT NULL,
            definition_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'created', 'running', 'waiting', 'budget_exhausted',
                'waiting_for_budget', 'succeeded', 'failed', 'cancelled'
            )),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            correlation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (workflow_id, workflow_version)
                REFERENCES workflow_versions(workflow_id, version),
            CHECK (run_id LIKE 'run:%'),
            CHECK (correlation_id LIKE 'run:%')
        );

        CREATE TABLE run_events (
            global_position INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            aggregate_id TEXT NOT NULL,
            aggregate_sequence INTEGER NOT NULL CHECK (aggregate_sequence >= 1),
            event_type TEXT NOT NULL,
            event_version INTEGER NOT NULL CHECK (event_version >= 1),
            correlation_id TEXT NOT NULL,
            causation_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (aggregate_id, aggregate_sequence),
            CHECK (event_id LIKE 'event:%')
        );

        CREATE TABLE execution_plans (
            plan_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
            workflow_id TEXT NOT NULL,
            workflow_version INTEGER NOT NULL,
            plan_schema_version TEXT NOT NULL,
            canonical_plan_json TEXT NOT NULL,
            definition_hash TEXT NOT NULL,
            created_event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, plan_version),
            FOREIGN KEY (workflow_id, workflow_version)
                REFERENCES workflow_versions(workflow_id, version),
            CHECK (plan_id LIKE 'plan:%'),
            CHECK (created_event_id LIKE 'event:%')
        );

        CREATE TABLE node_runs (
            node_run_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            source_plan_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'ready', 'running', 'waiting', 'succeeded',
                'failed', 'cancelled', 'skipped'
            )),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (run_id, source_plan_version)
                REFERENCES execution_plans(run_id, plan_version),
            CHECK (node_run_id LIKE 'node_run:%')
        );

        CREATE TABLE node_attempts (
            attempt_id TEXT PRIMARY KEY,
            -- run_id is deliberately normalized through node_runs. Run-scoped
            -- queries/deletion must join node_attempts -> node_runs; do not add
            -- a duplicate run_id column without a new reviewed migration.
            node_run_id TEXT NOT NULL REFERENCES node_runs(node_run_id),
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'created', 'leased', 'running', 'succeeded', 'failed',
                'timed_out', 'cancelled', 'lost', 'unknown_external_result'
            )),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (node_run_id, attempt_number),
            CHECK (attempt_id LIKE 'attempt:%')
        );

        CREATE TABLE branch_tokens (
            token_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            source_node_run_id TEXT REFERENCES node_runs(node_run_id),
            status TEXT NOT NULL CHECK (status IN (
                'active', 'completed', 'failed', 'cancelled', 'not_selected'
            )),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            scope_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (token_id LIKE 'branch_token:%')
        );

        CREATE TABLE command_receipts (
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            aggregate_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            command_fingerprint TEXT NOT NULL,
            command_id TEXT NOT NULL,
            expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
            result_event_ids_json TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            PRIMARY KEY (aggregate_id, idempotency_key),
            UNIQUE (command_id),
            CHECK (command_id LIKE 'command:%')
        );

        CREATE TABLE run_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            snapshot_sequence INTEGER NOT NULL CHECK (snapshot_sequence >= 1),
            snapshot_schema_version TEXT NOT NULL,
            reducer_version TEXT NOT NULL,
            last_global_position INTEGER NOT NULL CHECK (last_global_position >= 0),
            last_run_event_sequence INTEGER NOT NULL CHECK (last_run_event_sequence >= 0),
            state_json TEXT NOT NULL,
            checksum TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, snapshot_sequence),
            CHECK (snapshot_id LIKE 'snapshot:%')
        );

        CREATE INDEX run_events_by_run_position
            ON run_events(run_id, global_position);
        CREATE INDEX run_events_by_aggregate
            ON run_events(aggregate_id, aggregate_sequence);
        CREATE INDEX run_events_by_correlation
            ON run_events(correlation_id, global_position);
        CREATE INDEX run_events_by_causation
            ON run_events(causation_id);
        CREATE INDEX node_runs_by_run_status
            ON node_runs(run_id, status);
        CREATE INDEX node_attempts_by_node
            ON node_attempts(node_run_id, attempt_number);
        CREATE INDEX branch_tokens_by_run_status
            ON branch_tokens(run_id, status);
        CREATE INDEX run_snapshots_by_run_sequence
            ON run_snapshots(run_id, snapshot_sequence DESC);

        CREATE TRIGGER run_events_no_update
        BEFORE UPDATE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'RunEvent is immutable');
        END;

        CREATE TRIGGER run_events_no_delete
        BEFORE DELETE ON run_events
        BEGIN
            SELECT RAISE(ABORT, 'RunEvent is immutable');
        END;

        CREATE TRIGGER execution_plans_no_update
        BEFORE UPDATE ON execution_plans
        BEGIN
            SELECT RAISE(ABORT, 'ExecutionPlanVersion is immutable');
        END;

        CREATE TRIGGER execution_plans_no_delete
        BEFORE DELETE ON execution_plans
        BEGIN
            SELECT RAISE(ABORT, 'ExecutionPlanVersion is immutable');
        END;

        CREATE TRIGGER run_snapshots_no_update
        BEFORE UPDATE ON run_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'RunSnapshot is immutable');
        END;
        """,
    ),
    (
        3,
        "durable jobs leases and timers",
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            node_run_id TEXT NOT NULL REFERENCES node_runs(node_run_id),
            current_attempt_id TEXT REFERENCES node_attempts(attempt_id),
            job_kind TEXT NOT NULL,
            execution_safety TEXT NOT NULL CHECK (execution_safety IN (
                'replay_safe', 'unknown_on_lease_loss'
            )),
            status TEXT NOT NULL CHECK (status IN (
                'ready', 'leased', 'running', 'retry_wait',
                'completed', 'failed', 'cancelled'
            )),
            priority INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (delivery_count >= 0),
            max_delivery_attempts INTEGER NOT NULL CHECK (max_delivery_attempts >= 1),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (delivery_count <= max_delivery_attempts),
            CHECK (job_id LIKE 'job:%')
        );

        CREATE TABLE job_leases (
            lease_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            attempt_id TEXT NOT NULL REFERENCES node_attempts(attempt_id),
            worker_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            token_hash_version TEXT NOT NULL,
            fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
            status TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired')),
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            released_at TEXT,
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            renewal_revision INTEGER NOT NULL DEFAULT 0 CHECK (renewal_revision >= 0),
            UNIQUE (job_id, fencing_token),
            CHECK (lease_id LIKE 'lease:%'),
            CHECK (
                (status = 'active' AND released_at IS NULL) OR
                (status IN ('released', 'expired') AND released_at IS NOT NULL)
            )
        );

        CREATE TABLE durable_timers (
            timer_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            purpose TEXT NOT NULL CHECK (purpose IN (
                'job_backoff', 'node_timeout', 'lease_recovery',
                'join_deadline', 'planner_timeout', 'human_reminder',
                'human_escalation', 'run_deadline'
            )),
            dedupe_key TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            payload_schema_version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'scheduled', 'leased', 'fired', 'cancelled'
            )),
            due_at TEXT NOT NULL,
            fired_at TEXT,
            lease_owner TEXT,
            lease_token_hash TEXT,
            lease_fencing_token INTEGER NOT NULL DEFAULT 0
                CHECK (lease_fencing_token >= 0),
            lease_expires_at TEXT,
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, purpose, dedupe_key),
            CHECK (timer_id LIKE 'timer:%'),
            CHECK (
                (status = 'leased' AND lease_owner IS NOT NULL
                    AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR
                (status != 'leased' AND lease_owner IS NULL
                    AND lease_token_hash IS NULL AND lease_expires_at IS NULL)
            ),
            CHECK (
                (status = 'fired' AND fired_at IS NOT NULL) OR
                (status != 'fired' AND fired_at IS NULL)
            )
        );

        CREATE UNIQUE INDEX jobs_one_active_execution_per_node
            ON jobs(node_run_id, job_kind)
            WHERE status IN ('ready', 'leased', 'running', 'retry_wait');
        CREATE INDEX jobs_claim_order
            ON jobs(status, priority DESC, available_at, created_at, job_id);
        CREATE INDEX jobs_by_run_status
            ON jobs(run_id, status, job_id);
        CREATE UNIQUE INDEX job_leases_one_active_per_job
            ON job_leases(job_id) WHERE status = 'active';
        CREATE INDEX job_leases_expiry
            ON job_leases(status, expires_at, lease_id);
        CREATE INDEX durable_timers_due
            ON durable_timers(status, due_at, created_at, timer_id);
        CREATE INDEX durable_timers_by_run_status
            ON durable_timers(run_id, status, timer_id);
        """,
    ),
    (
        4,
        "immutable values artifacts and lineage",
        """
        CREATE TABLE "values" (
            value_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            owner_kind TEXT NOT NULL CHECK (owner_kind IN (
                'run_input', 'node_input', 'attempt_output'
            )),
            owner_id TEXT NOT NULL,
            port_id TEXT NOT NULL,
            schema_id TEXT NOT NULL,
            data_json TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 262144),
            created_event_id TEXT NOT NULL REFERENCES run_events(event_id),
            created_at TEXT NOT NULL,
            UNIQUE (owner_kind, owner_id, port_id),
            CHECK (value_id LIKE 'value:%')
        );

        CREATE TABLE value_links (
            link_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            source_value_id TEXT NOT NULL REFERENCES "values"(value_id),
            target_value_id TEXT NOT NULL REFERENCES "values"(value_id),
            link_type TEXT NOT NULL CHECK (link_type IN ('mapped_from', 'consumed_by')),
            mapping_hash TEXT,
            created_event_id TEXT NOT NULL REFERENCES run_events(event_id),
            created_at TEXT NOT NULL,
            UNIQUE (source_value_id, target_value_id, link_type),
            CHECK (link_id LIKE 'value_link:%'),
            CHECK (source_value_id != target_value_id),
            CHECK (
                (link_type = 'mapped_from' AND mapping_hash IS NOT NULL) OR
                (link_type = 'consumed_by' AND mapping_hash IS NULL)
            )
        );

        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            workflow_id TEXT NOT NULL,
            producer_type TEXT NOT NULL CHECK (producer_type IN ('attempt', 'run_ingress')),
            producer_id TEXT NOT NULL,
            producer_node_run_id TEXT,
            output_port_id TEXT NOT NULL,
            schema_id TEXT NOT NULL,
            content_type TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 1073741824),
            blob_key TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility IN ('node', 'run', 'subflow', 'workflow')),
            scope_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('staged', 'committed', 'abandoned')),
            created_at TEXT NOT NULL,
            committed_at TEXT,
            created_event_id TEXT REFERENCES run_events(event_id),
            UNIQUE (producer_type, producer_id, output_port_id, artifact_id),
            CHECK (artifact_id LIKE 'artifact:%'),
            CHECK (blob_key = checksum),
            CHECK (
                (status = 'committed' AND committed_at IS NOT NULL AND created_event_id IS NOT NULL) OR
                (status != 'committed' AND committed_at IS NULL AND created_event_id IS NULL)
            ),
            CHECK (
                (producer_type = 'attempt' AND producer_node_run_id IS NOT NULL) OR
                (producer_type = 'run_ingress' AND producer_node_run_id IS NULL)
            )
        );

        CREATE TABLE artifact_links (
            link_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            link_type TEXT NOT NULL CHECK (link_type IN ('producer', 'consumer', 'derived_from')),
            target_id TEXT NOT NULL,
            created_event_id TEXT NOT NULL REFERENCES run_events(event_id),
            created_at TEXT NOT NULL,
            UNIQUE (artifact_id, link_type, target_id),
            CHECK (link_id LIKE 'artifact_link:%'),
            CHECK (artifact_id != target_id)
        );

        CREATE INDEX values_by_run_owner ON "values"(run_id, owner_kind, owner_id, port_id);
        CREATE INDEX value_links_reverse ON value_links(target_value_id, link_type, link_id);
        CREATE INDEX artifacts_by_run_status ON artifacts(run_id, status, artifact_id);
        CREATE INDEX artifacts_staging_gc ON artifacts(status, created_at, artifact_id);
        CREATE INDEX artifacts_by_workflow_visibility ON artifacts(workflow_id, visibility, artifact_id);
        CREATE UNIQUE INDEX artifact_one_producer
            ON artifact_links(artifact_id) WHERE link_type = 'producer';
        CREATE INDEX artifact_links_target ON artifact_links(target_id, link_type, link_id);

        CREATE TRIGGER values_no_update
        BEFORE UPDATE ON "values" BEGIN
            SELECT RAISE(ABORT, 'Value is immutable');
        END;
        CREATE TRIGGER values_no_delete
        BEFORE DELETE ON "values" BEGIN
            SELECT RAISE(ABORT, 'Value is immutable');
        END;
        CREATE TRIGGER value_links_no_update
        BEFORE UPDATE ON value_links BEGIN
            SELECT RAISE(ABORT, 'Value Link is immutable');
        END;
        CREATE TRIGGER artifact_links_no_update
        BEFORE UPDATE ON artifact_links BEGIN
            SELECT RAISE(ABORT, 'Artifact Link is immutable');
        END;
        """,
    ),
    (
        5,
        "static graph generations joins and control counters",
        """
        ALTER TABLE node_runs ADD COLUMN generation INTEGER NOT NULL DEFAULT 1
            CHECK (generation >= 1);
        ALTER TABLE node_runs ADD COLUMN activation_key TEXT NOT NULL DEFAULT 'legacy';

        ALTER TABLE branch_tokens ADD COLUMN edge_id TEXT;
        ALTER TABLE branch_tokens ADD COLUMN target_node_id TEXT;
        ALTER TABLE branch_tokens ADD COLUMN target_generation INTEGER
            CHECK (target_generation IS NULL OR target_generation >= 1);
        ALTER TABLE branch_tokens ADD COLUMN branch_group_id TEXT;

        CREATE UNIQUE INDEX node_runs_graph_activation
            ON node_runs(run_id, source_plan_version, node_id, generation, activation_key)
            WHERE activation_key != 'legacy';
        CREATE INDEX branch_tokens_graph_target
            ON branch_tokens(run_id, target_node_id, target_generation, status, token_id);

        CREATE TABLE join_groups (
            join_group_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            node_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation >= 1),
            policy_json TEXT NOT NULL,
            participant_edge_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('waiting', 'open', 'failed', 'timed_out')),
            decision_json TEXT,
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, node_id, generation),
            CHECK (join_group_id LIKE 'join_group:%')
        );

        CREATE TABLE graph_control_counters (
            counter_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            policy_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            value INTEGER NOT NULL CHECK (value >= 0),
            limit_value INTEGER NOT NULL CHECK (limit_value >= 1),
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, policy_id, scope_key),
            CHECK (counter_id LIKE 'control_counter:%')
        );

        CREATE INDEX join_groups_by_run_status
            ON join_groups(run_id, status, join_group_id);
        CREATE INDEX graph_control_counters_by_run
            ON graph_control_counters(run_id, policy_id, counter_id);
        """,
    ),
    (
        6,
        "durable planner attempts and proposals",
        """
        CREATE TABLE planner_attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'requested', 'running', 'response_received', 'accepted',
                'rejected', 'unknown', 'failed'
            )),
            context_json TEXT NOT NULL,
            context_hash TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            capability_manifest_hash TEXT NOT NULL,
            model_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            raw_response TEXT,
            raw_response_checksum TEXT,
            provider_request_id TEXT,
            usage_json TEXT,
            proposal_id TEXT,
            error_json TEXT,
            lease_owner TEXT,
            lease_token_hash TEXT,
            fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
            lease_expires_at TEXT,
            aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, attempt_number),
            CHECK (attempt_id LIKE 'planner_attempt:%'),
            CHECK (proposal_id IS NULL OR proposal_id LIKE 'proposal:%'),
            CHECK (
                (status = 'running' AND lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR
                (status != 'running' AND lease_owner IS NULL AND lease_token_hash IS NULL AND lease_expires_at IS NULL)
            ),
            CHECK (
                (raw_response IS NULL AND raw_response_checksum IS NULL) OR
                (raw_response IS NOT NULL AND raw_response_checksum IS NOT NULL)
            )
        );

        CREATE TABLE planner_proposals (
            proposal_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE REFERENCES planner_attempts(attempt_id),
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            base_plan_version INTEGER NOT NULL CHECK (base_plan_version >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'parsed', 'protocol_accepted', 'protocol_rejected', 'consumed'
            )),
            proposal_json TEXT NOT NULL,
            action_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            validation_json TEXT NOT NULL,
            raw_response_checksum TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, content_hash),
            CHECK (proposal_id LIKE 'proposal:%')
        );

        CREATE INDEX planner_attempts_claim
            ON planner_attempts(status, created_at, attempt_id);
        CREATE INDEX planner_attempts_by_run
            ON planner_attempts(run_id, attempt_number, attempt_id);
        CREATE INDEX planner_attempts_lease_expiry
            ON planner_attempts(status, lease_expires_at, attempt_id);
        CREATE INDEX planner_proposals_by_run_status
            ON planner_proposals(run_id, status, proposal_id);
        """,
    ),
    (
        7,
        "dynamic plans policy human tasks and budget ledger",
        """
        CREATE TABLE plan_patches (
            patch_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES planner_proposals(proposal_id),
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            base_plan_version INTEGER NOT NULL CHECK (base_plan_version >= 1),
            result_plan_version INTEGER,
            status TEXT NOT NULL CHECK (status IN ('draft', 'validated', 'committed', 'rejected', 'conflict')),
            reason TEXT NOT NULL,
            patch_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, content_hash),
            CHECK (patch_id LIKE 'plan_patch:%'),
            CHECK (result_plan_version IS NULL OR result_plan_version > base_plan_version)
        );

        CREATE TABLE policy_decisions (
            decision_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            patch_id TEXT NOT NULL REFERENCES plan_patches(patch_id),
            input_hash TEXT NOT NULL,
            rule_set_version TEXT NOT NULL,
            allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
            requires_approval INTEGER NOT NULL CHECK (requires_approval IN (0, 1)),
            results_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (patch_id, input_hash),
            CHECK (decision_id LIKE 'policy_decision:%')
        );

        CREATE TABLE human_tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            node_run_id TEXT REFERENCES node_runs(node_run_id),
            kind TEXT NOT NULL CHECK (kind IN ('approval', 'input', 'budget', 'recovery')),
            status TEXT NOT NULL CHECK (status IN ('waiting', 'claimed', 'completed', 'rejected', 'cancelled', 'expired')),
            request_hash TEXT NOT NULL,
            capability_scope TEXT,
            submission_token_hash TEXT NOT NULL,
            actor TEXT,
            payload_json TEXT NOT NULL,
            result_json TEXT,
            deadline_at TEXT,
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, request_hash),
            CHECK (task_id LIKE 'human_task:%')
        );

        CREATE TABLE budget_accounts (
            run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
            total_microunits INTEGER NOT NULL CHECK (total_microunits >= 0),
            reserved_microunits INTEGER NOT NULL DEFAULT 0 CHECK (reserved_microunits >= 0),
            consumed_microunits INTEGER NOT NULL DEFAULT 0 CHECK (consumed_microunits >= 0),
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE budget_reservations (
            reservation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES budget_accounts(run_id),
            owner_id TEXT NOT NULL,
            reserved_microunits INTEGER NOT NULL CHECK (reserved_microunits > 0),
            consumed_microunits INTEGER NOT NULL DEFAULT 0 CHECK (consumed_microunits >= 0),
            last_usage_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_usage_sequence >= 0),
            status TEXT NOT NULL CHECK (status IN ('active', 'settled', 'released', 'unknown')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (run_id, owner_id),
            CHECK (reservation_id LIKE 'reservation:%')
        );

        CREATE TABLE budget_ledger_entries (
            entry_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES budget_accounts(run_id),
            reservation_id TEXT REFERENCES budget_reservations(reservation_id),
            kind TEXT NOT NULL CHECK (kind IN ('account_opened', 'reserved', 'usage', 'settled', 'released', 'budget_added')),
            amount_microunits INTEGER NOT NULL,
            usage_sequence INTEGER,
            occurred_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            UNIQUE (reservation_id, usage_sequence),
            CHECK (entry_id LIKE 'ledger_entry:%')
        );

        CREATE INDEX plan_patches_by_run_status ON plan_patches(run_id, status, patch_id);
        CREATE INDEX human_tasks_by_run_status ON human_tasks(run_id, status, task_id);
        CREATE INDEX human_tasks_deadline ON human_tasks(status, deadline_at, task_id);
        CREATE INDEX budget_reservations_by_run_status ON budget_reservations(run_id, status, reservation_id);
        CREATE INDEX budget_ledger_by_run ON budget_ledger_entries(run_id, occurred_at, entry_id);
        """,
    ),
    (
        8,
        "human collaboration foreach subflow and dynamic dag",
        """
        ALTER TABLE human_tasks ADD COLUMN assignee TEXT;
        ALTER TABLE human_tasks ADD COLUMN role TEXT;
        ALTER TABLE human_tasks ADD COLUMN form_schema_json TEXT;
        ALTER TABLE human_tasks ADD COLUMN quorum_kind TEXT NOT NULL DEFAULT 'any'
            CHECK (quorum_kind IN ('any', 'all', 'n_of_m'));
        ALTER TABLE human_tasks ADD COLUMN quorum_count INTEGER NOT NULL DEFAULT 1
            CHECK (quorum_count >= 1);
        ALTER TABLE human_tasks ADD COLUMN reminder_interval_seconds INTEGER
            CHECK (reminder_interval_seconds IS NULL OR reminder_interval_seconds > 0);
        ALTER TABLE human_tasks ADD COLUMN escalation_policy_json TEXT;
        ALTER TABLE human_tasks ADD COLUMN claimed_by TEXT;
        ALTER TABLE human_tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1);

        CREATE TABLE human_task_participants (
            task_id TEXT NOT NULL REFERENCES human_tasks(task_id),
            actor TEXT NOT NULL,
            role TEXT,
            delegated_from TEXT,
            decision TEXT CHECK (decision IN ('approve', 'reject', 'provide_input', 'withdraw')),
            value_json TEXT,
            submitted_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            PRIMARY KEY (task_id, actor)
        );

        CREATE TABLE foreach_groups (
            group_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            node_run_id TEXT REFERENCES node_runs(node_run_id),
            source_checksum TEXT NOT NULL,
            plan_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'partial', 'failed', 'cancelled')),
            failure_policy TEXT NOT NULL CHECK (failure_policy IN ('fail_fast', 'continue', 'partial_success')),
            concurrency_limit INTEGER NOT NULL CHECK (concurrency_limit >= 1),
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            aggregate_json TEXT,
            aggregate_checksum TEXT,
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (group_id LIKE 'foreach_group:%')
        );

        CREATE TABLE foreach_items (
            item_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES foreach_groups(group_id),
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            item_key TEXT NOT NULL,
            item_index INTEGER NOT NULL CHECK (item_index >= 0),
            status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')),
            input_json TEXT NOT NULL,
            output_json TEXT,
            error_json TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (group_id, item_key), UNIQUE (group_id, item_index),
            CHECK (item_id LIKE 'foreach_item:%')
        );

        CREATE TABLE subflow_links (
            link_id TEXT PRIMARY KEY,
            parent_run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            child_run_id TEXT NOT NULL UNIQUE REFERENCES workflow_runs(run_id),
            parent_node_run_id TEXT REFERENCES node_runs(node_run_id),
            workflow_id TEXT NOT NULL,
            workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
            status TEXT NOT NULL CHECK (status IN ('starting', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')),
            correlation_id TEXT NOT NULL,
            propagation_policy_json TEXT NOT NULL,
            input_mapping_json TEXT NOT NULL,
            output_mapping_json TEXT NOT NULL,
            artifact_scope_json TEXT NOT NULL,
            recursion_depth INTEGER NOT NULL CHECK (recursion_depth >= 1),
            aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (link_id LIKE 'subflow_link:%')
        );

        CREATE INDEX human_participants_actor ON human_task_participants(actor, task_id);
        CREATE INDEX foreach_groups_scan ON foreach_groups(run_id, status, group_id);
        CREATE INDEX foreach_items_schedule ON foreach_items(group_id, status, item_index);
        CREATE INDEX subflow_links_parent ON subflow_links(parent_run_id, status, link_id);
        """,
    ),
    (
        9,
        "security audit and api idempotency",
        """
        CREATE TABLE security_capabilities (
            capability_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            scope TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            parent_capability_id TEXT REFERENCES security_capabilities(capability_id),
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
            issued_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            CHECK (capability_id LIKE 'capability:%')
        );

        CREATE TABLE artifact_acl (
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            subject TEXT NOT NULL,
            permission TEXT NOT NULL CHECK (permission IN ('read', 'write', 'delegate')),
            granted_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (artifact_id, subject, permission)
        );

        CREATE TABLE audit_records (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES workflow_runs(run_id),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            details_json TEXT NOT NULL,
            correlation_id TEXT,
            occurred_at TEXT NOT NULL,
            CHECK (audit_id LIKE 'audit:%')
        );

        CREATE TABLE api_command_receipts (
            actor TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (actor, idempotency_key)
        );

        CREATE INDEX capabilities_subject ON security_capabilities(subject, status, capability_id);
        CREATE INDEX audit_run_time ON audit_records(run_id, occurred_at, audit_id);
        """,
    ),
    (
        10,
        "run discovery projection",
        """
        ALTER TABLE workflow_runs ADD COLUMN goal TEXT;
        ALTER TABLE workflow_runs ADD COLUMN display_name TEXT;
        UPDATE workflow_runs SET display_name = run_id WHERE display_name IS NULL;
        CREATE INDEX workflow_runs_discovery
            ON workflow_runs(updated_at DESC, run_id ASC);
        CREATE INDEX workflow_runs_status_discovery
            ON workflow_runs(status, updated_at DESC, run_id ASC);
        """,
    ),
    (
        11,
        "actor scoped artifact catalog",
        """
        CREATE TABLE run_artifact_subjects (
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            subject TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'participant')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, subject)
        );
        CREATE INDEX artifact_acl_subject
            ON artifact_acl(subject, permission, artifact_id);
        CREATE INDEX run_artifact_subject_actor
            ON run_artifact_subjects(subject, run_id);
        """,
    ),
    (
        12,
        "foreach child run binding",
        """
        ALTER TABLE foreach_items ADD COLUMN child_run_id TEXT
            REFERENCES workflow_runs(run_id);
        CREATE UNIQUE INDEX foreach_item_child_run
            ON foreach_items(child_run_id) WHERE child_run_id IS NOT NULL;
        CREATE INDEX foreach_child_terminal_scan
            ON foreach_items(group_id, status, child_run_id);
        """,
    ),
    (
        13,
        "persistent workflow editing drafts",
        """
        CREATE TABLE workflow_drafts (
            draft_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflow_definitions(workflow_id),
            base_version INTEGER NOT NULL CHECK (base_version >= 1),
            actor TEXT NOT NULL,
            source_format TEXT NOT NULL CHECK (source_format IN ('json', 'yaml')),
            source_text TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            validation_status TEXT NOT NULL CHECK (
                validation_status IN ('dirty', 'valid', 'invalid')
            ),
            validated_source_hash TEXT,
            validated_definition_hash TEXT,
            diagnostics_json TEXT NOT NULL DEFAULT '[]',
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            status TEXT NOT NULL CHECK (status IN ('active', 'published', 'discarded')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_version INTEGER,
            CHECK (draft_id LIKE 'workflow_draft:%')
        );

        CREATE UNIQUE INDEX workflow_drafts_one_active
            ON workflow_drafts(workflow_id, actor)
            WHERE status = 'active';
        """,
    ),
    (
        14,
        "agent workflow draft revision candidates",
        """
        CREATE TABLE workflow_draft_revisions (
            revision_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES workflow_drafts(draft_id),
            base_draft_revision INTEGER NOT NULL CHECK (base_draft_revision >= 1),
            instruction_text TEXT NOT NULL,
            instruction_hash TEXT NOT NULL,
            previous_source_text TEXT NOT NULL,
            previous_source_hash TEXT NOT NULL,
            previous_validation_status TEXT NOT NULL CHECK (
                previous_validation_status IN ('dirty', 'valid', 'invalid')
            ),
            previous_validated_source_hash TEXT,
            previous_definition_hash TEXT,
            proposed_source_text TEXT NOT NULL,
            proposed_source_hash TEXT NOT NULL,
            proposed_definition_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL CHECK (attempts >= 1),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'accepted', 'rejected', 'undone')
            ),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by TEXT,
            CHECK (revision_id LIKE 'workflow_revision:%')
        );

        CREATE UNIQUE INDEX workflow_draft_one_pending_revision
            ON workflow_draft_revisions(draft_id) WHERE status = 'pending';
        CREATE INDEX workflow_draft_revision_history
            ON workflow_draft_revisions(draft_id, created_at DESC);
        """,
    ),
)


def migrate_workflow_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        )
        """
    )
    connection.commit()
    applied = {
        row[0] for row in connection.execute("SELECT version FROM workflow_schema_migrations")
    }
    for version, name, sql in _MIGRATIONS:
        if version in applied:
            continue
        escaped_name = name.replace("'", "''")
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + sql
            + f"\nINSERT INTO workflow_schema_migrations(version, name) "
            f"VALUES ({version}, '{escaped_name}');\nCOMMIT;"
        )
