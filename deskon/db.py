"""Koneksi database.

Pembagian tanggung jawab transaksi:
- caller (CLI, API, test) membuka koneksi dan memegang transaksi
  lewat `transaction()`;
- service hanya menerima koneksi, tidak pernah commit/rollback.
"""

from contextlib import contextmanager

import psycopg2

from deskon.config import load_database_config


def connect(config=None):
    """Buka koneksi psycopg2 baru (autocommit off)."""
    config = config or load_database_config()
    conn = psycopg2.connect(**config.connect_kwargs())
    conn.autocommit = False
    return conn


@contextmanager
def transaction(conn):
    """Commit jika blok selesai normal, rollback jika ada exception.

        with transaction(conn):
            result = some_service.some_use_case(conn, ...)
    """
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def connection(config=None):
    """Koneksi yang otomatis ditutup. Tidak commit; gunakan transaction()."""
    conn = connect(config)
    try:
        yield conn
    finally:
        conn.close()
