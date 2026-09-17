"""Standalone mocked self-checks for durable detected-incident polling."""

import asyncio
import copy
from datetime import datetime, timezone

from incident_monitor import (
    STATE_BLOB,
    Cursor,
    IncidentMonitor,
    MonitorConfig,
    build_incident_query,
    remove_subscription,
    save_subscription,
)


class FakeRepository:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.closed = False

    async def initialize(self):
        pass

    async def read_json(self, name):
        return copy.deepcopy(self.values.get(name))

    async def write_json(self, name, value):
        self.values[name] = copy.deepcopy(value)
        self.writes.append((name, copy.deepcopy(value)))

    async def delete(self, name):
        self.values.pop(name, None)

    async def close(self):
        self.closed = True


class FakeSource:
    def __init__(self, events):
        self.events = events
        self.queries = []

    async def poll(self, query):
        self.queries.append(query)
        return list(self.events)

    def close(self):
        pass


def config(**changes):
    values = dict(
        enabled=True,
        query_uri="https://query.invalid",
        database="db",
        storage_account_name="storage",
        poll_seconds=0,
        max_attempts=2,
    )
    values.update(changes)
    return MonitorConfig(**values)


async def check_cursor_query_and_pending():
    now = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    cursor = Cursor("2026-09-16T11:59:00.000000Z", "INC'OOPS")
    query = build_incident_query(config(), cursor, now)
    assert "Stage == 'Detected'" in query
    assert "strcmp(IncidentId, 'INC''OOPS') > 0" in query
    assert "project Timestamp, IncidentId, Detail" in query
    assert "order by Timestamp asc, IncidentId asc" in query
    assert query.index("Timestamp >") < query.index("order by")

    repository = FakeRepository()
    source = FakeSource(
        [
            {"timestamp": "2026-09-16T12:00:00Z", "incident_id": "INC-002"},
            {"timestamp": "2026-09-16T12:00:00Z", "incident_id": "INC-001"},
        ]
    )
    observed = []

    async def handler(event, subscription):
        persisted = repository.values[STATE_BLOB]
        assert persisted["pending"]
        assert persisted["cursor"] is None or persisted["cursor"]["incident_id"] != event["incident_id"]
        observed.append(event["incident_id"])
        return True

    await save_subscription(repository, "conversation", "user", "Operator")
    monitor = IncidentMonitor(config(), repository, source, handler, now=lambda: now)
    await monitor.run_once()
    assert observed == ["INC-001", "INC-002"]
    assert repository.values[STATE_BLOB]["cursor"]["incident_id"] == "INC-002"
    assert not repository.values[STATE_BLOB]["pending"]


async def check_resume_retry_dead_letter_and_subscription():
    repository = FakeRepository()
    event = {"timestamp": "2026-09-16T12:00:00Z", "incident_id": "INC-RETRY"}
    repository.values[STATE_BLOB] = {
        "cursor": None,
        "pending": {"pending-key": {"event": event, "attempts": 0}},
        "dead_letters": [],
    }
    await save_subscription(repository, "conversation", "user", "Operator")
    assert repository.values["monitor/subscription.json"]["conversation_id"] == "conversation"
    attempts = 0

    async def failing_handler(_event, _subscription):
        nonlocal attempts
        attempts += 1
        return False

    monitor = IncidentMonitor(config(), repository, FakeSource([]), failing_handler)
    await monitor.run_once()
    assert repository.values[STATE_BLOB]["pending"]["pending-key"]["attempts"] == 1
    assert repository.values[STATE_BLOB]["cursor"] is None
    await monitor.run_once()
    state = repository.values[STATE_BLOB]
    assert attempts == 2 and not state["pending"]
    assert state["cursor"]["incident_id"] == "INC-RETRY"
    assert state["dead_letters"][0]["error_type"] == "RuntimeError"
    try:
        await remove_subscription(repository, "other", "user")
        raise AssertionError("mismatched conversation must not unsubscribe")
    except PermissionError:
        pass
    assert await remove_subscription(repository, "conversation", "user")


async def check_cancel_and_lease_loss():
    class Lease:
        def __init__(self):
            self.released = False

        async def renew(self):
            raise RuntimeError("lost")

        async def release(self):
            self.released = True

    class LeaseRepository(FakeRepository):
        async def acquire_lease(self, _duration):
            self.lease = Lease()
            return self.lease

    async def short_sleep(_seconds):
        await asyncio.sleep(0)

    repository = LeaseRepository()
    monitor = IncidentMonitor(
        config(lease_seconds=15),
        repository,
        FakeSource([]),
        lambda *_args: asyncio.sleep(0, result=True),
        sleep=short_sleep,
    )
    await monitor.run()
    assert monitor._lease_lost.is_set()
    assert repository.lease.released

    never = asyncio.Event()

    async def blocking_sleep(_seconds):
        await never.wait()

    repository = LeaseRepository()
    monitor = IncidentMonitor(
        config(),
        repository,
        FakeSource([]),
        lambda *_args: asyncio.sleep(0, result=True),
        sleep=blocking_sleep,
    )
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert repository.lease.released


async def run():
    await check_cursor_query_and_pending()
    await check_resume_retry_dead_letter_and_subscription()
    await check_cancel_and_lease_loss()


if __name__ == "__main__":
    asyncio.run(run())
    print("PASS: test_incident_monitor.py self-check passed")
