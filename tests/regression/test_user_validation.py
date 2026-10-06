"""Regression test validasi user bersama (user_service.require_active_user).

Refactor ini hanya mengekstrak blok "user ada, lalu aktif" yang sebelumnya
inline di lima use case. Tes di sini membuktikan bahwa kelima use case
berperilaku sama seperti sebelumnya, bukan hanya helper-nya:

- user tidak ada -> NotFoundError, user nonaktif -> InvalidStateError, pesan sama;
- id non-integer dan bool ditolak di batas use case, sebelum database disentuh;
- urutan validasi: input -> user -> klaim/review/reselection;
- statement pertama yang dijalankan tiap use case adalah SELECT user yang sama,
  tanpa kunci, dan hanya satu kali;
- create_review_queue tetap tidak punya validasi user sendiri.
"""

import inspect
import uuid

import pytest

from deskon.errors import InvalidStateError, NotFoundError, ValidationError
from deskon.services import user_service
from deskon.services.finding_service import create_review_finding
from deskon.services.reselection_service import create_reselection, resolve_reselection
from deskon.services.review_service import (
    close_review,
    create_review_queue,
    open_review_cycle,
)
from deskon.services.user_service import require_active_user
from tests.regression.test_create_reselection import _NoDatabase

VPK = "VERIFIKASI_PASCA_KLAIM"
USER_SQL = "SELECT is_active FROM core.users WHERE id = %s"
MISSING = 999999999


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

@pytest.fixture
def fx(conn):
    """Klaim dengan review OPEN dan reselection PROPOSED, user aktif dan nonaktif."""
    tag = uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.hospitals (kdppklayan, hospital_name) VALUES (%s, 'RS Test') RETURNING id",
            (f"RS{tag}",),
        )
        hospital_id = cur.fetchone()[0]
        users = {}
        for label, active in (("active", True), ("inactive", False)):
            cur.execute(
                "INSERT INTO core.users (full_name, email, password_hash, role, is_active) "
                "VALUES (%s, %s, 'x', 'BPJS_VERIFIER', %s) RETURNING id",
                (label, f"{label}-{tag}@test.local", active),
            )
            users[label] = cur.fetchone()[0]
        nosjp = f"0001R001{tag}"
        cur.execute(
            "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
            "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01') RETURNING id",
            (nosjp, hospital_id, VPK, f"RS{tag}"),
        )
        claim_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
            "VALUES (%s, %s, 1, %s) RETURNING id",
            (claim_id, users["active"], VPK),
        )
        review_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO review.claim_reselections (claim_id, review_id, target_type, action, "
            "original_code, proposed_code, reason, status, created_by) "
            "VALUES (%s, %s, 'DIAGNOSIS', 'CHANGE', 'A', 'B', 'r', 'PROPOSED', %s) RETURNING id",
            (claim_id, review_id, users["active"]),
        )
        reselection_id = cur.fetchone()[0]
    return {
        "nosjp": nosjp,
        "review_id": review_id,
        "reselection_id": reselection_id,
        "active": users["active"],
        "inactive": users["inactive"],
    }


# Satu entri per use case: nama kolom user, pemanggil, target nyata, target yang
# tidak ada, dan satu argumen input yang tidak valid (untuk tes urutan).
def _open(conn, fx, uid, nosjp=None, **kw):
    return open_review_cycle(conn, nosjp=fx["nosjp"] if nosjp is None else nosjp, opened_by=uid, **kw)


def _finding(conn, fx, uid, review_id=None, **kw):
    args = dict(finding_category="K", finding_title="T", finding_description="D")
    args.update(kw)
    return create_review_finding(
        conn, review_id=fx["review_id"] if review_id is None else review_id, created_by=uid, **args
    )


def _close(conn, fx, uid, review_id=None, **kw):
    args = dict(final_decision="LAYAK", resolution_note="x")
    args.update(kw)
    return close_review(
        conn, review_id=fx["review_id"] if review_id is None else review_id, closed_by=uid, **args
    )


def _create_resel(conn, fx, uid, review_id=None, **kw):
    args = dict(
        target_type="DIAGNOSIS", action="CHANGE", original_code="A", proposed_code="B", reason="r"
    )
    args.update(kw)
    return create_reselection(
        conn, review_id=fx["review_id"] if review_id is None else review_id, created_by=uid, **args
    )


