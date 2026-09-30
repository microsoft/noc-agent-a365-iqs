"""Approval gate every write/action tool must pass before doing anything.

Ported from pranabpaul-tech/pdm-fabric-foundry (actions/approval.py). Their
local-file store did not survive a hosted container, so this one persists to
the same Blob container the incident monitor already uses
(AGENT_STATE_CONTAINER_NAME, prefix ``approvals/``).

Contract:
    approval_id = gate.request("broadcast_email", "Send P1 update", payload)
    ...operator approves/denies in Teams -> gate.decide(approval_id, True, user)
    gate.require_approved(approval_id)   # raises ApprovalError unless approved & unexpired

No tool in this repo is gated yet; wire new write tools through require_approved().
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

APPROVAL_TTL = timedelta(days=3)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ApprovalError(RuntimeError):
    pass


class ApprovalStore(Protocol):
    def get(self, approval_id: str) -> Optional[dict]: ...
    def put(self, approval_id: str, record: dict) -> None: ...


class BlobApprovalStore:
    """Stores one JSON blob per approval under approvals/<id>.json."""

    def __init__(self, account_name: Optional[str] = None, container: Optional[str] = None, credential: Any = None):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account = account_name or os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        service = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential or DefaultAzureCredential())
        self._container = service.get_container_client(container or os.getenv("AGENT_STATE_CONTAINER_NAME", "agent-state"))

    def get(self, approval_id: str) -> Optional[dict]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self._container.download_blob(f"approvals/{approval_id}.json").readall())
        except ResourceNotFoundError:
            return None

    def put(self, approval_id: str, record: dict) -> None:
        self._container.upload_blob(f"approvals/{approval_id}.json", json.dumps(record), overwrite=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalGate:
    def __init__(self, store: ApprovalStore):
        self._store = store

    def request(self, action_type: str, description: str, payload: dict, requested_by: str = "noc-agent") -> str:
        approval_id = uuid.uuid4().hex
        self._store.put(approval_id, {
            "id": approval_id,
            "actionType": action_type,
            "description": description,
            "payload": payload,
            "requestedBy": requested_by,
            "status": ApprovalStatus.PENDING.value,
            "requestedAt": _now().isoformat(),
            "expiresAt": (_now() + APPROVAL_TTL).isoformat(),
        })
        return approval_id

    def status(self, approval_id: str) -> ApprovalStatus:
        return _effective_status(self._require(approval_id))

    def decide(self, approval_id: str, approved: bool, decided_by: str) -> None:
        record = self._require(approval_id)
        current = _effective_status(record)
        if current != ApprovalStatus.PENDING:
            raise ApprovalError(f"Approval {approval_id} is no longer pending (status={current.value})")
        record.update({
            "status": (ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED).value,
            "decidedBy": decided_by,
            "decidedAt": _now().isoformat(),
        })
        self._store.put(approval_id, record)

    def require_approved(self, approval_id: str) -> dict:
        record = self._require(approval_id)
        current = _effective_status(record)
        if current != ApprovalStatus.APPROVED:
            raise ApprovalError(f"Approval {approval_id} is not approved (status={current.value}); refusing to act.")
        return record

    def _require(self, approval_id: str) -> dict:
        record = self._store.get(approval_id)
        if record is None:
            raise ApprovalError(f"No such approval: {approval_id}")
        return record


def _effective_status(record: dict) -> ApprovalStatus:
    status = ApprovalStatus(record["status"])
    if status == ApprovalStatus.PENDING and datetime.fromisoformat(record["expiresAt"]) < _now():
        return ApprovalStatus.EXPIRED
    return status
