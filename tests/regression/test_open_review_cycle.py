"""Regression test open_review_cycle terhadap behavior create_new_review_cycle_v1.py.

Yang dibandingkan adalah hasil bisnis, bukan id mentah: status, review_type,
cycle_no, opened_by, event_type/entity_type/event_data, dan relasi FK.
Id dari database diterjemahkan ke label ("claim", "cycle1", "verifier", ...)
oleh business_snapshot() sebelum dibandingkan.

Behavior lama yang dipertahankan (script v1):
- user harus ada dan aktif;
- klaim dicari lewat Nosjp, tidak ada = error;
- klaim dengan review OPEN: tidak membuat apa pun, mengembalikan review itu;
- selain itu: insert review OPEN + satu event, dalam satu transaksi.

Perubahan yang disepakati: cycle_no, review_type, event REVIEW_OPENED
(bukan REVIEW_CYCLE_CREATED) lewat audit_service, tanpa print, tanpa commit.
"""

import threading
import uuid

import psycopg2
import pytest

from deskon.constants import EntityType, EventType
from deskon.db import transaction
from deskon.errors import (
    AuditEventError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service, review_service
from deskon.services.review_service import open_review_cycle

VPK = "VERIFIKASI_PASCA_KLAIM"
AAK = "AUDIT_ADMINISTRASI_KLAIM"
PENDING = "PENDING"


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn, claim_status=VPK):
    """Hospital, user aktif, user tidak aktif, dan satu klaim tanpa review."""
    tag = uuid.uuid4().hex[:12]
    nosjp = f"0001R001{tag}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.hospitals (kdppklayan, hospital_name) "
            "VALUES (%s, 'RS Test') RETURNING id",
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
        cur.execute(
            "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
            "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01') RETURNING id",
            (nosjp, hospital_id, claim_status, f"RS{tag}"),
        )
        claim_id = cur.fetchone()[0]
    return {
        "nosjp": nosjp,
        "hospital_id": hospital_id,
        "claim_id": claim_id,
        "verifier": users["verifier"],
        "inactive": users["inactive"],
    }


def add_closed_review(conn, fx, cycle_no, review_type, decision="LAYAK"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review.claim_reviews (
                claim_id, status, final_decision, resolution_note,
                opened_by, closed_by, closed_at, cycle_no, review_type
            )
            VALUES (%s, 'CLOSED', %s, 'catatan lama', %s, %s, CURRENT_TIMESTAMP, %s, %s)
            RETURNING id
            """,
            (fx["claim_id"], decision, fx["verifier"], fx["verifier"], cycle_no, review_type),
        )
        return cur.fetchone()[0]


def set_claim_status(conn, fx, claim_status):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.claims SET claim_status = %s WHERE id = %s",
            (claim_status, fx["claim_id"]),
        )


def cleanup(pg_dsn, fx):
    """Hapus data uji yang sudah di-commit."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM review.review_events WHERE review_id IN "
                "(SELECT id FROM review.claim_reviews WHERE claim_id = %s)",
                (fx["claim_id"],),
            )
            cur.execute("DELETE FROM review.claim_reviews WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM core.claims WHERE id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM core.users WHERE id IN (%s, %s)", (fx["verifier"], fx["inactive"]))
            cur.execute("DELETE FROM core.hospitals WHERE id = %s", (fx["hospital_id"],))
    finally:
        conn.close()


@pytest.fixture
def fx(conn):
    """Data uji di transaksi tes (di-rollback setelah tes)."""
    return seed(conn)


