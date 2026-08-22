from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from orbit.workflow.domain.data import (
    ArtifactLink, ArtifactLinkType, ArtifactMetadata, ArtifactStatus,
    ArtifactVisibility, DataCommitManifest, DataOwnerKind, InputManifest,
    InputManifestItem, PortDataPolicy, PortTransport, SecretRef,
    StagedArtifactCommit, ValueCommit, ValueLink, ValueLinkType, ValueRecord,
    DEFAULT_ARTIFACT_BYTES, MAX_ARTIFACT_BYTES, MAX_INLINE_VALUE_BYTES,
    derive_artifact_id, derive_value_id,
)
from orbit.workflow.data.secrets import SecretLeakDetected, assert_no_secret_values
from orbit.workflow.domain.errors import ERROR_CODE_REGISTRY, ErrorCategory
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.models import ArtifactRef
from orbit.workflow.domain.schemas import SchemaValidationError, validate_contract
from orbit.workflow.domain.serialization import canonical_json, definition_hash, to_primitive
from orbit.workflow.domain.stability import CONTRACT_STABILITY, ContractStability
from orbit.workflow.domain.versions import DefinitionHash


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "workflow_data" / "v1" / "data-contracts.json"


def value(port_id="score", data=None):
    data = {"score": 0.8} if data is None else data
    return ValueCommit(
        port_id, "schema://score/1.0", data, definition_hash(data),
        len(canonical_json(data).encode("utf-8")),
    )


def artifact_ref():
    return ArtifactRef(
        EntityId("artifact", "report-001"), "schema://report/1.0",
        "text/markdown", DefinitionHash("sha256:" + "a" * 64), 120,
    )


def contracts():
    run_id = EntityId("run", "data-001")
    workflow_id = EntityId("workflow", "data")
    node_run_id = EntityId("node_run", "produce-001")
    attempt_id = EntityId("attempt", "produce-001")
    event_id = EntityId("event", "data-001")
    inline = value()
    artifact = artifact_ref()
    staged = StagedArtifactCommit(
        "report", artifact.artifact_id, artifact.checksum, artifact.size_bytes
    )
    metadata = ArtifactMetadata(
        artifact.artifact_id, run_id, workflow_id, "attempt", attempt_id,
        node_run_id, "report", artifact.schema_id, artifact.content_type,
        artifact.checksum, artifact.size_bytes, artifact.checksum.value,
        ArtifactVisibility.RUN, run_id, ArtifactStatus.COMMITTED, NOW, NOW,
        event_id,
    )
    record = ValueRecord(
        EntityId("value", "score-001"), run_id, DataOwnerKind.ATTEMPT_OUTPUT,
        attempt_id, inline.port_id, inline.schema_id, inline.data,
        inline.checksum, inline.size_bytes, event_id, NOW,
    )
    value_link = ValueLink(
        EntityId("value_link", "mapped-001"), run_id,
        EntityId("value", "source-001"), record.value_id,
        ValueLinkType.MAPPED_FROM, definition_hash({"op": "identity"}),
        event_id, NOW,
    )
    artifact_link = ArtifactLink(
        EntityId("artifact_link", "producer-001"), workflow_id, run_id,
        artifact.artifact_id, ArtifactLinkType.PRODUCER, attempt_id, event_id,
        NOW,
    )
    secret = SecretRef("API_KEY", "v1", "local")
    manifest = InputManifest(
        run_id, node_run_id, attempt_id,
        (
            InputManifestItem("score", PortTransport.INLINE, inline.schema_id, value=inline),
            InputManifestItem("report", PortTransport.ARTIFACT_REF, artifact.schema_id, artifact=artifact),
            InputManifestItem("credential", PortTransport.SECRET_REF, "schema://secret-ref/1.0", secret=secret),
        ),
    )
    commit = DataCommitManifest(
        run_id, DataOwnerKind.ATTEMPT_OUTPUT, attempt_id, (inline,), (staged,)
    )
    return {
        "port_data_policy": PortDataPolicy(PortTransport.ARTIFACT_REF),
        "secret_ref": secret,
        "value_commit": inline,
        "staged_artifact_commit": staged,
        "data_commit_manifest": commit,
        "value_record": record,
        "value_link": value_link,
        "artifact_metadata": metadata,
        "artifact_link": artifact_link,
        "input_manifest": manifest,
    }


