"""Regression test create_review_queue terhadap behavior create_review_queue_v1.py.

Yang dibandingkan adalah hasil bisnis, bukan id mentah: status, cycle_no,
review_type, opened_by, event_type/entity_type/event_data, dan relasi FK.

Behavior lama yang dipertahankan (script v1):
- user ada dan aktif; batch ada dan IMPORTED; batch tanpa klaim ditolak;
- klaim = core.claims dengan source_import_batch_id = batch, urut id;
- klaim yang sudah punya review OPEN dilewati; klaim lain mendapat review
  OPEN baru (termasuk klaim yang hanya punya riwayat CLOSED);
- seluruh batch dalam satu transaksi (all-or-nothing).

Perubahan yang disepakati: cycle_no dan review_type (claim_status per klaim)
lewat open_review_cycle, event REVIEW_OPENED per review yang dibuat, kunci
batch/klaim/review, tanpa print, tanpa commit, tanpa salinan validasi user.
"""

import uuid

import psycopg2
import pytest

from deskon.db import transaction
from deskon.errors import (
    AuditEventError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service
from deskon.services.finding_service import create_review_finding
from deskon.services.reselection_service import create_reselection, resolve_reselection
from deskon.services.review_service import (
    close_review,
    create_review_queue,
    open_review_cycle,
)
from tests.regression.test_create_reselection import (
    CHANGE,
    _assert_waiting,
    _lock_attempt,
    _NoDatabase,
    _run_in_thread,
)

VPK = "VERIFIKASI_PASCA_KLAIM"
AAK = "AUDIT_ADMINISTRASI_KLAIM"
PENDING = "PENDING"

OPENED_KEYS = {
    "claim_id", "nosjp", "review_id", "new_review_id", "cycle_no", "review_type",
    "review_type_source", "previous_review_count", "previous_review_ids",
}


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn, statuses=(VPK, VPK, VPK), batch_status="IMPORTED"):
    """Hospital, user aktif/nonaktif, batch dengan satu klaim per status, dan
    satu klaim milik batch lain."""
    tag = uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.hospitals (kdppklayan, hospital_name) VALUES (%s, 'RS Test') RETURNING id",
            (f"RS{tag}",),
        )
        hospital_id = cur.fetchone()[0]
        users = {}
        for label, active in (("verifier", True), ("inactive", False)):
            cur.execute(
                "INSERT INTO core.users (full_name, email, password_hash, role, is_active) "
                "VALUES (%s, %s, 'x', 'BPJS_VERIFIER', %s) RETURNING id",
                (label, f"{label}-{tag}@test.local", active),
            )
            users[label] = cur.fetchone()[0]
        batch_ids = []
        for status in (batch_status, "IMPORTED"):
            cur.execute(
                "INSERT INTO staging.import_batches (source_filename, source_type, imported_by, status) "
                "VALUES (%s, 'CSV', %s, %s) RETURNING id",
                (f"f-{tag}.csv", users["verifier"], status),
            )
            batch_ids.append(cur.fetchone()[0])
        claims = []
        for n, status in enumerate(statuses):
            nosjp = f"0001R001{tag}{n:03d}"
            cur.execute(
                "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, "
                "service_month, source_import_batch_id) "
                "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01', %s) RETURNING id",
                (nosjp, hospital_id, status, f"RS{tag}", batch_ids[0]),
            )
            claims.append((cur.fetchone()[0], nosjp))
        other_nosjp = f"0001R001{tag}OTH"
        cur.execute(
            "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, "
            "service_month, source_import_batch_id) "
            "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01', %s) RETURNING id",
            (other_nosjp, hospital_id, VPK, f"RS{tag}", batch_ids[1]),
        )
        other = (cur.fetchone()[0], other_nosjp)
    return {
        "hospital_id": hospital_id,
        "batch_id": batch_ids[0],
        "other_batch_id": batch_ids[1],
        "claims": claims,
        "claim_ids": [c[0] for c in claims],
        "nosjps": [c[1] for c in claims],
        "other_claim": other,
        "verifier": users["verifier"],
        "inactive": users["inactive"],
    }


def add_open_review(conn, fx, claim_id, cycle_no=1, review_type=VPK):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (claim_id, fx["verifier"], cycle_no, review_type),
        )
        return cur.fetchone()[0]


