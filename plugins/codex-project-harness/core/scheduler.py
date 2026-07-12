"""Task scheduling and dependency consistency rules."""

from __future__ import annotations

import sqlite3
from typing import Callable


def current_cycle_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("select current_cycle_id from project where id = 1").fetchone()
    return str(row["current_cycle_id"] if row else "")


def dependency_blockers(conn: sqlite3.Connection, task_id: str, cycle_id: str = "") -> list[str]:
    cycle_id = cycle_id or current_cycle_id(conn)
    return [
        f"{row['depends_on']}={row['status']}"
        for row in conn.execute(
            """
            select d.depends_on, t.status from task_dependencies d
            join tasks t on t.cycle_id = d.cycle_id and t.id = d.depends_on
            where d.cycle_id = ? and d.task_id = ? and t.status != 'accepted'
            order by d.depends_on
            """,
            (cycle_id, task_id),
        )
    ]


def assert_no_dependency_cycle(
    conn: sqlite3.Connection,
    task_id: str,
    depends_on: str,
    *,
    cycle_id: str = "",
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    cycle_id = cycle_id or current_cycle_id(conn)
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
            for row in conn.execute(
                "select depends_on from task_dependencies where cycle_id = ? and task_id = ?",
                (cycle_id, current),
            )
        )


def require_task_runnable(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    if row["status"] != "planned":
        raise error_factory(f"task status is not runnable: {row['id']} status={row['status']}")
    blockers = dependency_blockers(conn, row["id"], row["cycle_id"])
    if blockers:
        raise error_factory(f"task dependencies are not accepted: {row['id']} blockers={', '.join(blockers)}")
