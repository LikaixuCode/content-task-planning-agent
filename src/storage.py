import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import PlanningRequest


class RequestConflict(ValueError):
    """同一 request_id 对应了不同的请求内容。"""


class PlanningStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS requests ("
            "request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, start_at TEXT NOT NULL, "
            "state TEXT NOT NULL, last_output TEXT)"
        )

    def close(self):
        self.connection.close()

    def register(self, request: PlanningRequest) -> tuple[dict, bool]:
        payload = request.canonical()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request.request_id,)
            ).fetchone()
            if row:
                if row["payload"] != payload:
                    raise RequestConflict("相同 request_id 对应不同请求内容")
                return self._decode(row), False
            start_at = request.start_at or datetime.now(timezone.utc)
            state = {"materials": None, "estimates": {}, "scripts": {}, "events": [], "faults": [], "plan": None}
            self.connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, NULL)",
                (request.request_id, payload, start_at.isoformat(), json.dumps(state, ensure_ascii=False)),
            )
            row = self.connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request.request_id,)
            ).fetchone()
            return self._decode(row), True

    def get(self, request_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return self._decode(row) if row else None

    def save_state(self, request_id: str, state: dict):
        with self.connection:
            self.connection.execute(
                "UPDATE requests SET state = ?, last_output = NULL WHERE request_id = ?",
                (json.dumps(state, ensure_ascii=False), request_id),
            )

    def save_output(self, request_id: str, state: dict, output: dict):
        with self.connection:
            self.connection.execute(
                "UPDATE requests SET state = ?, last_output = ? WHERE request_id = ?",
                (json.dumps(state, ensure_ascii=False), json.dumps(output, ensure_ascii=False), request_id),
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        return {
            "request_id": row["request_id"],
            "start_at": row["start_at"],
            "state": json.loads(row["state"]),
            "last_output": json.loads(row["last_output"]) if row["last_output"] else None,
        }
