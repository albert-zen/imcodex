from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Literal

from .models import OutboundMessage


_SCHEMA_VERSION = 1
_MAX_PENDING = 1024
_MAX_OUTCOMES = 512


class DeliveryOutboxConflict(ValueError):
    pass


class DeliveryOutboxCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    delivery_id: str
    fingerprint: str
    message: OutboundMessage
    sequence: int
    attempts: int
    last_error: str


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivery_id: str
    fingerprint: str
    message: OutboundMessage
    delivered: bool
    durable: bool
    error_kind: str
    error: str


@dataclass(frozen=True, slots=True)
class DeliveryStage:
    status: Literal["pending", "outcome"]
    pending: PendingDelivery | None = None
    outcome: DeliveryOutcome | None = None


class DeliveryOutbox:
    """Minimal IM-owned payload checkpoint over SDK delivery identity."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = RLock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=10,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize_schema()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"Could not open delivery outbox: {self.path}") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def stage(self, message: OutboundMessage) -> DeliveryStage:
        delivery_id = str(message.metadata.get("delivery_id") or "").strip()
        if not delivery_id:
            raise ValueError("delivery_id is required for durable delivery")
        fingerprint = delivery_fingerprint(message)
        serialized = _serialize_message(message)
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            outcome = self._connection.execute(
                "SELECT * FROM outcomes WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if outcome is not None:
                self._require_fingerprint(delivery_id, fingerprint, outcome["fingerprint"])
                return DeliveryStage(status="outcome", outcome=_outcome_from_row(outcome))
            pending = self._connection.execute(
                "SELECT * FROM pending WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if pending is not None:
                self._require_fingerprint(delivery_id, fingerprint, pending["fingerprint"])
                return DeliveryStage(status="pending", pending=_pending_from_row(pending))
            count = self._connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
            if count >= _MAX_PENDING:
                raise DeliveryOutboxCapacityError("durable delivery outbox is at capacity")
            cursor = self._connection.execute(
                """
                INSERT INTO pending(
                    delivery_id, fingerprint, payload_json, created_at, attempts, last_error
                ) VALUES (?, ?, ?, ?, 0, '')
                """,
                (delivery_id, fingerprint, serialized, self.clock()),
            )
            return DeliveryStage(
                status="pending",
                pending=PendingDelivery(
                    delivery_id=delivery_id,
                    fingerprint=fingerprint,
                    message=_deserialize_message(serialized),
                    sequence=int(cursor.lastrowid),
                    attempts=0,
                    last_error="",
                ),
            )

    def list_pending(self) -> list[PendingDelivery]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM pending ORDER BY sequence"
            ).fetchall()
        return [_pending_from_row(row) for row in rows]

    def get_outcome(self, delivery_id: str) -> DeliveryOutcome | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM outcomes WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return None if row is None else _outcome_from_row(row)

    def record_retry(
        self,
        delivery_id: str,
        message: OutboundMessage,
        *,
        error: str = "",
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                UPDATE pending
                SET payload_json = ?, attempts = attempts + 1, last_error = ?
                WHERE delivery_id = ?
                """,
                (_serialize_message(message), error[:500], delivery_id),
            )

    def complete(
        self,
        delivery_id: str,
        message: OutboundMessage,
        *,
        delivered: bool,
        durable: bool,
        error_kind: str = "",
        error: str = "",
    ) -> DeliveryOutcome:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            pending = self._connection.execute(
                "SELECT fingerprint FROM pending WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if pending is None:
                existing = self._connection.execute(
                    "SELECT * FROM outcomes WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if existing is None:
                    raise KeyError(delivery_id)
                return _outcome_from_row(existing)
            result = json.dumps(
                {
                    "message": json.loads(_serialize_message(message)),
                    "delivered": bool(delivered),
                    "durable": bool(durable),
                    "error_kind": error_kind,
                    "error": error[:500],
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._connection.execute(
                """
                INSERT INTO outcomes(
                    delivery_id, fingerprint, outcome_json, completed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (delivery_id, pending["fingerprint"], result, self.clock()),
            )
            self._connection.execute(
                "DELETE FROM pending WHERE delivery_id = ?",
                (delivery_id,),
            )
            self._connection.execute(
                """
                DELETE FROM outcomes
                WHERE sequence NOT IN (
                    SELECT sequence FROM outcomes ORDER BY sequence DESC LIMIT ?
                )
                """,
                (_MAX_OUTCOMES,),
            )
            row = self._connection.execute(
                "SELECT * FROM outcomes WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return _outcome_from_row(row)

    def referenced_artifact_paths(self) -> set[str]:
        paths: set[str] = set()
        for pending in self.list_pending():
            paths.update(artifact.local_path for artifact in pending.message.artifacts)
        return paths

    def health(self) -> dict[str, object]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS pending_count, MIN(created_at) AS oldest,
                       MAX(attempts) AS max_attempts
                FROM pending
                """
            ).fetchone()
        count = int(row["pending_count"] or 0)
        return {
            "status": "degraded" if count else "healthy",
            "pending_count": count,
            "oldest_pending_at": row["oldest"],
            "max_attempts": int(row["max_attempts"] or 0),
        }

    def _initialize_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, _SCHEMA_VERSION}:
            raise RuntimeError(f"Unsupported delivery outbox schema version: {version}")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outcomes(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @staticmethod
    def _require_fingerprint(delivery_id: str, actual: str, expected: str) -> None:
        if actual != expected:
            raise DeliveryOutboxConflict(
                f"delivery_id {delivery_id!r} is already reserved for a different target or payload"
            )


def delivery_fingerprint(message: OutboundMessage) -> str:
    identity = {
        "channel_id": message.channel_id,
        "conversation_id": message.conversation_id,
        "message_type": message.message_type,
        "text": message.text,
        "request_id": message.request_id,
        "metadata": dict(message.metadata),
        "artifacts": [
            {
                "kind": artifact.kind,
                "content_type": artifact.content_type,
                "filename": artifact.filename,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for artifact in message.artifacts
        ],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _serialize_message(message: OutboundMessage) -> str:
    return json.dumps(
        {
            "channel_id": message.channel_id,
            "conversation_id": message.conversation_id,
            "message_type": message.message_type,
            "text": message.text,
            "request_id": message.request_id,
            "metadata": dict(message.metadata),
            "artifacts": [asdict(artifact) for artifact in message.artifacts],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_message(value: str) -> OutboundMessage:
    payload = json.loads(value)
    return OutboundMessage(**payload)


def _pending_from_row(row: sqlite3.Row) -> PendingDelivery:
    return PendingDelivery(
        delivery_id=str(row["delivery_id"]),
        fingerprint=str(row["fingerprint"]),
        message=_deserialize_message(str(row["payload_json"])),
        sequence=int(row["sequence"]),
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]),
    )


def _outcome_from_row(row: sqlite3.Row) -> DeliveryOutcome:
    payload = json.loads(str(row["outcome_json"]))
    return DeliveryOutcome(
        delivery_id=str(row["delivery_id"]),
        fingerprint=str(row["fingerprint"]),
        message=OutboundMessage(**payload["message"]),
        delivered=bool(payload["delivered"]),
        durable=bool(payload["durable"]),
        error_kind=str(payload.get("error_kind") or ""),
        error=str(payload.get("error") or ""),
    )