class WorkflowDataContractTests(unittest.TestCase):
    def test_all_contracts_match_schema_and_golden(self):
        values = contracts()
        self.assertEqual(json.loads(FIXTURE.read_text()), to_primitive(values))
        names = {
            "port_data_policy": "port-data-policy/1.0",
            "secret_ref": "secret-ref/1.0",
            "value_commit": "value-commit/1.0",
            "staged_artifact_commit": "staged-artifact-commit/1.0",
            "data_commit_manifest": "data-commit-manifest/1.0",
            "value_record": "value-record/1.0",
            "value_link": "value-link/1.0",
            "artifact_metadata": "artifact-metadata/1.0",
            "artifact_link": "artifact-link/1.0",
            "input_manifest": "input-manifest/1.0",
        }
        for name, contract in names.items():
            with self.subTest(contract):
                validate_contract(to_primitive(values[name]), contract)

    def test_port_policy_defaults_and_fail_closed_combinations(self):
        artifact = PortDataPolicy(PortTransport.ARTIFACT_REF)
        self.assertEqual(67_108_864, artifact.max_size_bytes)
        self.assertEqual(("application/octet-stream",), artifact.content_types)
        self.assertIs(ArtifactVisibility.RUN, artifact.visibility)
        with self.assertRaises(ValueError):
            PortDataPolicy(PortTransport.INLINE, visibility=ArtifactVisibility.RUN)
        with self.assertRaises(ValueError):
            PortDataPolicy(PortTransport.SECRET_REF, max_size_bytes=1)

    def test_value_checksum_size_and_deep_immutability(self):
        item = value(data={"nested": {"answer": 42}})
        with self.assertRaises(TypeError):
            item.data["nested"]["answer"] = 0
        with self.assertRaises(FrozenInstanceError):
            item.port_id = "changed"
        with self.assertRaisesRegex(ValueError, "checksum"):
            ValueCommit(
                "score", item.schema_id, item.data,
                DefinitionHash("sha256:" + "0" * 64), item.size_bytes,
            )
        with self.assertRaisesRegex(ValueError, "size_bytes"):
            ValueCommit("score", item.schema_id, item.data, item.checksum, 1)
        owner = EntityId("attempt", "a1")
        self.assertEqual(
            derive_value_id(owner, "score"), derive_value_id(owner, "score")
        )
        self.assertNotEqual(
            derive_artifact_id(owner, "report", "one"),
            derive_artifact_id(owner, "report", "two"),
        )

    def test_artifact_lifecycle_scope_and_lineage_are_explicit(self):
        item = contracts()["artifact_metadata"]
        self.assertEqual(item.ref, artifact_ref())
        with self.assertRaisesRegex(ValueError, "commit time"):
            ArtifactMetadata(
                item.artifact_id, item.run_id, item.workflow_id, "attempt",
                item.producer_id, item.producer_node_run_id, item.output_port_id,
                item.schema_id, item.content_type, item.checksum,
                item.size_bytes, item.blob_key, item.visibility, item.scope_id,
                ArtifactStatus.COMMITTED, NOW,
            )
        with self.assertRaisesRegex(ValueError, "scope"):
            ArtifactMetadata(
                item.artifact_id, item.run_id, item.workflow_id, "attempt",
                item.producer_id, item.producer_node_run_id, item.output_port_id,
                item.schema_id, item.content_type, item.checksum,
                item.size_bytes, item.blob_key, ArtifactVisibility.WORKFLOW,
                item.run_id, ArtifactStatus.STAGED, NOW,
            )

    def test_manifest_requires_exactly_one_representation_and_unique_ports(self):
        inline = value()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            InputManifestItem(
                "score", PortTransport.INLINE, inline.schema_id,
                value=inline, secret=SecretRef("API_KEY"),
            )
        with self.assertRaisesRegex(ValueError, "duplicate ports"):
            InputManifest(
                EntityId("run", "r1"), EntityId("node_run", "n1"),
                EntityId("attempt", "a1"),
                (
                    InputManifestItem("score", PortTransport.INLINE, inline.schema_id, value=inline),
                    InputManifestItem("score", PortTransport.INLINE, inline.schema_id, value=inline),
                ),
            )

    def test_stability_and_error_registry_are_explicit(self):
        for name in (
            "port_data_policy", "artifact_contracts",
            "input_manifest", "data_commit_manifest",
        ):
            self.assertIs(ContractStability.STABLE, CONTRACT_STABILITY[name])
        self.assertIs(
            ErrorCategory.POLICY_REJECTED,
            ERROR_CODE_REGISTRY["artifact_access_denied"],
        )

    def test_resolved_secret_scan_covers_values_and_binary_artifacts(self):
        with self.assertRaises(SecretLeakDetected) as caught:
            assert_no_secret_values(
                {"nested": ["prefix-super-secret-suffix"]}, ("super-secret",)
            )
        self.assertEqual("$.nested[0]", caught.exception.json_path)
        with self.assertRaises(SecretLeakDetected):
            assert_no_secret_values(b"binary-super-secret", ("super-secret",))
        self.assertNotIn("super-secret", str(caught.exception))

    def test_schema_reports_precise_path(self):
        primitive = to_primitive(contracts()["port_data_policy"])
        primitive["transport"] = "file"
        with self.assertRaises(SchemaValidationError) as caught:
            validate_contract(primitive, "port-data-policy/1.0")
        self.assertEqual("$.transport", caught.exception.json_path)


