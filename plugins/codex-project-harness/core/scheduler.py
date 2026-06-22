"""Task scheduling and dependency consistency rules."""

from __future__ import annotations

import sqlite3
from typing import Callable


def dependency_blockers(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return [
        f"{row['depends_on']}={row['status']}"
        for row in conn.execute(
            """
            select d.depends_on, t.status from task_dependencies d
            join tasks t on t.id = d.depends_on
            where d.task_id = ? and t.status != 'accepted'
            order by d.depends_on
            """,
            (task_id,),
        )
    ]


def assert_no_dependency_cycle(
    conn: sqlite3.Connection,
    task_id: str,
    depends_on: str,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    stack = [depends_on]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == task_id:
            raise error_factory(f"dependency cycle detected for {task_id}")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(
            row["depends_on"]
            for row in conn.execute("select depends_on from task_dependencies where task_id = ?", (current,))
        )


def require_task_runnable(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    if row["status"] not in {"ready", "claimed"}:
        raise error_factory(f"task status is not runnable: {row['id']} status={row['status']}")
    blockers = dependency_blockers(conn, row["id"])
    if blockers:
        raise error_factory(f"task dependencies are not accepted: {row['id']} blockers={', '.join(blockers)}")


def ready_queue(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("select id from tasks where status = 'ready' order by id").fetchall()
    ready: list[str] = []
    for row in rows:
        if not dependency_blockers(conn, row["id"]):
            ready.append(row["id"])
    return ready