def add_closed_review(conn, fx, claim_id, cycle_no=1, review_type=VPK):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review.claim_reviews (
                claim_id, status, final_decision, resolution_note, opened_by,
                closed_by, closed_at, cycle_no, review_type
            )
            VALUES (%s, 'CLOSED', 'LAYAK', 'lama', %s, %s, CURRENT_TIMESTAMP, %s, %s)
            RETURNING id
            """,
            (claim_id, fx["verifier"], fx["verifier"], cycle_no, review_type),
        )
        return cur.fetchone()[0]


def cleanup(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            ids = fx["claim_ids"] + [fx["other_claim"][0]]
            reviews = "(SELECT id FROM review.claim_reviews WHERE claim_id = ANY(%s))"
            cur.execute(f"DELETE FROM review.review_events WHERE review_id IN {reviews}", (ids,))
            cur.execute(f"DELETE FROM review.review_findings WHERE review_id IN {reviews}", (ids,))
            cur.execute("DELETE FROM review.claim_reselections WHERE claim_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM review.claim_reviews WHERE claim_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM core.claims WHERE id = ANY(%s)", (ids,))
            cur.execute(
                "DELETE FROM staging.import_batches WHERE id IN (%s, %s)",
                (fx["batch_id"], fx["other_batch_id"]),
            )
            cur.execute("DELETE FROM core.users WHERE id IN (%s, %s)", (fx["verifier"], fx["inactive"]))
            cur.execute("DELETE FROM core.hospitals WHERE id = %s", (fx["hospital_id"],))
    finally:
        conn.close()


@pytest.fixture
def fx(conn):
    return seed(conn)


@pytest.fixture
def committed_fx(pg_dsn):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            data = seed(conn)
    finally:
        conn.close()
    yield data
    cleanup(pg_dsn, data)


def queue(conn, fx, opened_by=None, batch_id=None):
    return create_review_queue(
        conn,
        import_batch_id=fx["batch_id"] if batch_id is None else batch_id,
        opened_by=fx["verifier"] if opened_by is None else opened_by,
    )


def reviews_of(conn, claim_ids):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT claim_id, id, status, cycle_no, review_type, opened_by, final_decision,
                   resolution_note, closed_by, closed_at, opened_at
            FROM review.claim_reviews WHERE claim_id = ANY(%s) ORDER BY claim_id, cycle_no
            """,
            (claim_ids,),
        )
        return cur.fetchall()


