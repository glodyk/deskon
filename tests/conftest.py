"""Fixture PostgreSQL sementara untuk tes.

Membuat cluster PostgreSQL baru di direktori temp (initdb + pg_ctl),
memuat database/schema/*.sql berurutan, lalu menghapusnya setelah tes.
Tidak pernah menyentuh database dev/produksi.

Tes database di-skip jika binary PostgreSQL tidak ditemukan.
Set DESKON_TEST_PG_BIN ke direktori berisi initdb/pg_ctl bila perlu
(mis. /usr/lib/postgresql/14/bin).
"""

import glob
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import psycopg2
import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "database" / "schema"


def _find_pg_bin():
    candidates = []
    if os.environ.get("DESKON_TEST_PG_BIN"):
        candidates.append(os.environ["DESKON_TEST_PG_BIN"])
    found = shutil.which("pg_ctl")
    if found:
        candidates.append(os.path.dirname(found))
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    candidates += sorted(glob.glob("/usr/local/opt/postgresql*/bin"), reverse=True)
    for path in candidates:
        if os.path.isfile(os.path.join(path, "initdb")) and os.path.isfile(os.path.join(path, "pg_ctl")):
            return path
    return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def pg_dsn():
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.skip("binary PostgreSQL (initdb/pg_ctl) tidak ditemukan")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb tidak bisa dijalankan sebagai root; jalankan tes sebagai user biasa")

    tmp = tempfile.mkdtemp(prefix="deskon-pg-")
    data_dir = os.path.join(tmp, "data")
    port = _free_port()
    subprocess.run(
        [os.path.join(pg_bin, "initdb"), "-D", data_dir, "-U", "postgres",
         "-A", "trust", "-E", "UTF8", "--no-locale"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [os.path.join(pg_bin, "pg_ctl"), "-D", data_dir, "-w", "-l", os.path.join(tmp, "pg.log"),
         "-o", f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1", "start"],
        check=True, capture_output=True,
    )
    try:
        admin = psycopg2.connect(host="127.0.0.1", port=port, user="postgres", dbname="postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE DATABASE deskon_test")
        admin.close()

        dsn = {"host": "127.0.0.1", "port": port, "user": "postgres", "dbname": "deskon_test"}
        _apply_migrations(dsn)
        yield dsn
    finally:
        subprocess.run(
            [os.path.join(pg_bin, "pg_ctl"), "-D", data_dir, "-m", "fast", "stop"],
            capture_output=True,
        )
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_migrations(dsn):
    """Muat setiap file schema dalam satu transaksi (runner memegang transaksi)."""
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        conn = psycopg2.connect(**dsn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
        finally:
            conn.close()


@pytest.fixture
def conn(pg_dsn):
    """Koneksi per tes; semua perubahan di-rollback setelah tes selesai."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def review_fixture(conn):
    """Satu hospital, user, claim, dan review OPEN cycle 1 (belum di-commit)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.hospitals (kdppklayan, hospital_name) "
            "VALUES ('RS001', 'RS Test') RETURNING id"
        )
        hospital_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO core.users (full_name, email, password_hash, role) "
            "VALUES ('Verifier Test', 'verifier@test.local', 'x', 'BPJS_VERIFIER') RETURNING id"
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
            "VALUES ('0001R0011025V000031', %s, 'VERIFIKASI_PASCA_KLAIM', 'RS001', 'RITL', '2025-10-01') "
            "RETURNING id",
            (hospital_id,),
        )
        claim_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
            "VALUES (%s, %s, 1, 'VERIFIKASI_PASCA_KLAIM') RETURNING id",
            (claim_id, user_id),
        )
        review_id = cur.fetchone()[0]
    return {
        "hospital_id": hospital_id,
        "user_id": user_id,
        "claim_id": claim_id,
        "nosjp": "0001R0011025V000031",
        "review_id": review_id,
    }
