import psycopg2
import pytest

from deskon.db import transaction


def _count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM core.hospitals WHERE kdppklayan = 'TX001'")
        return cur.fetchone()[0]


def _insert(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core.hospitals (kdppklayan, hospital_name) VALUES ('TX001', 'RS Tx')")


def test_transaction_commits_on_success(pg_dsn):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            _insert(conn)
        assert _count(conn) == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM core.hospitals WHERE kdppklayan = 'TX001'")
        conn.commit()
        conn.close()


def test_transaction_rolls_back_on_error(pg_dsn):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                _insert(conn)
                raise RuntimeError("gagal")
        assert _count(conn) == 0
    finally:
        conn.close()


def test_migration_002_applied(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'review' AND table_name = 'claim_reviews' "
            "AND column_name IN ('cycle_no', 'review_type') ORDER BY column_name"
        )
        assert cur.fetchall() == [("cycle_no", "NO"), ("review_type", "NO")]