@pytest.fixture
def committed_fx(pg_dsn):
    """Data uji yang di-commit, untuk tes lintas koneksi/transaksi."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            data = seed(conn)
    finally:
        conn.close()
    yield data
    cleanup(pg_dsn, data)


# ---------------------------------------------------------------------------
# Snapshot bisnis (tanpa id mentah)
# ---------------------------------------------------------------------------

def business_snapshot(conn, fx):
    """Review dan event untuk klaim fx, dengan id diganti label."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, claim_id, status, final_decision, review_type, cycle_no,
                   opened_by, closed_by
            FROM review.claim_reviews
            WHERE claim_id = %s
            ORDER BY cycle_no
            """,
            (fx["claim_id"],),
        )
        review_rows = cur.fetchall()
        cur.execute(
            """
            SELECT e.review_id, e.user_id, e.event_type, e.entity_type,
                   e.entity_id, e.event_data
            FROM review.review_events AS e
            JOIN review.claim_reviews AS cr ON cr.id = e.review_id
            WHERE cr.claim_id = %s
            ORDER BY e.id
            """,
            (fx["claim_id"],),
        )
        event_rows = cur.fetchall()

    review_label = {row[0]: f"cycle{row[5]}" for row in review_rows}
    user_label = {fx["verifier"]: "verifier", fx["inactive"]: "inactive"}
    claim_label = {fx["claim_id"]: "claim"}

    reviews = [
        {
            "review": review_label[rid],
            "claim": claim_label[claim_id],
            "status": status,
            "final_decision": decision,
            "review_type": review_type,
            "cycle_no": cycle_no,
            "opened_by": user_label[opened_by],
            "closed_by": user_label.get(closed_by),
        }
        for rid, claim_id, status, decision, review_type, cycle_no, opened_by, closed_by in review_rows
    ]

    def label_data(data):
        data = dict(data)
        data["claim_id"] = claim_label[data["claim_id"]]
        for key in ("review_id", "new_review_id"):
            if key in data:
                data[key] = review_label[data[key]]
        if "previous_review_ids" in data:
            data["previous_review_ids"] = [review_label[i] for i in data["previous_review_ids"]]
        return data

    events = [
        {
            "review": review_label[review_id],
            "user": user_label[user_id],
            "event_type": event_type,
            "entity_type": entity_type,
            "entity": review_label.get(entity_id),
            "event_data": label_data(event_data),
        }
        for review_id, user_id, event_type, entity_type, entity_id, event_data in event_rows
    ]
    return {"reviews": reviews, "events": events}


def open_review(cycle_no, review_type, opened_by="verifier"):
    return {
        "review": f"cycle{cycle_no}",
        "claim": "claim",
        "status": "OPEN",
        "final_decision": None,
        "review_type": review_type,
        "cycle_no": cycle_no,
        "opened_by": opened_by,
        "closed_by": None,
    }


def closed_review(cycle_no, review_type, decision="LAYAK"):
    return {
        "review": f"cycle{cycle_no}",
        "claim": "claim",
        "status": "CLOSED",
        "final_decision": decision,
        "review_type": review_type,
        "cycle_no": cycle_no,
        "opened_by": "verifier",
        "closed_by": "verifier",
    }


def opened_event(fx, cycle_no, review_type, source, previous):
    review = f"cycle{cycle_no}"
    return {
        "review": review,
        "user": "verifier",
        "event_type": "REVIEW_OPENED",
        "entity_type": "claim_review",
        "entity": review,
        "event_data": {
            "claim_id": "claim",
            "nosjp": fx["nosjp"],
            "review_id": review,
            "new_review_id": review,
            "cycle_no": cycle_no,
            "review_type": review_type,
            "review_type_source": source,
            "previous_review_count": len(previous),
            "previous_review_ids": previous,
        },
    }


def count_rows(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM review.claim_reviews WHERE claim_id = %s", (fx["claim_id"],)
        )
        reviews = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM review.review_events e "
            "JOIN review.claim_reviews cr ON cr.id = e.review_id WHERE cr.claim_id = %s",
            (fx["claim_id"],),
        )
        events = cur.fetchone()[0]
    return reviews, events


# ---------------------------------------------------------------------------
# Klaim tanpa review
# ---------------------------------------------------------------------------

def test_claim_without_review_opens_cycle_1(conn, fx):
    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])

    assert result.created is True
    assert result.previous_review_ids == ()
    assert result.review.claim_id == fx["claim_id"]
    assert result.review.nosjp == fx["nosjp"]
    assert result.review.status == "OPEN"
    assert result.review.cycle_no == 1
    assert result.review.review_type == VPK
    assert result.review.opened_by == fx["verifier"]
    assert result.event.event_type == EventType.REVIEW_OPENED
    assert result.event.entity_type == EntityType.CLAIM_REVIEW
    assert result.event.entity_id == result.review.id

    assert business_snapshot(conn, fx) == {
        "reviews": [open_review(1, VPK)],
        "events": [opened_event(fx, 1, VPK, "claim_status", [])],
    }


def test_nosjp_is_trimmed_like_the_old_script(conn, fx):
    result = open_review_cycle(conn, nosjp=f"  {fx['nosjp']}  ", opened_by=fx["verifier"])
    assert result.created is True
    assert result.review.nosjp == fx["nosjp"]


def test_explicit_review_type_wins_over_claim_status(conn, fx):
    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"], review_type=AAK)

    assert result.review.review_type == AAK
    assert business_snapshot(conn, fx) == {
        "reviews": [open_review(1, AAK)],
        "events": [opened_event(fx, 1, AAK, "explicit", [])],
    }


def test_event_keeps_the_old_scripts_event_data_keys(conn, fx):
    """event_data lama: claim_id, nosjp, new_review_id, previous_review_count,
    previous_review_ids. Semua tetap ada dengan arti yang sama."""
    closed = add_closed_review(conn, fx, 1, VPK)
    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
    data = result.event.event_data

    assert data["claim_id"] == fx["claim_id"]
    assert data["nosjp"] == fx["nosjp"]
    assert data["new_review_id"] == result.review.id
    assert data["previous_review_count"] == 1
    assert data["previous_review_ids"] == [closed]


# ---------------------------------------------------------------------------
# Klaim dengan review CLOSED
# ---------------------------------------------------------------------------

def test_claim_with_closed_review_opens_cycle_2_and_keeps_history(conn, fx):
    add_closed_review(conn, fx, 1, VPK, decision="RESELEKSI")
    before = business_snapshot(conn, fx)

    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])

    assert result.created is True
    assert result.review.cycle_no == 2
    after = business_snapshot(conn, fx)
    assert after["reviews"][0] == before["reviews"][0] == closed_review(1, VPK, "RESELEKSI")
    assert after == {
        "reviews": [closed_review(1, VPK, "RESELEKSI"), open_review(2, VPK)],
        "events": [opened_event(fx, 2, VPK, "claim_status", ["cycle1"])],
    }


def test_review_type_follows_current_claim_status_and_history_does_not_change(conn, fx):
    add_closed_review(conn, fx, 1, VPK, decision="RESELEKSI")
    set_claim_status(conn, fx, AAK)

    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])

    assert result.review.review_type == AAK
    reviews = business_snapshot(conn, fx)["reviews"]
    assert [(r["cycle_no"], r["review_type"]) for r in reviews] == [(1, VPK), (2, AAK)]


def test_cycle_no_continues_after_the_highest_cycle(conn, fx):
    """cycle_no = cycle terakhir + 1, bukan jumlah review + 1."""
    add_closed_review(conn, fx, 1, VPK)
    add_closed_review(conn, fx, 3, VPK)

    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])

    assert result.review.cycle_no == 4
    assert result.event.event_data["previous_review_count"] == 2


# ---------------------------------------------------------------------------
# Klaim yang sudah punya review OPEN
# ---------------------------------------------------------------------------

def test_claim_with_open_review_returns_it_without_creating(conn, fx):
    add_closed_review(conn, fx, 1, VPK)
    first = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
    before = business_snapshot(conn, fx)

    again = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"], review_type=AAK)

    assert again.created is False
    assert again.event is None
    assert again.previous_review_ids == ()
    assert again.review == first.review
    assert again.review.review_type == VPK
    assert business_snapshot(conn, fx) == before


def test_open_review_is_returned_even_for_another_active_user(conn, fx):
    first = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.users (full_name, email, password_hash, role) "
            "VALUES ('Lain', %s, 'x', 'BPJS_VERIFIER') RETURNING id",
            (f"lain-{fx['nosjp']}@test.local",),
        )
        other = cur.fetchone()[0]

    again = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=other)

    assert again.created is False
    assert again.review.opened_by == fx["verifier"]
    assert count_rows(conn, fx) == (1, 1)


# ---------------------------------------------------------------------------
# Validasi
# ---------------------------------------------------------------------------

def test_unknown_user_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="tidak ditemukan"):
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"] + 10_000)
    assert count_rows(conn, fx) == (0, 0)


def test_inactive_user_is_rejected(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["inactive"])
    assert count_rows(conn, fx) == (0, 0)


def test_inactive_user_is_rejected_even_if_review_is_open(conn, fx):
    """Seperti script lama, user divalidasi sebelum review OPEN dicari."""
    open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
    with pytest.raises(InvalidStateError):
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["inactive"])


def test_unknown_nosjp_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="Nosjp=TIDAK-ADA"):
        open_review_cycle(conn, nosjp="TIDAK-ADA", opened_by=fx["verifier"])


@pytest.mark.parametrize("nosjp", ["", "   ", None, 123])
def test_empty_nosjp_is_rejected(conn, fx, nosjp):
    with pytest.raises(ValidationError):
        open_review_cycle(conn, nosjp=nosjp, opened_by=fx["verifier"])


@pytest.mark.parametrize("opened_by", [None, "1", 1.0, True])
def test_opened_by_must_be_an_integer(conn, fx, opened_by):
    with pytest.raises(ValidationError):
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=opened_by)


@pytest.mark.parametrize("review_type", ["", "OPEN", "verifikasi_pasca_klaim", "LAYAK"])
def test_invalid_review_type_is_rejected(conn, fx, review_type):
    with pytest.raises(ValidationError, match="review_type tidak valid"):
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"], review_type=review_type)
    assert count_rows(conn, fx) == (0, 0)


@pytest.mark.parametrize("review_type", [PENDING, VPK, AAK])
def test_all_three_review_types_are_accepted(conn, fx, review_type):
    result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"], review_type=review_type)
    assert result.review.review_type == review_type


# ---------------------------------------------------------------------------
# Transaksi dan rollback
# ---------------------------------------------------------------------------

def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    a = psycopg2.connect(**pg_dsn)
    b = psycopg2.connect(**pg_dsn)
    try:
        open_review_cycle(a, nosjp=fx["nosjp"], opened_by=fx["verifier"])
        assert count_rows(a, fx) == (1, 1)
        assert count_rows(b, fx) == (0, 0)
        a.commit()
        b.rollback()
        assert count_rows(b, fx) == (1, 1)
    finally:
        a.close()
        b.close()


def test_callers_rollback_discards_review_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
        conn.rollback()
        assert count_rows(conn, fx) == (0, 0)
    finally:
        conn.close()


def test_exception_inside_transaction_block_rolls_back_both(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
                raise RuntimeError("langkah berikutnya gagal")
        assert count_rows(conn, fx) == (0, 0)

        with transaction(conn):
            result = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
        assert result.review.cycle_no == 1
        assert count_rows(conn, fx) == (1, 1)
    finally:
        conn.close()


def test_audit_failure_rolls_back_the_new_review(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx

    def failing_record_event(*args, **kwargs):
        raise AuditEventError("audit gagal")

    monkeypatch.setattr(audit_service, "record_event", failing_record_event)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                review_service.open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
        assert count_rows(conn, fx) == (0, 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Konkurensi
# ---------------------------------------------------------------------------

def _open_in_thread(pg_dsn, fx, outcome):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '10s'")
        with transaction(conn):
            outcome["result"] = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])
    except Exception as exc:  # diperiksa oleh tes
        outcome["error"] = exc
    finally:
        conn.close()


def _start(pg_dsn, fx):
    outcome = {}
    thread = threading.Thread(target=_open_in_thread, args=(pg_dsn, fx, outcome))
    thread.start()
    return thread, outcome


def test_concurrent_open_waits_and_returns_the_first_review(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        created = open_review_cycle(first, nosjp=fx["nosjp"], opened_by=fx["verifier"])

        thread, outcome = _start(pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive(), "pemanggil kedua seharusnya menunggu kunci klaim"

        first.commit()
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        first.close()

    assert "error" not in outcome
    assert outcome["result"].created is False
    assert outcome["result"].review.id == created.review.id
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (1, 1)
    finally:
        conn.close()


def test_concurrent_open_after_first_rolls_back_creates_cycle_1(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        open_review_cycle(first, nosjp=fx["nosjp"], opened_by=fx["verifier"])
        thread, outcome = _start(pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive()
        first.rollback()
        thread.join(timeout=10)
    finally:
        first.close()

    assert "error" not in outcome
    assert outcome["result"].created is True
    assert outcome["result"].review.cycle_no == 1


def test_unique_constraints_backstop_a_writer_that_skips_the_lock(pg_dsn, committed_fx):
    """Penulis lain (mis. script lama) yang insert review OPEN tanpa mengunci
    klaim: service menunggu di index unik lalu melempar ConflictError."""
    fx = committed_fx
    other = psycopg2.connect(**pg_dsn)
    try:
        with other.cursor() as cur:
            cur.execute(
                "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
                "VALUES (%s, %s, 1, %s)",
                (fx["claim_id"], fx["verifier"], VPK),
            )
        thread, outcome = _start(pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive()
        other.commit()
        thread.join(timeout=10)
    finally:
        other.close()

    assert isinstance(outcome.get("error"), ConflictError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (1, 0)
    finally:
        conn.close()
