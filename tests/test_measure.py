"""The storage report runs end-to-end on the test database (read-only) and contains every section."""

import os

import psycopg
import pytest

from tracker.measure import measure

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_measure_report_has_all_sections():
    with psycopg.connect(URL) as conn:
        report = measure(conn)
        conn.rollback()
    for heading in ("Database size", "Space by table", "Growth per day", "Repeated readings", "Biggest placement lists", "Dormant games"):
        assert heading in report