if __name__ == "__main__":
    unittest.main()


class PortDataPolicyTests(unittest.TestCase):
    """What each transport may declare, and what it is given if it does not.

    The three transports are not variations on one shape: an inline port
    carries a value, an artifact port carries a reference to bytes held
    elsewhere, and a secret port carries neither. Mixing their vocabularies is
    the authoring mistake this refuses.
    """

    def test_inline_takes_a_default_ceiling_and_nothing_else(self) -> None:
        policy = PortDataPolicy()
        self.assertIs(PortTransport.INLINE, policy.transport)
        self.assertEqual(MAX_INLINE_VALUE_BYTES, policy.max_size_bytes)
        self.assertEqual((), policy.content_types)
        self.assertIsNone(policy.visibility)

    def test_inline_refuses_the_artifact_vocabulary(self) -> None:
        for extra in (
            {"content_types": ("text/plain",)},
            {"visibility": ArtifactVisibility.RUN},
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, "inline ports cannot"):
                    PortDataPolicy(transport=PortTransport.INLINE, **extra)

    def test_an_artifact_port_is_given_a_type_and_a_visibility(self) -> None:
        policy = PortDataPolicy(transport=PortTransport.ARTIFACT_REF)
        self.assertEqual(DEFAULT_ARTIFACT_BYTES, policy.max_size_bytes)
        self.assertEqual(("application/octet-stream",), policy.content_types)
        self.assertIs(ArtifactVisibility.RUN, policy.visibility)

    def test_content_types_are_normalised_and_deduplicated(self) -> None:
        policy = PortDataPolicy(
            transport=PortTransport.ARTIFACT_REF,
            content_types=(" TEXT/Markdown ", "text/markdown", "text/plain"),
        )
        self.assertEqual(("text/markdown", "text/plain"), policy.content_types)

    def test_a_content_type_that_is_not_a_mime_type_is_refused(self) -> None:
        for value in ("markdown", "text /plain", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "normalized MIME types"):
                    PortDataPolicy(
                        transport=PortTransport.ARTIFACT_REF, content_types=(value,),
                    )

    def test_a_secret_port_declares_nothing_about_the_value(self) -> None:
        policy = PortDataPolicy(transport=PortTransport.SECRET_REF)
        self.assertEqual(0, policy.max_size_bytes)
        self.assertEqual((), policy.content_types)
        self.assertIsNone(policy.visibility)

        for extra in (
            {"max_size_bytes": 10},
            {"content_types": ("text/plain",)},
            {"visibility": ArtifactVisibility.RUN},
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, "secret_ref ports cannot"):
                    PortDataPolicy(transport=PortTransport.SECRET_REF, **extra)

    def test_a_data_port_must_have_room_for_something(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            PortDataPolicy(transport=PortTransport.INLINE, max_size_bytes=0)

    def test_a_size_over_the_transport_limit_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds the transport hard limit"):
            PortDataPolicy(
                transport=PortTransport.INLINE,
                max_size_bytes=MAX_INLINE_VALUE_BYTES + 1,
            )
        with self.assertRaisesRegex(ValueError, "exceeds the transport hard limit"):
            PortDataPolicy(
                transport=PortTransport.ARTIFACT_REF,
                max_size_bytes=MAX_ARTIFACT_BYTES + 1,
            )

    def test_a_negative_or_boolean_size_is_not_a_size(self) -> None:
        for value in (-1, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    PortDataPolicy(
                        transport=PortTransport.INLINE, max_size_bytes=value,
                    )

    def test_the_transport_must_be_one_of_the_three(self) -> None:
        with self.assertRaisesRegex(TypeError, "transport must be PortTransport"):
            PortDataPolicy(transport="inline")


class DerivedIdentityTests(unittest.TestCase):
    """Value and Artifact ids are derived, so the same work names one thing.

    An id that moved between two derivations of the same inputs would make
    every idempotent retry produce a second record.
    """

    def test_the_same_inputs_derive_the_same_value_id(self) -> None:
        owner = EntityId("attempt", "a" * 32)
        self.assertEqual(
            derive_value_id(owner, "result"), derive_value_id(owner, "result"),
        )

    def test_each_part_of_the_input_changes_the_id(self) -> None:
        owner = EntityId("attempt", "a" * 32)
        other = EntityId("attempt", "b" * 32)
        base = derive_value_id(owner, "result")
        self.assertNotEqual(base, derive_value_id(other, "result"))
        self.assertNotEqual(base, derive_value_id(owner, "other"))
        self.assertNotEqual(base, derive_value_id(owner, "result", 2))

    def test_a_value_owner_must_be_a_run_node_run_or_attempt(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected run/node_run/attempt id"):
            derive_value_id(EntityId("workflow", "a" * 32), "result")
        with self.assertRaisesRegex(TypeError, "must be an EntityId"):
            derive_value_id("attempt:aaa", "result")

    def test_a_generation_counts_from_one(self) -> None:
        owner = EntityId("attempt", "a" * 32)
        for generation in (0, -1, True, 1.0):
            with self.subTest(generation=generation):
                with self.assertRaisesRegex(ValueError, "generation must be positive"):
                    derive_value_id(owner, "result", generation)

    def test_an_artifact_id_needs_a_port_and_a_logical_name(self) -> None:
        owner = EntityId("attempt", "a" * 32)
        self.assertNotEqual(
            derive_artifact_id(owner, "result", "one"),
            derive_artifact_id(owner, "result", "two"),
        )
        for port, name in (("", "one"), ("result", ""), ("  ", "one")):
            with self.subTest(port=port, name=name):
                with self.assertRaises(ValueError):
                    derive_artifact_id(owner, port, name)

    def test_an_artifact_owner_is_a_run_or_an_attempt(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected run/attempt id"):
            derive_artifact_id(EntityId("node_run", "a" * 32), "result", "one")
