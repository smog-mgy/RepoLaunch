from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select, update


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Small durable task store. SQLite by default; MySQL via DATABASE_URL."""

    def __init__(self, data_dir: Path, database_url: str = "", *, initialize: bool = True):
        data_dir.mkdir(parents=True, exist_ok=True)
        url = database_url or f"sqlite:///{(data_dir / 'tasks.sqlite3').as_posix()}"
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite:") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kwargs)
        self.backend = self.engine.dialect.name
        self.lock = threading.RLock()
        metadata = MetaData()
        self.table = Table("repolaunch_tasks", metadata, Column("id", String(32), primary_key=True), Column("data", Text().with_variant(LONGTEXT(), "mysql"), nullable=False))
        if initialize:
            metadata.create_all(self.engine)

    def get(self, task_id: str) -> dict:
        with self.lock, self.engine.connect() as conn:
            value = conn.execute(select(self.table.c.data).where(self.table.c.id == task_id)).scalar_one_or_none()
        if value is None:
            raise KeyError(task_id)
        return json.loads(value)

    def list(self) -> list[dict]:
        with self.lock, self.engine.connect() as conn:
            rows = conn.execute(select(self.table.c.data)).scalars().all()
        return sorted((json.loads(row) for row in rows), key=lambda x: x["created_at"], reverse=True)[:100]

    def create(self, task: dict) -> dict:
        with self.lock, self.engine.begin() as conn:
            conn.execute(self.table.insert().values(id=task["id"], data=json.dumps(task, ensure_ascii=False)))
        return task

    def mutate(self, task_id: str, transform: Callable[[dict], None]) -> dict:
        with self.lock:
            task = self.get(task_id)
            transform(task)
            task["updated_at"] = now()
            with self.engine.begin() as conn:
                conn.execute(update(self.table).where(self.table.c.id == task_id).values(data=json.dumps(task, ensure_ascii=False)))
            return task

    def close(self) -> None:
        self.engine.dispose()
