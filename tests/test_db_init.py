"""db.init: skips an unchanged schema; with a changed schema it gives up on a busy table quickly and retries."""
import os
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from tracker import db

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def connect():
    return psycopg.connect(URL, row_factory=dict_row)


def test_unchanged_schema_is_skipped_even_while_snapshots_is_locked():
    with connect() as c:
        db.init(c)
        assert db.get_maintenance(c, "schema_sha256")
    with connect() as locker, connect() as c:
        locker.execute("lock table snapshots in access exclusive mode")  # a busy scrape, worst case
        t = time.monotonic()
        db.init(c)  # must not touch snapshots at all
        assert time.monotonic() - t < 5
        locker.rollback()


def test_changed_schema_retries_then_raises_on_a_locked_table(monkeypatch):
    with connect() as c:
        db.set_maintenance(c, "schema_sha256", "stale")
        c.commit()
    with connect() as locker, connect() as c:
        locker.execute("lock table snapshots in access exclusive mode")
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        orig = c.execute

        def fast(sql, *a, **k):  # shorten init's lock timeout for the test
            if isinstance(sql, str) and sql.startswith("set local lock_timeout"):
                sql = "set local lock_timeout = '300ms'"
            return orig(sql, *a, **k)
        monkeypatch.setattr(c, "execute", fast)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            db.init(c, attempts=3, wait_s=1)
        assert sleeps == [1, 2]
        locker.rollback()
    with connect() as c:  # once the lock is gone it applies and records the hash
        db.init(c)
        assert db.get_maintenance(c, "schema_sha256") != "stale"
