from __future__ import annotations

import base64
import gzip
import sqlite3
from contextlib import closing
from pathlib import Path


def create_schema28_fixture(root: Path) -> None:
    path = root / ".ai-team/state/harness.db"
    path.parent.mkdir(parents=True)
    encoded = (Path(__file__).parent / "fixtures/schema28-development.sql.gz.b64").read_bytes()
    sql = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(sql)
        conn.commit()
