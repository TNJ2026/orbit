"""Application boundary for durable Planner attempts and strict proposals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import secrets

from ..domain.ids import EntityId
from ..domain.planner import (
    PlannerAttemptRecord, PlannerAttemptStatus, PlannerProposalRecord,
    PlannerProposalStatus, PlannerUsage, PlanningContext,
    derive_planner_attempt_id, planner_request_fingerprint, strict_parse_proposal,
    validate_planner_transition,
)
from ..domain.serialization import definition_hash, to_primitive
from ..domain.versions import AggregateVersion, DefinitionHash, Revision
from ..persistence.uow import SQLiteUnitOfWork
from ..planner.events import planner_event


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class PlannerClaim:
    attempt_id: EntityId
    lease_token: str
    fencing_token: int
    request_fingerprint: DefinitionHash


class PlannerApplicationService:
    def __init__(self, path, *, provider=None, uow_factory=None, fault_hook=None, max_attempts=3) -> None:
        self.path = path
        self.provider = provider
        if max_attempts < 1: raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self.uow_factory = uow_factory or (lambda: SQLiteUnitOfWork(path, fault_hook=fault_hook))
        from ..runtime.planner_recovery import PlannerRecoveryScanner
        self.recovery = PlannerRecoveryScanner(self)

    def request_decision(
        self, context: PlanningContext, *, prompt_hash: DefinitionHash,
        capability_manifest_hash: DefinitionHash, model_id: str,
        provider_id: str, now: datetime,
    ) -> PlannerAttemptRecord:
        fingerprint = planner_request_fingerprint(
            context, prompt_hash=prompt_hash,
            capability_manifest_hash=capability_manifest_hash,
            model_id=model_id, provider_id=provider_id,
        )
        with self.uow_factory() as uow:
            if uow.runs.get(context.run_id) is None:
                raise ValueError("Planner Run was not found")
            prior = uow.planner_attempts.list_by_run(context.run_id)
            duplicate = next((item for item in prior if item.request_fingerprint == fingerprint), None)
            if duplicate is not None:
                return duplicate
            number = Revision(len(prior) + 1)
            attempt_id = derive_planner_attempt_id(context.run_id, number, fingerprint)
            initial = PlannerAttemptRecord(
                attempt_id=attempt_id, run_id=context.run_id,
                attempt_number=number, status=PlannerAttemptStatus.REQUESTED,
                context=context, prompt_hash=prompt_hash,
                capability_manifest_hash=capability_manifest_hash,
                model_id=model_id, provider_id=provider_id,
                request_fingerprint=fingerprint, raw_response=None,
                raw_response_checksum=None, provider_request_id=None, usage=None,
                proposal_id=None, error=None, lease_owner=None,
                lease_token_hash=None, fencing_token=0, lease_expires_at=None,
                aggregate_version=AggregateVersion(0), created_at=now, updated_at=now,
            )
            event = planner_event(
                attempt=initial, ordinal=1, event_type="planner_decision_requested", now=now,
                payload={
                    "run_id": str(context.run_id), "attempt_number": number.value,
                    "context_hash": context.context_hash.value,
                    "prompt_hash": prompt_hash.value,
                    "capability_manifest_hash": capability_manifest_hash.value,
                    "model_id": model_id, "provider_id": provider_id,
                    "request_fingerprint": fingerprint.value,
                },
            )
            uow.events.append(context.run_id, attempt_id, AggregateVersion(0), (event,))
            created = replace(initial, aggregate_version=AggregateVersion(1))
            uow.planner_attempts.create(created)
            uow.commit()
            return created

    def claim(self, worker_id: str, now: datetime, *, lease_ttl=timedelta(seconds=60)) -> PlannerClaim | None:
        if not worker_id.strip() or lease_ttl <= timedelta(0) or lease_ttl > timedelta(minutes=10):
            raise ValueError("invalid Planner lease")
        raw = secrets.token_urlsafe(32)
        with self.uow_factory() as uow:
            candidates = uow.planner_attempts.list_claimable(limit=1)
            if not candidates:
                return None
            attempt = candidates[0]
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_attempt_started", now=now,
                payload={"worker_id": worker_id, "fencing_token": attempt.fencing_token + 1,
                         "lease_expires_at": to_primitive(now + lease_ttl)},
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            validate_planner_transition(attempt.status, PlannerAttemptStatus.RUNNING)
            updated = replace(
                attempt, status=PlannerAttemptStatus.RUNNING, lease_owner=worker_id,
                lease_token_hash=_token_hash(raw), fencing_token=attempt.fencing_token + 1,
                lease_expires_at=now + lease_ttl,
                aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit()
            return PlannerClaim(updated.attempt_id, raw, updated.fencing_token, updated.request_fingerprint)

    @staticmethod
    def _authorize(attempt, claim, now):
        if attempt is None or attempt.status is not PlannerAttemptStatus.RUNNING:
            raise PermissionError("Planner Attempt is not running")
        if claim.attempt_id != attempt.attempt_id or claim.fencing_token != attempt.fencing_token:
            raise PermissionError("stale Planner lease fence")
        if _token_hash(claim.lease_token) != attempt.lease_token_hash:
            raise PermissionError("invalid Planner lease token")
        if attempt.lease_expires_at <= now:
            raise PermissionError("Planner lease expired")

    def record_response(self, claim, response, now):
        checksum = definition_hash(response.raw_response)
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(claim.attempt_id)
            self._authorize(attempt, claim, now)
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_response_received", now=now,
                payload={
                    "raw_response_checksum": checksum.value,
                    "raw_response_size": len(response.raw_response.encode()),
                    "provider_request_id": response.provider_request_id,
                    "usage": to_primitive(response.usage),
                },
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            validate_planner_transition(attempt.status, PlannerAttemptStatus.RESPONSE_RECEIVED)
            updated = replace(
                attempt, status=PlannerAttemptStatus.RESPONSE_RECEIVED,
                raw_response=response.raw_response, raw_response_checksum=checksum,
                provider_request_id=response.provider_request_id, usage=response.usage,
                lease_owner=None, lease_token_hash=None, lease_expires_at=None,
                aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit()
            return updated

    def parse_response(self, attempt_id, now):
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(attempt_id)
            if attempt is None: raise ValueError("Planner Attempt was not found")
            if attempt.status in {PlannerAttemptStatus.ACCEPTED, PlannerAttemptStatus.REJECTED}:
                return None if attempt.proposal_id is None else uow.planner_proposals.get(attempt.proposal_id)
            if attempt.status is not PlannerAttemptStatus.RESPONSE_RECEIVED:
                raise ValueError("Planner response is not ready to parse")
            try:
                proposal = strict_parse_proposal(attempt.raw_response, expected_run_id=attempt.run_id)
                if proposal.base_plan_version != attempt.context.plan_version:
                    raise ValueError("ActionProposal base_plan_version does not match PlanningContext")
                same_id = uow.planner_proposals.get(proposal.proposal_id)
                if same_id is not None and same_id.proposal.content_hash != proposal.content_hash:
                    raise ValueError("Proposal ID was reused with different content")
                prior = uow.planner_proposals.find_by_hash(attempt.run_id, proposal.content_hash)
                selected = same_id or prior
                events = (
                    planner_event(attempt=attempt, ordinal=1, event_type="planner_proposal_parsed", now=now,
                                  payload={"proposal_id": str(proposal.proposal_id), "content_hash": proposal.content_hash.value}),
                    planner_event(attempt=attempt, ordinal=2, event_type="planner_proposal_accepted", now=now,
                                  payload={"proposal_id": str((selected or PlannerProposalRecord(proposal, attempt.attempt_id, PlannerProposalStatus.PROTOCOL_ACCEPTED, {"valid": True}, attempt.raw_response_checksum, now)).proposal.proposal_id), "protocol_only": True}),
                )
                uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, events)
                record = selected or PlannerProposalRecord(
                    proposal, attempt.attempt_id, PlannerProposalStatus.PROTOCOL_ACCEPTED,
                    {"valid": True, "phase": "protocol"}, attempt.raw_response_checksum, now,
                )
                if selected is None: uow.planner_proposals.create(record)
                validate_planner_transition(attempt.status, PlannerAttemptStatus.ACCEPTED)
                updated = replace(
                    attempt, status=PlannerAttemptStatus.ACCEPTED,
                    proposal_id=record.proposal.proposal_id,
                    aggregate_version=AggregateVersion(attempt.aggregate_version.value + 2), updated_at=now,
                )
                uow.planner_attempts.update(updated, attempt.aggregate_version)
                uow.commit()
                return record
            except ValueError as exc:
                event = planner_event(
                    attempt=attempt, ordinal=1, event_type="planner_proposal_rejected", now=now,
                    payload={"code": "planner_proposal_invalid", "message": str(exc)},
                )
                uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
                validate_planner_transition(attempt.status, PlannerAttemptStatus.REJECTED)
                updated = replace(
                    attempt, status=PlannerAttemptStatus.REJECTED,
                    error={"code": "planner_proposal_invalid", "message": str(exc)},
                    aggregate_version=attempt.aggregate_version.next(), updated_at=now,
                )
                uow.planner_attempts.update(updated, attempt.aggregate_version)
                uow.commit()
                return None

    def mark_unknown(self, claim, now, *, usage=None, reason="provider result unknown"):
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(claim.attempt_id)
            self._authorize(attempt, claim, now)
            usage = usage or PlannerUsage(incomplete=True)
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_attempt_unknown", now=now,
                payload={"reason": reason, "usage": to_primitive(usage)},
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            validate_planner_transition(attempt.status, PlannerAttemptStatus.UNKNOWN)
            updated = replace(
                attempt, status=PlannerAttemptStatus.UNKNOWN, usage=usage,
                error={"code": "planner_result_unknown", "message": reason},
                lease_owner=None, lease_token_hash=None, lease_expires_at=None,
                aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit(); return updated

    def expire_attempt(self, attempt_id, now):
        """Fence an expired call as unknown without pretending its result failed."""
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(attempt_id)
            if attempt is None or attempt.status is not PlannerAttemptStatus.RUNNING:
                return attempt
            if attempt.lease_expires_at > now:
                raise ValueError("Planner lease has not expired")
            usage = attempt.usage or PlannerUsage(incomplete=True)
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_attempt_unknown", now=now,
                payload={"reason": "planner lease expired with unknown provider result", "usage": to_primitive(usage)},
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            validate_planner_transition(attempt.status, PlannerAttemptStatus.UNKNOWN)
            updated = replace(
                attempt, status=PlannerAttemptStatus.UNKNOWN, usage=usage,
                error={"code": "planner_result_unknown", "message": "planner lease expired with unknown provider result"},
                lease_owner=None, lease_token_hash=None, lease_expires_at=None,
                aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit(); return updated

    def mark_failed(self, claim, now, *, code="planner_provider_permanent", message="planner provider failed"):
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(claim.attempt_id)
            self._authorize(attempt, claim, now)
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_attempt_failed", now=now,
                payload={"code": code, "message": message},
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            validate_planner_transition(attempt.status, PlannerAttemptStatus.FAILED)
            updated = replace(
                attempt, status=PlannerAttemptStatus.FAILED,
                error={"code": code, "message": message},
                lease_owner=None, lease_token_hash=None, lease_expires_at=None,
                aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit(); return updated

    def retry_unknown(self, attempt_id, now):
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(attempt_id)
            if attempt is None or attempt.status is not PlannerAttemptStatus.UNKNOWN:
                raise ValueError("only unknown Planner Attempt can be retried")
            prior = uow.planner_attempts.list_by_run(attempt.run_id)
            decision_chain = tuple(
                item for item in prior
                if item.context.context_hash == attempt.context.context_hash
            )
            if len(decision_chain) >= self.max_attempts:
                event = planner_event(
                    attempt=attempt, ordinal=1,
                    event_type="planner_escalation_requested", now=now,
                    payload={"reason": "unknown planner attempts exhausted", "attempts_exhausted": len(decision_chain)},
                )
                uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
                updated = replace(
                    attempt, error={**dict(attempt.error), "escalation_requested": True},
                    aggregate_version=attempt.aggregate_version.next(), updated_at=now,
                )
                uow.planner_attempts.update(updated, attempt.aggregate_version)
                uow.commit()
                return None
        # A retry is intentionally a new fingerprint/Attempt identity even though
        # the recorded Context and provider configuration are unchanged.
        retry_prompt = DefinitionHash("sha256:" + hashlib.sha256(f"{attempt.prompt_hash.value}|retry|{len(decision_chain)+1}".encode()).hexdigest())
        return self.request_decision(
            attempt.context, prompt_hash=retry_prompt,
            capability_manifest_hash=attempt.capability_manifest_hash,
            model_id=attempt.model_id, provider_id=attempt.provider_id, now=now,
        )

    def renew_claim(self, claim, now, *, lease_ttl=timedelta(seconds=60)):
        """Renewal is the documented lease-only projection exception."""
        if lease_ttl <= timedelta(0) or lease_ttl > timedelta(minutes=10):
            raise ValueError("invalid Planner lease renewal")
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(claim.attempt_id)
            self._authorize(attempt, claim, now)
            updated = replace(attempt, lease_expires_at=now + lease_ttl, updated_at=now)
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit(); return updated

    def retry_failed(self, attempt_id, now):
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(attempt_id)
            if attempt is None or attempt.status is not PlannerAttemptStatus.FAILED:
                raise ValueError("only failed Planner Attempt can be retried")
            if not attempt.error or attempt.error.get("code") != "planner_provider_transient":
                raise ValueError("Planner Attempt failure is not retryable")
            prior = uow.planner_attempts.list_by_run(attempt.run_id)
            decision_chain = tuple(
                item for item in prior
                if item.context.context_hash == attempt.context.context_hash
            )
            if len(decision_chain) >= self.max_attempts:
                event = planner_event(
                    attempt=attempt, ordinal=1,
                    event_type="planner_escalation_requested", now=now,
                    payload={"reason": "planner retry attempts exhausted", "attempts_exhausted": len(decision_chain)},
                )
                uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
                updated = replace(
                    attempt, error={**dict(attempt.error), "escalation_requested": True},
                    aggregate_version=attempt.aggregate_version.next(), updated_at=now,
                )
                uow.planner_attempts.update(updated, attempt.aggregate_version)
                uow.commit()
                return None
        retry_prompt = DefinitionHash("sha256:" + hashlib.sha256(f"{attempt.prompt_hash.value}|retry|{len(decision_chain)+1}".encode()).hexdigest())
        return self.request_decision(
            attempt.context, prompt_hash=retry_prompt,
            capability_manifest_hash=attempt.capability_manifest_hash,
            model_id=attempt.model_id, provider_id=attempt.provider_id, now=now,
        )

    def record_late_response(self, attempt_id, response, now):
        checksum = definition_hash(response.raw_response)
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(attempt_id)
            if attempt is None or attempt.status is not PlannerAttemptStatus.UNKNOWN:
                raise ValueError("late response requires unknown Planner Attempt")
            event = planner_event(
                attempt=attempt, ordinal=1, event_type="planner_late_response_recorded", now=now,
                payload={"raw_response_checksum": checksum.value,
                         "provider_request_id": response.provider_request_id,
                         "usage": to_primitive(response.usage)},
            )
            uow.events.append(attempt.run_id, attempt.attempt_id, attempt.aggregate_version, (event,))
            updated = replace(
                attempt, raw_response=response.raw_response,
                raw_response_checksum=checksum, provider_request_id=response.provider_request_id,
                usage=response.usage, aggregate_version=attempt.aggregate_version.next(), updated_at=now,
            )
            uow.planner_attempts.update(updated, attempt.aggregate_version)
            uow.commit(); return updated

    def execute_claimed(self, claim, now):
        if self.provider is None: raise RuntimeError("Planner provider is not configured")
        with self.uow_factory() as uow:
            attempt = uow.planner_attempts.get(claim.attempt_id)
            self._authorize(attempt, claim, now)
        try:
            response = self.provider.generate(
                attempt.context, model_id=attempt.model_id,
                request_fingerprint=attempt.request_fingerprint.value,
            )
        except TimeoutError:
            return self.mark_unknown(claim, now, reason="planner provider timed out with unknown result")
        except Exception as exc:
            from ..planner.provider import PlannerTransientError
            if isinstance(exc, PlannerTransientError):
                failed = self.mark_failed(
                    claim, now, code="planner_provider_transient",
                    message=f"{type(exc).__name__}: {exc}",
                )
                retry = self.retry_failed(failed.attempt_id, now)
                return failed if retry is None else retry
            return self.mark_failed(claim, now, message=f"{type(exc).__name__}: {exc}")
        self.record_response(claim, response, now)
        return self.parse_response(claim.attempt_id, now)

    def get_attempt(self, attempt_id):
        with self.uow_factory() as uow: return uow.planner_attempts.get(attempt_id)

    def list_attempts(self, run_id):
        with self.uow_factory() as uow: return uow.planner_attempts.list_by_run(run_id)

    def list_proposals(self, run_id):
        with self.uow_factory() as uow: return uow.planner_proposals.list_by_run(run_id)

    def diagnostics(self, run_id):
        attempts, proposals = self.list_attempts(run_id), self.list_proposals(run_id)
        return {
            "run_id": str(run_id),
            "attempts": [{
                "attempt_id": str(item.attempt_id), "number": item.attempt_number.value,
                "status": item.status.value, "context_hash": item.context.context_hash.value,
                "model_id": item.model_id, "provider_id": item.provider_id,
                "usage": None if item.usage is None else to_primitive(item.usage),
                "proposal_id": None if item.proposal_id is None else str(item.proposal_id),
                "error": item.error,
            } for item in attempts],
            "proposals": [{
                "proposal_id": str(item.proposal.proposal_id),
                "status": item.status.value, "action": item.proposal.action.kind.value,
                "content_hash": item.proposal.content_hash.value,
            } for item in proposals],
            "waiting_reason": "planner_wait" if any(item.status in {PlannerAttemptStatus.REQUESTED, PlannerAttemptStatus.RUNNING, PlannerAttemptStatus.RESPONSE_RECEIVED} for item in attempts) else None,
        }
