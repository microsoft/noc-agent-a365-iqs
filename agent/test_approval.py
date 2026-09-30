"""Offline checks for the approval gate."""

from datetime import datetime, timedelta, timezone

import pytest

import approval
from approval import ApprovalError, ApprovalGate, ApprovalStatus


class DictStore:
    def __init__(self):
        self.items = {}

    def get(self, approval_id):
        return self.items.get(approval_id)

    def put(self, approval_id, record):
        self.items[approval_id] = dict(record)


def test_approve_then_act():
    gate = ApprovalGate(DictStore())
    approval_id = gate.request("broadcast_email", "P1 update", {"to": ["exec"]})
    with pytest.raises(ApprovalError):
        gate.require_approved(approval_id)
    gate.decide(approval_id, True, "operator@contoso.com")
    assert gate.require_approved(approval_id)["decidedBy"] == "operator@contoso.com"


def test_denied_and_double_decision_refused():
    gate = ApprovalGate(DictStore())
    approval_id = gate.request("broadcast_email", "P1 update", {})
    gate.decide(approval_id, False, "operator")
    with pytest.raises(ApprovalError):
        gate.require_approved(approval_id)
    with pytest.raises(ApprovalError):
        gate.decide(approval_id, True, "operator")


def test_pending_expires(monkeypatch):
    gate = ApprovalGate(DictStore())
    approval_id = gate.request("broadcast_email", "P1 update", {})
    later = datetime.now(timezone.utc) + approval.APPROVAL_TTL + timedelta(minutes=1)
    monkeypatch.setattr(approval, "_now", lambda: later)
    assert gate.status(approval_id) == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError):
        gate.decide(approval_id, True, "operator")


def test_unknown_id():
    with pytest.raises(ApprovalError):
        ApprovalGate(DictStore()).require_approved("nope")