def _resolve(conn, fx, uid, reselection_id=None, **kw):
    args = dict(decision="AGREED")
    args.update(kw)
    return resolve_reselection(
        conn,
        reselection_id=fx["reselection_id"] if reselection_id is None else reselection_id,
        resolved_by=uid,
        **args,
    )


# (id, pemanggil, nama argumen user, kwargs target yang tidak ada, kwargs input tidak valid)
CASES = [
    ("open_review_cycle", _open, "opened_by", {"nosjp": "TIDAK-ADA"}, {"review_type": "BUKAN-TIPE"}),
    ("create_review_finding", _finding, "created_by", {"review_id": MISSING}, {"finding_title": "  "}),
    ("close_review", _close, "closed_by", {"review_id": MISSING}, {"resolution_note": "  "}),
    ("create_reselection", _create_resel, "created_by", {"review_id": MISSING}, {"reason": "  "}),
    ("resolve_reselection", _resolve, "resolved_by", {"reselection_id": MISSING}, {"decision": "BUKAN"}),
]
IDS = [case[0] for case in CASES]


@pytest.fixture(params=CASES, ids=IDS)
def case(request):
    return request.param


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def test_helper_accepts_an_active_user(conn, fx):
    assert require_active_user(conn, fx["active"]) is None


def test_helper_rejects_an_unknown_user(conn, fx):
    with pytest.raises(NotFoundError) as exc:
        require_active_user(conn, MISSING)
    assert str(exc.value) == f"User id={MISSING} tidak ditemukan."


def test_helper_rejects_an_inactive_user(conn, fx):
    with pytest.raises(InvalidStateError) as exc:
        require_active_user(conn, fx["inactive"])
    assert str(exc.value) == f"User id={fx['inactive']} tidak aktif."


def test_helper_takes_no_lock_on_the_user_row(pg_dsn):
    import psycopg2

    tag = uuid.uuid4().hex[:12]
    setup = psycopg2.connect(**pg_dsn)
    try:
        with setup, setup.cursor() as cur:
            cur.execute(
                "INSERT INTO core.users (full_name, email, password_hash, role, is_active) "
                "VALUES ('u', %s, 'x', 'BPJS_VERIFIER', true) RETURNING id",
                (f"u-{tag}@test.local",),
            )
            user_id = cur.fetchone()[0]
        checker = psycopg2.connect(**pg_dsn)
        locker = psycopg2.connect(**pg_dsn)
        try:
            require_active_user(checker, user_id)
            with locker.cursor() as cur:
                # FOR UPDATE NOWAIT gagal bila ada kunci lain di baris itu.
                cur.execute("SELECT id FROM core.users WHERE id = %s FOR UPDATE NOWAIT", (user_id,))
                assert cur.fetchone() == (user_id,)
        finally:
            checker.rollback()
            locker.rollback()
            checker.close()
            locker.close()
    finally:
        with setup, setup.cursor() as cur:
            cur.execute("DELETE FROM core.users WHERE email = %s", (f"u-{tag}@test.local",))
        setup.close()


# ---------------------------------------------------------------------------
# Lima use case: perilaku sama dengan sebelum refactor
# ---------------------------------------------------------------------------

def test_active_user_passes_validation_and_the_use_case_succeeds(conn, fx, case):
    _, call, *_ = case
    result = call(conn, fx, fx["active"])
    assert result is not None


def test_unknown_user_is_not_found_with_the_same_message(conn, fx, case):
    _, call, *_ = case
    with pytest.raises(NotFoundError) as exc:
        call(conn, fx, MISSING)
    assert str(exc.value) == f"User id={MISSING} tidak ditemukan."


def test_inactive_user_is_invalid_state_with_the_same_message(conn, fx, case):
    _, call, *_ = case
    with pytest.raises(InvalidStateError) as exc:
        call(conn, fx, fx["inactive"])
    assert str(exc.value) == f"User id={fx['inactive']} tidak aktif."


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, None])
def test_non_integer_and_bool_user_ids_are_rejected_before_the_database(case, bad):
    _, call, *_ = case
    with pytest.raises(ValidationError):
        call(_NoDatabase(), {"nosjp": "N", "review_id": 1, "reselection_id": 1}, bad)


