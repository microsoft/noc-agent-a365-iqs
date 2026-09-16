"""Disabled-by-default polling monitor for newly detected Fabric incidents."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.storage.blob.aio import BlobLeaseClient, BlobServiceClient

logger = logging.getLogger(__name__)

STATE_BLOB = "monitor/state.json"
SUBSCRIPTION_BLOB = "monitor/subscription.json"
LEASE_BLOB = "monitor/leader.lock"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _kql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclasses.dataclass(frozen=True, order=True)
class Cursor:
    timestamp: str
    incident_id: str

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "Cursor":
        return cls(_iso(event["timestamp"]), str(event["incident_id"]))


@dataclasses.dataclass(frozen=True)
class MonitorConfig:
    enabled: bool
    query_uri: str
    database: str
    storage_account_name: str
    container_name: str = "agent-state"
    poll_seconds: float = 30.0
    first_run_lookback_minutes: int = 15
    max_catchup_minutes: int = 120
    overlap_seconds: int = 30
    batch_size: int = 20
    max_attempts: int = 3
    lease_seconds: int = 60
    specialist_concurrency: int = 3

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        enabled = os.getenv("INCIDENT_MONITOR_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        config = cls(
            enabled=enabled,
            query_uri=os.getenv("FABRIC_KQL_QUERY_URI", "").strip(),
            database=os.getenv("FABRIC_KQL_DATABASE_NAME", "").strip(),
            storage_account_name=os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "").strip(),
            container_name=os.getenv("AGENT_STATE_CONTAINER_NAME", "agent-state").strip(),
            poll_seconds=float(os.getenv("INCIDENT_MONITOR_POLL_SECONDS", "30")),
            first_run_lookback_minutes=int(os.getenv("INCIDENT_MONITOR_FIRST_LOOKBACK_MINUTES", "15")),
            max_catchup_minutes=int(os.getenv("INCIDENT_MONITOR_MAX_CATCHUP_MINUTES", "120")),
            overlap_seconds=int(os.getenv("INCIDENT_MONITOR_OVERLAP_SECONDS", "30")),
            batch_size=int(os.getenv("INCIDENT_MONITOR_BATCH_SIZE", "20")),
            max_attempts=int(os.getenv("INCIDENT_MONITOR_MAX_ATTEMPTS", "3")),
            lease_seconds=int(os.getenv("INCIDENT_MONITOR_LEASE_SECONDS", "60")),
            specialist_concurrency=int(os.getenv("INCIDENT_MONITOR_SPECIALIST_CONCURRENCY", "3")),
        )
        if enabled:
            missing = [
                name
                for name, value in (
                    ("FABRIC_KQL_QUERY_URI", config.query_uri),
                    ("FABRIC_KQL_DATABASE_NAME", config.database),
                    ("AZURE_STORAGE_ACCOUNT_NAME", config.storage_account_name),
                    ("AGENT_STATE_CONTAINER_NAME", config.container_name),
                )
                if not value
            ]
            if missing:
                raise ValueError("Incident monitor is enabled but required settings are missing: " + ", ".join(missing))
            if config.batch_size < 1 or config.max_attempts < 1:
                raise ValueError("INCIDENT_MONITOR_BATCH_SIZE and INCIDENT_MONITOR_MAX_ATTEMPTS must be positive")
            if not 15 <= config.lease_seconds <= 60:
                raise ValueError("INCIDENT_MONITOR_LEASE_SECONDS must be between 15 and 60")
        return config


def build_incident_query(config: MonitorConfig, cursor: Optional[Cursor], now: datetime) -> str:
    """Build stable, bounded KQL ordered by the compound cursor."""
    now = _as_utc(now)
    if cursor:
        cursor_time = _as_utc(cursor.timestamp)
        lower = max(
            cursor_time - timedelta(seconds=config.overlap_seconds),
            now - timedelta(minutes=config.max_catchup_minutes),
        )
        cursor_filter = (
            f"| where Timestamp > datetime({_iso(cursor_time)}) "
            f"or (Timestamp == datetime({_iso(cursor_time)}) and IncidentId > {_kql_string(cursor.incident_id)})"
        )
    else:
        lower = now - timedelta(minutes=config.first_run_lookback_minutes)
        cursor_filter = ""
    return "\n".join(
        [
            "IncidentEvents",
            "| where Stage == 'Detected'",
            f"| where Timestamp >= datetime({_iso(lower)}) and Timestamp <= datetime({_iso(now)})",
            cursor_filter,
            "| project Timestamp, IncidentId",
            "| order by Timestamp asc, IncidentId asc",
            f"| take {config.batch_size}",
        ]
    ).replace("\n\n", "\n")


class BlobRepository:
    """JSON state repository and leader lease over one private blob container."""

    def __init__(self, account_name: str, container_name: str, credential: Any):
        endpoint = f"https://{account_name}.blob.core.windows.net"
        self._service = BlobServiceClient(endpoint, credential=credential)
        self._container = self._service.get_container_client(container_name)

    async def initialize(self) -> None:
        try:
            await self._container.create_container()
        except ResourceExistsError:
            pass

    async def read_json(self, name: str) -> Optional[dict[str, Any]]:
        blob = self._container.get_blob_client(name)
        if not await blob.exists():
            return None
        payload = await (await blob.download_blob()).readall()
        return json.loads(payload)

    async def write_json(self, name: str, value: dict[str, Any]) -> None:
        await self._container.upload_blob(
            name, json.dumps(value, separators=(",", ":"), sort_keys=True), overwrite=True
        )

    async def delete(self, name: str) -> None:
        try:
            await self._container.delete_blob(name, delete_snapshots="include")
        except ResourceNotFoundError:
            pass

    async def acquire_lease(self, duration: int) -> BlobLeaseClient:
        blob = self._container.get_blob_client(LEASE_BLOB)
        try:
            await blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        lease = BlobLeaseClient(blob)
        await lease.acquire(lease_duration=duration)
        return lease

    async def close(self) -> None:
        await self._service.close()


class BlobAgentsStorage:
    """Small implementation of the installed Agents SDK ``Storage`` protocol."""

    def __init__(self, repository: BlobRepository):
        self._repository = repository

    @staticmethod
    def _name(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"agents/{digest}.json"

    async def read(self, keys: list[str], *, target_cls=None, **_kwargs) -> dict[str, Any]:
        if not keys or target_cls is None:
            raise ValueError("Storage.read requires keys and target_cls")
        result = {}
        for key in keys:
            if not key:
                raise ValueError("Storage key cannot be empty")
            value = await self._repository.read_json(self._name(key))
            if value is not None:
                result[key] = target_cls.from_json_to_store_item(value)
        return result

    async def write(self, changes: dict[str, Any]) -> None:
        if not changes:
            raise ValueError("Storage.write requires changes")
        for key, value in changes.items():
            if not key:
                raise ValueError("Storage key cannot be empty")
            await self._repository.write_json(self._name(key), value.store_item_to_json())

    async def delete(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("Storage.delete requires keys")
        for key in keys:
            if not key:
                raise ValueError("Storage key cannot be empty")
            await self._repository.delete(self._name(key))


class KustoIncidentSource:
    def __init__(self, query_uri: str, database: str, credential: Any):
        builder = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, credential)
        self._client = KustoClient(builder)
        self._database = database

    async def poll(self, query: str) -> list[dict[str, str]]:
        response = await asyncio.to_thread(self._client.execute_query, self._database, query)
        table = response.primary_results[0]
        events = [
            {"timestamp": _iso(row["Timestamp"]), "incident_id": str(row["IncidentId"])}
            for row in table
        ]
        return sorted(events, key=lambda item: Cursor.from_event(item))

    def close(self) -> None:
        self._client.close()


class IncidentMonitor:
    """Single-leader at-least-once monitor with durable pending work."""

    def __init__(
        self,
        config: MonitorConfig,
        repository: Any,
        source: Any,
        handler: Callable[[dict[str, str], dict[str, str]], Awaitable[bool]],
        *,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config = config
        self.repository = repository
        self.source = source
        self.handler = handler
        self.now = now
        self.sleep = sleep
        self._task: Optional[asyncio.Task] = None
        self._lease_lost = asyncio.Event()
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[str] = None
        self.is_leader = False

    @classmethod
    def create(cls, config: MonitorConfig, handler) -> "IncidentMonitor":
        blob_credential = AsyncDefaultAzureCredential()
        kusto_credential = DefaultAzureCredential()
        repository = BlobRepository(
            config.storage_account_name, config.container_name, blob_credential
        )
        source = KustoIncidentSource(config.query_uri, config.database, kusto_credential)
        monitor = cls(config, repository, source, handler)
        monitor._blob_credential = blob_credential
        monitor._kusto_credential = kusto_credential
        return monitor

    async def start(self) -> None:
        if not self.config.enabled or self._task:
            return
        await self.repository.initialize()
        self._task = asyncio.create_task(self.run(), name="incident-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        close = getattr(self.source, "close", None)
        if close:
            close()
        await self.repository.close()
        blob_credential = getattr(self, "_blob_credential", None)
        if blob_credential:
            await blob_credential.close()
        kusto_credential = getattr(self, "_kusto_credential", None)
        if kusto_credential:
            kusto_credential.close()

    async def _renew(self, lease: Any) -> None:
        try:
            while True:
                await self.sleep(self.config.lease_seconds / 2)
                await lease.renew()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"leader lease lost: {type(exc).__name__}"
            self._lease_lost.set()

    async def run(self) -> None:
        lease = None
        renew_task = None
        try:
            lease = await self.repository.acquire_lease(self.config.lease_seconds)
            self.is_leader = True
            renew_task = asyncio.create_task(self._renew(lease), name="incident-monitor-lease")
            while not self._lease_lost.is_set():
                await self.run_once()
                try:
                    await asyncio.wait_for(
                        self._lease_lost.wait(), timeout=self.config.poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        except (ResourceExistsError, ResourceModifiedError):
            logger.info("Incident monitor standby: another instance owns the leader lease")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Incident monitor stopped: %s", self.last_error)
        finally:
            self.is_leader = False
            if renew_task:
                renew_task.cancel()
                try:
                    await renew_task
                except asyncio.CancelledError:
                    pass
            if lease:
                try:
                    await lease.release()
                except Exception:
                    logger.warning("Incident monitor leader lease could not be released")

    async def run_once(self) -> None:
        state = await self.repository.read_json(STATE_BLOB) or {
            "cursor": None,
            "pending": {},
            "dead_letters": [],
        }
        subscription = await self.repository.read_json(SUBSCRIPTION_BLOB)
        if not subscription:
            self.last_poll_at = _iso(self.now())
            return

        pending = state.get("pending", {})
        for key in sorted(pending, key=lambda item: Cursor.from_event(pending[item]["event"])):
            if self._lease_lost.is_set():
                return
            if not await self._process(key, state, subscription):
                return

        cursor = Cursor(**state["cursor"]) if state.get("cursor") else None
        query = build_incident_query(self.config, cursor, self.now())
        events = sorted(await self.source.poll(query), key=Cursor.from_event)
        for event in events:
            if self._lease_lost.is_set():
                return
            event_cursor = Cursor.from_event(event)
            if cursor and event_cursor <= cursor:
                continue
            key = base64.urlsafe_b64encode(
                f"{event_cursor.timestamp}\n{event_cursor.incident_id}".encode()
            ).decode().rstrip("=")
            state["pending"][key] = {"event": event, "attempts": 0}
            await self.repository.write_json(STATE_BLOB, state)
            if not await self._process(key, state, subscription):
                return
            cursor = event_cursor
        self.last_poll_at = _iso(self.now())
        self.last_error = None

    async def _process(self, key: str, state: dict, subscription: dict) -> bool:
        item = state["pending"][key]
        try:
            successful = await self.handler(item["event"], subscription)
            if not successful:
                raise RuntimeError("investigation incomplete")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            item["attempts"] += 1
            if item["attempts"] < self.config.max_attempts:
                await self.repository.write_json(STATE_BLOB, state)
                self.last_error = f"incident {item['event']['incident_id']} will retry ({type(exc).__name__})"
                return False
            event_cursor = Cursor.from_event(item["event"])
            state["dead_letters"].append(
                {
                    "timestamp": event_cursor.timestamp,
                    "incident_id": event_cursor.incident_id,
                    "attempts": item["attempts"],
                    "error_type": type(exc).__name__,
                }
            )
            state["cursor"] = dataclasses.asdict(event_cursor)
            del state["pending"][key]
            await self.repository.write_json(STATE_BLOB, state)
            self.last_error = f"incident {event_cursor.incident_id} dead-lettered"
            return True

        if self._lease_lost.is_set():
            return False
        event_cursor = Cursor.from_event(item["event"])
        state["cursor"] = dataclasses.asdict(event_cursor)
        del state["pending"][key]
        await self.repository.write_json(STATE_BLOB, state)
        return True

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "running": bool(self._task and not self._task.done()),
            "leader": self.is_leader,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
        }


async def save_subscription(repository: Any, conversation_id: str, user_id: str, user_name: str) -> None:
    if not conversation_id or not user_id:
        raise ValueError("A conversation and authorized user are required")
    await repository.write_json(
        SUBSCRIPTION_BLOB,
        {
            "conversation_id": conversation_id,
            "authorized_user_id": user_id,
            "authorized_user_name": user_name,
        },
    )


async def remove_subscription(repository: Any, conversation_id: str, user_id: str) -> bool:
    existing = await repository.read_json(SUBSCRIPTION_BLOB)
    if not existing:
        return False
    if (
        existing.get("conversation_id") != conversation_id
        or existing.get("authorized_user_id") != user_id
    ):
        raise PermissionError("Only the subscribed conversation and user can unsubscribe")
    await repository.delete(SUBSCRIPTION_BLOB)
    return True
