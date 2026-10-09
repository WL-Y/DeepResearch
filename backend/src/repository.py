import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


TERMINAL_STATUSES = {"completed", "error", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchRepository:
    """SQLite-backed research state and event history."""

    _UPDATABLE_FIELDS = {
        "status",
        "progress",
        "progress_text",
        "tasks_json",
        "report",
        "error",
    }

    def __init__(self, database_path: str = "./workspace/research.db"):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    progress_text TEXT NOT NULL DEFAULT '',
                    tasks_json TEXT NOT NULL DEFAULT '[]',
                    report TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    research_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (research_id) REFERENCES research_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_research_events_run_id
                ON research_events(research_id, id);

                CREATE TABLE IF NOT EXISTS research_checkpoints (
                    research_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    results_json TEXT,
                    summary_json TEXT,
                    PRIMARY KEY (research_id, task_id),
                    FOREIGN KEY (research_id) REFERENCES research_runs(id) ON DELETE CASCADE
                );
                """
            )

    def create(self, research_id: str, topic: str) -> Dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_runs (
                    id, topic, status, progress, progress_text,
                    tasks_json, created_at, updated_at
                ) VALUES (?, ?, 'planning', 0, '等待研究任务启动...', '[]', ?, ?)
                """,
                (research_id, topic, now, now),
            )
        return self.get(research_id)

    def get(self, research_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT *, (SELECT COALESCE(MAX(id), 0) FROM research_events
                    WHERE research_id = research_runs.id) AS last_event_id
                    FROM research_runs WHERE id = ?""", (research_id,)
            ).fetchone()
        return self._deserialize_run(row) if row else None

    def list_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM research_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._deserialize_run(row) for row in rows]

    def delete(self, research_id: str) -> bool:
        """Delete a research run and its events through the foreign-key cascade."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM research_runs WHERE id = ?", (research_id,)
            )
            return cursor.rowcount > 0

    def save_checkpoint(self, research_id: str, task_id: int, *, results=None, summary=None):
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO research_checkpoints (research_id, task_id, results_json, summary_json)
                   VALUES (?, ?, ?, ?) ON CONFLICT(research_id, task_id) DO UPDATE SET
                   results_json = COALESCE(excluded.results_json, results_json),
                   summary_json = COALESCE(excluded.summary_json, summary_json)""",
                (research_id, task_id,
                 json.dumps(results, ensure_ascii=False) if results is not None else None,
                 json.dumps(summary, ensure_ascii=False) if summary is not None else None),
            )

    def checkpoints(self, research_id: str) -> Dict[int, dict]:
        with self._connect() as connection:
            rows = connection.execute('SELECT * FROM research_checkpoints WHERE research_id = ?', (research_id,)).fetchall()
        output = {}
        for row in rows:
            try:
                output[row['task_id']] = {
                    'results': json.loads(row['results_json']) if row['results_json'] else None,
                    'summary': json.loads(row['summary_json']) if row['summary_json'] else None,
                }
            except (ValueError, TypeError):
                continue  # A damaged checkpoint must be rebuilt, not trusted.
        return output

    def begin_resume(self, research_id: str) -> int:
        """Claim an interrupted run and append a new SSE replay boundary atomically."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT status, tasks_json FROM research_runs WHERE id = ?', (research_id,)
            ).fetchone()
            if not row:
                raise KeyError(research_id)
            resumable = row['status'] in {'error', 'cancelled'}
            if row['status'] == 'completed':
                try:
                    resumable = any(task.get('status') == 'failed'
                                    for task in json.loads(row['tasks_json']))
                except (TypeError, ValueError):
                    resumable = False
            if not resumable:
                raise ValueError('只有中断的研究或包含失败子任务的研究可以继续')
            changed = connection.execute(
                """UPDATE research_runs SET status = 'resuming', error = NULL,
                   progress_text = '正在恢复上次研究...', updated_at = ? WHERE id = ?""", (now, research_id),
            ).rowcount
            if not changed:
                raise ValueError('只有已中断或已停止的研究可以继续')
            cursor = connection.execute(
                """INSERT INTO research_events (research_id, event_type, data_json, created_at)
                   VALUES (?, 'resumed', ?, ?)""",
                (research_id, json.dumps({'text': '正在恢复上次研究...'}, ensure_ascii=False), now),
            )
            return int(cursor.lastrowid)

    def resume_boundary(self, research_id: str) -> int:
        with self._connect() as connection:
            return connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM research_events WHERE research_id = ? AND event_type = 'resumed'",
                (research_id,),
            ).fetchone()[0]

    def record_event(
        self,
        research_id: str,
        event_type: str,
        data: Dict[str, Any],
        updates: Optional[Dict[str, Any]] = None,
    ) -> int:
        updates = updates or {}
        unknown_fields = set(updates) - self._UPDATABLE_FIELDS
        if unknown_fields:
            raise ValueError(f"不支持的研究字段: {sorted(unknown_fields)}")

        normalized_updates = dict(updates)
        if "tasks_json" in normalized_updates and not isinstance(
            normalized_updates["tasks_json"], str
        ):
            normalized_updates["tasks_json"] = json.dumps(
                normalized_updates["tasks_json"], ensure_ascii=False
            )

        now = utc_now()
        with self._connect() as connection:
            if normalized_updates:
                assignments = ", ".join(
                    f"{field} = ?" for field in normalized_updates
                )
                values = list(normalized_updates.values()) + [now, research_id]
                connection.execute(
                    f"UPDATE research_runs SET {assignments}, updated_at = ? WHERE id = ?",
                    values,
                )

            cursor = connection.execute(
                """
                INSERT INTO research_events (
                    research_id, event_type, data_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (research_id, event_type, json.dumps(data, ensure_ascii=False), now),
            )
            return int(cursor.lastrowid)

    def events_after(self, research_id: str, event_id: int = 0) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, data_json, created_at
                FROM research_events
                WHERE research_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (research_id, event_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "type": row["event_type"],
                "data": json.loads(row["data_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mark_interrupted_runs(self) -> int:
        """Mark jobs interrupted by a backend restart and record replayable errors."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM research_runs
                WHERE status NOT IN ('completed', 'error', 'cancelled')
                """
            ).fetchall()
            now = utc_now()
            for row in rows:
                research_id = row["id"]
                message = "后端服务重启，研究任务已中断，可点击继续研究从已保存进度恢复。"
                connection.execute(
                    """
                    UPDATE research_runs
                    SET status = 'error', error = ?, progress_text = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (message, message, now, research_id),
                )
                connection.execute(
                    """
                    INSERT INTO research_events (
                        research_id, event_type, data_json, created_at
                    ) VALUES (?, 'error', ?, ?)
                    """,
                    (
                        research_id,
                        json.dumps({"message": message}, ensure_ascii=False),
                        now,
                    ),
                )
            return len(rows)

    @staticmethod
    def _deserialize_run(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "topic": row["topic"],
            "status": row["status"],
            "progress": row["progress"],
            "progress_text": row["progress_text"],
            "tasks": json.loads(row["tasks_json"] or "[]"),
            "report": row["report"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_event_id": row['last_event_id'] if 'last_event_id' in row.keys() else 0,
        }