def test_user_failure_changes_nothing(conn, fx, case):
    _, call, *_ = case
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review.review_events")
        events_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM review.claim_reviews WHERE status = 'OPEN'")
        open_before = cur.fetchone()[0]
    for uid in (MISSING, fx["inactive"]):
        with pytest.raises((NotFoundError, InvalidStateError)):
            call(conn, fx, uid)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review.review_events")
        assert cur.fetchone()[0] == events_before
        cur.execute("SELECT count(*) FROM review.claim_reviews WHERE status = 'OPEN'")
        assert cur.fetchone()[0] == open_before


# ---------------------------------------------------------------------------
# Urutan validasi: input -> user -> klaim/review/reselection
# ---------------------------------------------------------------------------

def test_input_validation_comes_before_the_user_check(conn, fx, case):
    _, call, _, _, bad_input = case
    with pytest.raises(ValidationError):
        call(conn, fx, MISSING, **bad_input)
    with pytest.raises(ValidationError):
        call(conn, fx, fx["inactive"], **bad_input)


def test_user_check_comes_before_the_target_check(conn, fx, case):
    _, call, _, missing_target, _ = case
    with pytest.raises(NotFoundError, match="User"):
        call(conn, fx, MISSING, **missing_target)
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        call(conn, fx, fx["inactive"], **missing_target)


def test_an_active_user_then_reaches_the_target_check(conn, fx, case):
    _, call, _, missing_target, _ = case
    with pytest.raises(NotFoundError) as exc:
        call(conn, fx, fx["active"], **missing_target)
    assert "User" not in str(exc.value)


# ---------------------------------------------------------------------------
# SQL: statement pertama adalah SELECT user yang sama, tanpa kunci, satu kali
# ---------------------------------------------------------------------------

class _Recording:
    """Bungkus koneksi: catat setiap statement SQL yang dijalankan."""

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def cursor(self, *args, **kwargs):
        return _RecordingCursor(self._conn.cursor(*args, **kwargs), self.statements)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecordingCursor:
    def __init__(self, cur, statements):
        self._cur = cur
        self._statements = statements

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)

    def execute(self, sql, params=None):
        self._statements.append((" ".join(sql.split()), params))
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def test_the_first_statement_is_the_user_lookup_and_it_runs_once(conn, fx, case):
    _, call, *_ = case
    recording = _Recording(conn)
    call(recording, fx, fx["active"])

    statements = recording.statements
    assert statements[0] == (USER_SQL, (fx["active"],))
    assert "FOR " not in statements[0][0]
    assert sum("core.users" in sql for sql, _ in statements) == 1


# ---------------------------------------------------------------------------
# create_review_queue: tetap mendelegasikan ke open_review_cycle
# ---------------------------------------------------------------------------

def test_queue_has_no_user_validation_of_its_own():
    source = inspect.getsource(create_review_queue)
    assert "core.users" not in source
    assert "require_active_user" not in source
    assert "user_service" not in source


def test_queue_still_validates_the_user_through_open_review_cycle(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO staging.import_batches (source_filename, source_type, imported_by, status) "
            "VALUES ('f.csv', 'CSV', %s, 'IMPORTED') RETURNING id",
            (fx["active"],),
        )
        batch_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE core.claims SET source_import_batch_id = %s WHERE nosjp = %s",
            (batch_id, fx["nosjp"]),
        )
    with pytest.raises(NotFoundError, match="User"):
        create_review_queue(conn, import_batch_id=batch_id, opened_by=MISSING)
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        create_review_queue(conn, import_batch_id=batch_id, opened_by=fx["inactive"])
    result = create_review_queue(conn, import_batch_id=batch_id, opened_by=fx["active"])
    assert result.claims_total == 1


def test_no_inline_copy_of_the_user_query_is_left_in_the_services():
    from deskon.services import finding_service, reselection_service, review_service

    for module in (finding_service, reselection_service, review_service):
        assert "core.users" not in inspect.getsource(module), module.__name__
        assert "user_service.require_active_user" in inspect.getsource(module)
    assert "core.users" in inspect.getsource(user_service)