def events_of(conn, claim_ids):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.event_type, e.entity_type, e.entity_id, e.user_id, e.event_data, e.review_id
            FROM review.review_events e
            JOIN review.claim_reviews r ON r.id = e.review_id
            WHERE r.claim_id = ANY(%s) ORDER BY e.id
            """,
            (claim_ids,),
        )
        return cur.fetchall()


def all_claim_ids(fx):
    return fx["claim_ids"] + [fx["other_claim"][0]]


# ---------------------------------------------------------------------------
# Sukses
# ---------------------------------------------------------------------------

def test_claims_without_review_get_cycle_1_open_reviews(conn, fx):
    result = queue(conn, fx)

    rows = reviews_of(conn, fx["claim_ids"])
    assert [(r[0], r[2], r[3], r[4], r[5]) for r in rows] == [
        (claim_id, "OPEN", 1, VPK, fx["verifier"]) for claim_id in fx["claim_ids"]
    ]
    assert [e[0] for e in events_of(conn, fx["claim_ids"])] == ["REVIEW_OPENED"] * 3
    assert [r.claim_id for r in result.created] == fx["claim_ids"]
    assert [r.nosjp for r in result.created] == fx["nosjps"]
    assert result.existing == ()
    assert len(result.events) == 3
    assert [e.entity_id for e in result.events] == [r.id for r in result.created]


@pytest.mark.parametrize("statuses", [(PENDING, VPK, AAK), (AAK, AAK, PENDING)])
def test_review_type_is_taken_from_each_claim_status(conn, statuses):
    fx = seed(conn, statuses=statuses)
    result = queue(conn, fx)
    assert [r.review_type for r in result.created] == list(statuses)
    types = [row[4] for row in reviews_of(conn, fx["claim_ids"])]
    assert types == list(statuses)
    sources = {e[4]["review_type_source"] for e in events_of(conn, fx["claim_ids"])}
    assert sources == {"claim_status"}


def test_new_reviews_have_no_decision_or_closing_data(conn, fx):
    queue(conn, fx)
    for row in reviews_of(conn, fx["claim_ids"]):
        assert row[6:10] == (None, None, None, None)


def test_all_reviews_in_one_call_share_opened_at(conn, fx):
    queue(conn, fx)
    assert len({row[10] for row in reviews_of(conn, fx["claim_ids"])}) == 1


def test_result_counts_are_consistent(conn, fx):
    add_open_review(conn, fx, fx["claim_ids"][1])
    result = queue(conn, fx)
    assert result.import_batch_id == fx["batch_id"]
    assert result.claims_total == 3
    assert len(result.created) + len(result.existing) == result.claims_total
    assert [r.claim_id for r in result.existing] == [fx["claim_ids"][1]]
    assert len(result.events) == len(result.created) == 2


def test_other_batch_claims_are_untouched(conn, fx):
    queue(conn, fx)
    assert reviews_of(conn, [fx["other_claim"][0]]) == []
    assert events_of(conn, [fx["other_claim"][0]]) == []


def test_batch_and_claims_are_not_changed(conn, fx):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM staging.import_batches WHERE id = %s", (fx["batch_id"],))
        batch_before = cur.fetchone()
        cur.execute("SELECT * FROM core.claims WHERE id = ANY(%s) ORDER BY id", (fx["claim_ids"],))
        claims_before = cur.fetchall()
    queue(conn, fx)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM staging.import_batches WHERE id = %s", (fx["batch_id"],))
        assert cur.fetchone() == batch_before
        cur.execute("SELECT * FROM core.claims WHERE id = ANY(%s) ORDER BY id", (fx["claim_ids"],))
        assert cur.fetchall() == claims_before


# ---------------------------------------------------------------------------
# Idempotency, OPEN / CLOSED / belum ada
# ---------------------------------------------------------------------------

def test_second_run_creates_nothing_and_emits_no_events(conn, fx):
    queue(conn, fx)
    snapshot = (reviews_of(conn, fx["claim_ids"]), events_of(conn, fx["claim_ids"]))

    result = queue(conn, fx)

    assert result.created == () and result.events == ()
    assert len(result.existing) == 3
    assert (reviews_of(conn, fx["claim_ids"]), events_of(conn, fx["claim_ids"])) == snapshot


def test_only_missing_claims_are_created_and_open_reviews_are_untouched(conn, fx):
    existing_id = add_open_review(conn, fx, fx["claim_ids"][0], review_type=AAK)
    before = reviews_of(conn, [fx["claim_ids"][0]])

    result = queue(conn, fx)

    assert reviews_of(conn, [fx["claim_ids"][0]]) == before
    assert [r.id for r in result.existing] == [existing_id]
    assert [r.claim_id for r in result.created] == fx["claim_ids"][1:]
    assert len(events_of(conn, fx["claim_ids"])) == 2
    assert all(e[5] != existing_id for e in events_of(conn, fx["claim_ids"]))


def test_all_claims_already_open_creates_nothing(conn, fx):
    for claim_id in fx["claim_ids"]:
        add_open_review(conn, fx, claim_id)
    result = queue(conn, fx)
    assert result.created == () and result.events == ()
    assert len(result.existing) == 3
    assert events_of(conn, fx["claim_ids"]) == []


def test_claim_with_only_closed_history_gets_the_next_cycle(conn, fx):
    old_id = add_closed_review(conn, fx, fx["claim_ids"][0], cycle_no=1)
    old_before = reviews_of(conn, [fx["claim_ids"][0]])

    result = queue(conn, fx)

    rows = reviews_of(conn, [fx["claim_ids"][0]])
    assert [(r[2], r[3]) for r in rows] == [("CLOSED", 1), ("OPEN", 2)]
    assert rows[0] == old_before[0]
    event = [e for e in events_of(conn, fx["claim_ids"]) if e[5] == rows[1][1]][0]
    assert event[4]["cycle_no"] == 2
    assert event[4]["previous_review_ids"] == [old_id]
    assert event[4]["previous_review_count"] == 1


# ---------------------------------------------------------------------------
# Validasi
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"import_batch_id": "1"}, {"import_batch_id": True}, {"import_batch_id": None}, {"import_batch_id": 1.0},
    {"opened_by": "1"}, {"opened_by": False}, {"opened_by": None},
])
def test_wrong_types_are_rejected_before_the_database_is_read(kwargs):
    args = {"import_batch_id": 1, "opened_by": 1, **kwargs}
    with pytest.raises(ValidationError):
        create_review_queue(_NoDatabase(), **args)


def test_unknown_user_is_rejected_and_nothing_is_created(conn, fx):
    with pytest.raises(NotFoundError, match="User"):
        queue(conn, fx, opened_by=999999999)
    assert reviews_of(conn, fx["claim_ids"]) == []
    assert events_of(conn, fx["claim_ids"]) == []


def test_inactive_user_is_rejected_and_nothing_is_created(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        queue(conn, fx, opened_by=fx["inactive"])
    assert reviews_of(conn, fx["claim_ids"]) == []


def test_inactive_user_is_rejected_even_if_every_claim_is_already_open(conn, fx):
    for claim_id in fx["claim_ids"]:
        add_open_review(conn, fx, claim_id)
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        queue(conn, fx, opened_by=fx["inactive"])


def test_unknown_batch_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="batch"):
        queue(conn, fx, batch_id=999999999)


@pytest.mark.parametrize("status", ["UPLOADED", "VALIDATING", "VALIDATED", "TRANSFORMING", "FAILED"])
def test_batch_that_is_not_imported_is_rejected(conn, status):
    fx = seed(conn, batch_status=status)
    with pytest.raises(InvalidStateError, match=status):
        queue(conn, fx)
    assert reviews_of(conn, fx["claim_ids"]) == []


def test_batch_without_claims_is_rejected(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO staging.import_batches (source_filename, source_type, imported_by, status) "
            "VALUES ('kosong.csv', 'CSV', %s, 'IMPORTED') RETURNING id",
            (fx["verifier"],),
        )
        empty_batch = cur.fetchone()[0]
    with pytest.raises(NotFoundError, match="Tidak ada core.claims"):
        queue(conn, fx, batch_id=empty_batch)


def test_batch_errors_come_before_the_user_check(conn):
    fx = seed(conn, batch_status="FAILED")
    with pytest.raises(InvalidStateError, match="FAILED"):
        queue(conn, fx, opened_by=999999999)
    with pytest.raises(NotFoundError, match="batch"):
        queue(conn, fx, opened_by=999999999, batch_id=999999999)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_event_identity_and_payload_are_exactly_open_review_cycles(conn, fx):
    result = queue(conn, fx)
    events = events_of(conn, fx["claim_ids"])
    assert len(events) == 3
    for (event_type, entity_type, entity_id, user_id, data, review_id), review, claim in zip(
        events, result.created, fx["claims"]
    ):
        assert (event_type, entity_type, entity_id, user_id) == (
            "REVIEW_OPENED", "claim_review", review.id, fx["verifier"],
        )
        assert review_id == review.id
        assert set(data) == OPENED_KEYS
        assert "import_batch_id" not in data
        assert data == {
            "claim_id": claim[0],
            "nosjp": claim[1],
            "review_id": review.id,
            "new_review_id": review.id,
            "cycle_no": 1,
            "review_type": VPK,
            "review_type_source": "claim_status",
            "previous_review_count": 0,
            "previous_review_ids": [],
        }


def test_queue_writes_no_event_of_its_own(conn, fx):
    queue(conn, fx)
    assert {e[0] for e in events_of(conn, fx["claim_ids"])} == {"REVIEW_OPENED"}


# ---------------------------------------------------------------------------
# Transaksi dan rollback
# ---------------------------------------------------------------------------

def _count(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        return len(reviews_of(conn, fx["claim_ids"])), len(events_of(conn, fx["claim_ids"]))
    finally:
        conn.close()


def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    a = psycopg2.connect(**pg_dsn)
    try:
        queue(a, fx)
        assert _count(pg_dsn, fx) == (0, 0)
    finally:
        a.rollback()
        a.close()


def test_callers_rollback_discards_all_reviews_and_events(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        queue(conn, fx)
        assert len(reviews_of(conn, fx["claim_ids"])) == 3
        conn.rollback()
        assert reviews_of(conn, fx["claim_ids"]) == []
        assert events_of(conn, fx["claim_ids"]) == []
    finally:
        conn.close()


def test_transaction_commits_everything_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            queue(conn, fx)
    finally:
        conn.close()
    assert _count(pg_dsn, fx) == (3, 3)


def test_exception_after_the_queue_rolls_back_everything(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                queue(conn, fx)
                raise RuntimeError("gagal setelah service")
    finally:
        conn.close()
    assert _count(pg_dsn, fx) == (0, 0)


def test_audit_failure_on_a_later_claim_rolls_back_the_whole_batch(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx
    real = audit_service.record_event
    calls = []

    def failing_on_third(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise AuditEventError("audit gagal")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "record_event", failing_on_third)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                queue(conn, fx)
    finally:
        conn.close()
    assert len(calls) == 3
    assert _count(pg_dsn, fx) == (0, 0)


def test_failing_claim_is_never_skipped(conn, fx, monkeypatch):
    """Klaim ke-2 gagal: error merambat; tidak ada hasil parsial yang dikembalikan."""
    real = audit_service.record_event
    calls = []

    def failing_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise AuditEventError("audit gagal")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "record_event", failing_on_second)
    with pytest.raises(AuditEventError):
        queue(conn, fx)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Konkurensi
# ---------------------------------------------------------------------------

def _queue_job(fx):
    return lambda c: queue(c, fx)


def _committed_open_review(pg_dsn, fx, claim_index=0):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            return add_open_review(conn, fx, fx["claim_ids"][claim_index])
    finally:
        conn.close()


def _state(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        rows = reviews_of(conn, fx["claim_ids"])
        return [(r[0], r[2], r[3]) for r in rows], len(events_of(conn, fx["claim_ids"]))
    finally:
        conn.close()


def test_queue_waits_for_a_queue_in_progress_and_then_finds_everything_open(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        queue(first, fx)
        thread, outcome = _run_in_thread(pg_dsn, _queue_job(fx))
        _assert_waiting(thread, "antrian kedua")
        first.commit()
        thread.join(timeout=10)
    finally:
        first.close()
    assert "error" not in outcome
    assert outcome["result"].created == ()
    assert len(outcome["result"].existing) == 3
    assert _state(pg_dsn, fx) == ([(c, "OPEN", 1) for c in fx["claim_ids"]], 3)


def test_second_queue_creates_everything_if_the_first_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        queue(first, fx)
        thread, outcome = _run_in_thread(pg_dsn, _queue_job(fx))
        _assert_waiting(thread, "antrian kedua")
        first.rollback()
        thread.join(timeout=10)
    finally:
        first.close()
    assert "error" not in outcome
    assert len(outcome["result"].created) == 3
    assert _state(pg_dsn, fx) == ([(c, "OPEN", 1) for c in fx["claim_ids"]], 3)


def test_many_concurrent_queues_do_not_deadlock_and_leave_one_open_review_per_claim(pg_dsn):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            fx = seed(conn, statuses=(VPK,) * 12)
    finally:
        conn.close()
    try:
        runs = [_run_in_thread(pg_dsn, _queue_job(fx)) for _ in range(4)]
        for thread, _ in runs:
            thread.join(timeout=30)
            assert not thread.is_alive(), "kemungkinan deadlock"
        for _, outcome in runs:
            assert "error" not in outcome, outcome
        created = sum(len(o["result"].created) for _, o in runs)
        assert created == 12
        rows, events = _state(pg_dsn, fx)
        assert rows == [(c, "OPEN", 1) for c in fx["claim_ids"]]
        assert events == 12
    finally:
        cleanup(pg_dsn, fx)


def test_queue_waits_for_a_single_open_review_cycle_on_one_of_its_claims(pg_dsn, committed_fx):
    fx = committed_fx
    opener = psycopg2.connect(**pg_dsn)
    try:
        result = open_review_cycle(opener, nosjp=fx["nosjps"][1], opened_by=fx["verifier"])
        assert result.created
        thread, outcome = _run_in_thread(pg_dsn, _queue_job(fx))
        _assert_waiting(thread, "antrian")
        opener.commit()
        thread.join(timeout=10)
    finally:
        opener.close()
    assert "error" not in outcome
    assert [r.claim_id for r in outcome["result"].existing] == [fx["claim_ids"][1]]
    assert len(outcome["result"].created) == 2
    assert _state(pg_dsn, fx) == ([(c, "OPEN", 1) for c in fx["claim_ids"]], 3)


def _add_reselection(pg_dsn, fx, review_id):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            return create_reselection(
                conn, review_id=review_id, created_by=fx["verifier"], **CHANGE
            ).reselection.id
    finally:
        conn.close()


@pytest.mark.parametrize("child", ["finding", "reselection", "resolve"])
def test_queue_waits_for_a_child_write_on_an_existing_open_review(pg_dsn, committed_fx, child):
    fx = committed_fx
    review_id = _committed_open_review(pg_dsn, fx)
    reselection_id = _add_reselection(pg_dsn, fx, review_id) if child == "resolve" else None

    holder = psycopg2.connect(**pg_dsn)
    try:
        if child == "finding":
            create_review_finding(
                holder, review_id=review_id, created_by=fx["verifier"],
                finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
            )
        elif child == "reselection":
            create_reselection(holder, review_id=review_id, created_by=fx["verifier"], **CHANGE)
        else:
            resolve_reselection(
                holder, reselection_id=reselection_id, resolved_by=fx["verifier"], decision="AGREED"
            )
        thread, outcome = _run_in_thread(pg_dsn, _queue_job(fx))
        _assert_waiting(thread, "antrian")
        holder.commit()
        thread.join(timeout=10)
    finally:
        holder.close()
    assert "error" not in outcome
    assert [r.id for r in outcome["result"].existing] == [review_id]
    assert len(outcome["result"].created) == 2


def test_queue_waits_for_a_closing_review_and_then_opens_the_next_cycle(pg_dsn, committed_fx):
    fx = committed_fx
    review_id = _committed_open_review(pg_dsn, fx)
    closer = psycopg2.connect(**pg_dsn)
    try:
        close_review(
            closer, review_id=review_id, closed_by=fx["verifier"],
            final_decision="LAYAK", resolution_note="Selesai.",
        )
        thread, outcome = _run_in_thread(pg_dsn, _queue_job(fx))
        _assert_waiting(thread, "antrian")
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()
    assert "error" not in outcome
    assert outcome["result"].existing == ()
    assert len(outcome["result"].created) == 3
    rows, _ = _state(pg_dsn, fx)
    assert (fx["claim_ids"][0], "CLOSED", 1) in rows and (fx["claim_ids"][0], "OPEN", 2) in rows


def test_closing_waits_for_a_queue_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    review_id = _committed_open_review(pg_dsn, fx)
    runner = psycopg2.connect(**pg_dsn)
    try:
        queue(runner, fx)
        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: close_review(
                c, review_id=review_id, closed_by=fx["verifier"],
                final_decision="LAYAK", resolution_note="Selesai.",
            ),
        )
        _assert_waiting(thread, "close_review")
        runner.commit()
        thread.join(timeout=10)
    finally:
        runner.close()
    assert "error" not in outcome
    rows, _ = _state(pg_dsn, fx)
    assert (fx["claim_ids"][0], "CLOSED", 1) in rows


def test_finding_on_an_existing_review_waits_for_the_queue(pg_dsn, committed_fx):
    fx = committed_fx
    review_id = _committed_open_review(pg_dsn, fx)
    runner = psycopg2.connect(**pg_dsn)
    try:
        queue(runner, fx)
        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: create_review_finding(
                c, review_id=review_id, created_by=fx["verifier"],
                finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
            ),
        )
        _assert_waiting(thread, "create_review_finding")
        runner.commit()
        thread.join(timeout=10)
    finally:
        runner.close()
    assert "error" not in outcome


def test_batch_writer_waits_for_the_queue_and_queues_do_not_block_each_other(pg_dsn, committed_fx):
    fx = committed_fx
    runner = psycopg2.connect(**pg_dsn)
    try:
        queue(runner, fx)
        # FOR SHARE pada batch: sesama pembaca tidak menunggu
        assert _lock_attempt(
            pg_dsn, "SELECT id FROM staging.import_batches WHERE id = %s FOR SHARE NOWAIT", (fx["batch_id"],)
        )
        assert _lock_attempt(
            pg_dsn, "SELECT id FROM staging.import_batches WHERE id = %s FOR KEY SHARE NOWAIT", (fx["batch_id"],)
        )
        # penulis yang mengubah baris batch menunggu
        assert not _lock_attempt(
            pg_dsn, "SELECT id FROM staging.import_batches WHERE id = %s FOR NO KEY UPDATE NOWAIT", (fx["batch_id"],)
        )
        assert not _lock_attempt(
            pg_dsn, "SELECT id FROM staging.import_batches WHERE id = %s FOR UPDATE NOWAIT", (fx["batch_id"],)
        )
    finally:
        runner.rollback()
        runner.close()


def test_lock_modes_while_the_queue_runs(pg_dsn, committed_fx):
    fx = committed_fx
    review_id = _committed_open_review(pg_dsn, fx)
    runner = psycopg2.connect(**pg_dsn)
    try:
        queue(runner, fx)
        claim_sql = "SELECT id FROM core.claims WHERE id = %s FOR {} NOWAIT"
        review_sql = "SELECT id FROM review.claim_reviews WHERE id = %s FOR {} NOWAIT"
        other_claim = (fx["other_claim"][0],)

        # klaim batch: FOR NO KEY UPDATE; hanya KEY SHARE (FK) yang lolos
        for claim_id in fx["claim_ids"]:
            assert not _lock_attempt(pg_dsn, claim_sql.format("NO KEY UPDATE"), (claim_id,))
            assert not _lock_attempt(pg_dsn, claim_sql.format("SHARE"), (claim_id,))
            assert _lock_attempt(pg_dsn, claim_sql.format("KEY SHARE"), (claim_id,))
        # klaim batch lain tidak dikunci
        assert _lock_attempt(pg_dsn, claim_sql.format("UPDATE"), other_claim)
        # review OPEN yang sudah ada: FOR UPDATE (menahan anak review)
        assert not _lock_attempt(pg_dsn, review_sql.format("SHARE"), (review_id,))
        assert not _lock_attempt(pg_dsn, review_sql.format("NO KEY UPDATE"), (review_id,))
        assert not _lock_attempt(pg_dsn, review_sql.format("KEY SHARE"), (review_id,))
    finally:
        runner.rollback()
        runner.close()


def test_queue_open_close_and_children_together_do_not_deadlock(pg_dsn):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            fx = seed(conn, statuses=(VPK,) * 6)
            review_id = add_open_review(conn, fx, fx["claim_ids"][0])
            reselection_id = create_reselection(
                conn, review_id=review_id, created_by=fx["verifier"], **CHANGE
            ).reselection.id
    finally:
        conn.close()
    try:
        jobs = [
            _queue_job(fx),
            _queue_job(fx),
            lambda c: open_review_cycle(c, nosjp=fx["nosjps"][0], opened_by=fx["verifier"]),
            lambda c: open_review_cycle(c, nosjp=fx["nosjps"][3], opened_by=fx["verifier"]),
            lambda c: close_review(
                c, review_id=review_id, closed_by=fx["verifier"],
                final_decision="LAYAK", resolution_note="Selesai.",
            ),
            lambda c: create_reselection(c, review_id=review_id, created_by=fx["verifier"], **CHANGE),
            lambda c: create_review_finding(
                c, review_id=review_id, created_by=fx["verifier"],
                finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
            ),
            lambda c: resolve_reselection(
                c, reselection_id=reselection_id, resolved_by=fx["verifier"], decision="REJECTED"
            ),
        ]
        runs = [_run_in_thread(pg_dsn, job) for job in jobs]
        for thread, _ in runs:
            thread.join(timeout=30)
            assert not thread.is_alive(), "kemungkinan deadlock atau menunggu tanpa akhir"
        for _, outcome in runs:
            error = outcome.get("error")
            # Penolakan bisnis (review sudah CLOSED) sah; deadlock/timeout tidak.
            assert error is None or isinstance(error, (InvalidStateError, NotFoundError)), error
        conn = psycopg2.connect(**pg_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT claim_id, count(*) FROM review.claim_reviews "
                    "WHERE claim_id = ANY(%s) AND status = 'OPEN' GROUP BY claim_id HAVING count(*) > 1",
                    (fx["claim_ids"],),
                )
                assert cur.fetchall() == []
        finally:
            conn.close()
    finally:
        cleanup(pg_dsn, fx)
