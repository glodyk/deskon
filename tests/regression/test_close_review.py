"""Regression test close_review terhadap behavior close_review_v1.py.

Yang dibandingkan adalah hasil bisnis, bukan id mentah: field review, status,
final_decision, closed_by, event_type/entity_type/event_data, dan relasi FK.
Id dari database diterjemahkan ke label ("claim", "review", "verifier", ...)
sebelum dibandingkan.

Behavior lama yang dipertahankan (script v1):
- final_decision di-strip dan di-upper, harus LAYAK/TIDAK_LAYAK/RESELEKSI;
- resolution_note di-strip dan wajib tidak kosong;
- keduanya divalidasi sebelum database disentuh;
- user harus ada dan aktif, dicek sebelum review;
- review harus ada dan OPEN; review CLOSED tidak diubah;
- review menjadi CLOSED dengan final_decision, resolution_note, closed_by,
  closed_at, plus satu event REVIEW_CLOSED, dalam satu transaksi;
- finding, reselection, komentar, dan klaim tidak disentuh.

Perubahan yang disepakati: event_data lewat audit_service (identitas
claim_id/nosjp/review_id dari review; kunci lama final_decision dan
resolution_note tetap), lock FOR NO KEY UPDATE OF cr (bukan FOR UPDATE tanpa
OF yang ikut mengunci baris klaim), tanpa print, tanpa commit.
"""

import threading
import uuid

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

from deskon.db import transaction
from deskon.errors import (
    AuditEventError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service, review_service
from deskon.services.finding_service import create_review_finding
from deskon.services.review_service import close_review, open_review_cycle

VPK = "VERIFIKASI_PASCA_KLAIM"
NOTE = "Koding sesuai resume medis."

FINDING = {
    "finding_category": "KODING",
    "finding_title": "Diagnosis sekunder tidak didukung",
    "finding_description": "G50.0 tidak didukung resume medis.",
}


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn):
    """Hospital, dua user aktif, satu user tidak aktif, klaim, review OPEN cycle 1."""
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
        for label, active in (("verifier", True), ("closer", True), ("inactive", False)):
            cur.execute(
                "INSERT INTO core.users (full_name, email, password_hash, role, is_active) "
                "VALUES (%s, %s, 'x', 'BPJS_VERIFIER', %s) RETURNING id",
                (label, f"{label}-{tag}@test.local", active),
            )
            users[label] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
            "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01') RETURNING id",
            (nosjp, hospital_id, VPK, f"RS{tag}"),
        )
        claim_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
            "VALUES (%s, %s, 1, %s) RETURNING id",
            (claim_id, users["verifier"], VPK),
        )
        review_id = cur.fetchone()[0]
    return {
        "nosjp": nosjp,
        "hospital_id": hospital_id,
        "claim_id": claim_id,
        "review_id": review_id,
        **users,
    }


def add_reselection(conn, fx, status="PROPOSED"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review.claim_reselections (
                claim_id, review_id, target_type, action, original_code,
                proposed_code, reason, status, created_by
            )
            VALUES (%s, %s, 'DIAGNOSIS', 'CHANGE', 'G50.0', 'G50.1', 'Tidak didukung', %s, %s)
            RETURNING id
            """,
            (fx["claim_id"], fx["review_id"], status, fx["verifier"]),
        )
        return cur.fetchone()[0]


def cleanup(pg_dsn, fx):
    """Hapus data uji yang sudah di-commit."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            reviews = "(SELECT id FROM review.claim_reviews WHERE claim_id = %s)"
            cur.execute(f"DELETE FROM review.review_events WHERE review_id IN {reviews}", (fx["claim_id"],))
            cur.execute(f"DELETE FROM review.review_findings WHERE review_id IN {reviews}", (fx["claim_id"],))
            cur.execute("DELETE FROM review.claim_reselections WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM review.preliminary_notes WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM review.claim_reviews WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM core.claims WHERE id = %s", (fx["claim_id"],))
            cur.execute(
                "DELETE FROM core.users WHERE id IN (%s, %s, %s)",
                (fx["verifier"], fx["closer"], fx["inactive"]),
            )
            cur.execute("DELETE FROM core.hospitals WHERE id = %s", (fx["hospital_id"],))
    finally:
        conn.close()


@pytest.fixture
def fx(conn):
    """Data uji dengan review OPEN di transaksi tes (di-rollback setelah tes)."""
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


def close(conn, fx, closed_by=None, final_decision="LAYAK", resolution_note=NOTE):
    return close_review(
        conn,
        review_id=fx["review_id"],
        closed_by=fx["closer"] if closed_by is None else closed_by,
        final_decision=final_decision,
        resolution_note=resolution_note,
    )


# ---------------------------------------------------------------------------
# Snapshot bisnis (tanpa id mentah)
# ---------------------------------------------------------------------------

REVIEW_COLUMNS = (
    "id, claim_id, status, final_decision, resolution_note, opened_by, opened_at, "
    "closed_by, closed_at, created_at, updated_at, cycle_no, review_type"
)


def review_row(conn, review_id):
    """Seluruh kolom satu review, sebagai dict."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {REVIEW_COLUMNS} FROM review.claim_reviews WHERE id = %s", (review_id,))
        row = cur.fetchone()
        names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def business_review(conn, fx):
    """Field bisnis review fx, id diganti label."""
    row = review_row(conn, fx["review_id"])
    users = {fx["verifier"]: "verifier", fx["closer"]: "closer", None: None}
    return {
        "claim": "claim" if row["claim_id"] == fx["claim_id"] else row["claim_id"],
        "status": row["status"],
        "final_decision": row["final_decision"],
        "resolution_note": row["resolution_note"],
        "opened_by": users[row["opened_by"]],
        "closed_by": users[row["closed_by"]],
        "closed_at_set": row["closed_at"] is not None,
        "cycle_no": row["cycle_no"],
        "review_type": row["review_type"],
    }


def business_events(conn, fx):
    """Event review fx, id diganti label."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT review_id, user_id, event_type, entity_type, entity_id, event_data
            FROM review.review_events
            WHERE review_id = %s
            ORDER BY id
            """,
            (fx["review_id"],),
        )
        rows = cur.fetchall()
    users = {fx["verifier"]: "verifier", fx["closer"]: "closer"}

    def label(value):
        return "review" if value == fx["review_id"] else value

    events = []
    for review_id, user_id, event_type, entity_type, entity_id, event_data in rows:
        data = dict(event_data)
        data["claim_id"] = "claim" if data["claim_id"] == fx["claim_id"] else data["claim_id"]
        data["review_id"] = label(data["review_id"])
        events.append({
            "review": label(review_id),
            "user": users[user_id],
            "event_type": event_type,
            "entity_type": entity_type,
            "entity": label(entity_id) if entity_type == "claim_review" else "other",
            "event_data": data,
        })
    return events


def closed_review(fx, decision="LAYAK", note=NOTE, closed_by="closer"):
    return {
        "claim": "claim",
        "status": "CLOSED",
        "final_decision": decision,
        "resolution_note": note,
        "opened_by": "verifier",
        "closed_by": closed_by,
        "closed_at_set": True,
        "cycle_no": 1,
        "review_type": VPK,
    }


def closed_event(fx, decision="LAYAK", note=NOTE, user="closer"):
    return {
        "review": "review",
        "user": user,
        "event_type": "REVIEW_CLOSED",
        "entity_type": "claim_review",
        "entity": "review",
        "event_data": {
            "claim_id": "claim",
            "nosjp": fx["nosjp"],
            "review_id": "review",
            "final_decision": decision,
            "resolution_note": note,
        },
    }


def count_closed_events(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM review.review_events "
            "WHERE review_id = %s AND event_type = 'REVIEW_CLOSED'",
            (fx["review_id"],),
        )
        return cur.fetchone()[0]


def review_status(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        return business_review(conn, fx)["status"], count_closed_events(conn, fx)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Penutupan sukses
# ---------------------------------------------------------------------------

def test_open_review_is_closed_with_decision_note_and_closer(conn, fx):
    before = review_row(conn, fx["review_id"])

    result = close(conn, fx)

    assert business_review(conn, fx) == closed_review(fx)
    after = review_row(conn, fx["review_id"])
    for column in ("claim_id", "opened_by", "opened_at", "created_at", "cycle_no", "review_type"):
        assert after[column] == before[column]
    assert after["closed_at"] is not None
    assert after["updated_at"] >= before["updated_at"]

    review = result.review
    assert review.id == fx["review_id"]
    assert review.claim_id == fx["claim_id"]
    assert review.nosjp == fx["nosjp"]
    assert review.status == "CLOSED"
    assert review.final_decision == "LAYAK"
    assert review.resolution_note == NOTE
    assert review.closed_by == fx["closer"]
    assert review.closed_at == after["closed_at"]
    assert review.opened_by == fx["verifier"]
    assert review.cycle_no == 1
    assert review.review_type == VPK


@pytest.mark.parametrize("decision", ["LAYAK", "TIDAK_LAYAK", "RESELEKSI"])
def test_each_final_decision_is_accepted(conn, fx, decision):
    close(conn, fx, final_decision=decision)
    assert business_review(conn, fx) == closed_review(fx, decision=decision)


@pytest.mark.parametrize(
    "given, stored",
    [(" layak ", "LAYAK"), ("tidak_layak", "TIDAK_LAYAK"), ("\tReseleksi\n", "RESELEKSI")],
)
def test_final_decision_is_normalised_like_the_old_script(conn, fx, given, stored):
    result = close(conn, fx, final_decision=given)
    assert result.review.final_decision == stored
    assert business_review(conn, fx)["final_decision"] == stored
    assert business_events(conn, fx)[0]["event_data"]["final_decision"] == stored


def test_resolution_note_is_stripped(conn, fx):
    close(conn, fx, resolution_note="  Baris satu\nbaris dua \t\n")
    assert business_review(conn, fx)["resolution_note"] == "Baris satu\nbaris dua"


def test_closer_may_differ_from_opener(conn, fx):
    close(conn, fx, closed_by=fx["closer"])
    close_row = business_review(conn, fx)
    assert (close_row["opened_by"], close_row["closed_by"]) == ("verifier", "closer")


def test_opener_may_close_their_own_review(conn, fx):
    close(conn, fx, closed_by=fx["verifier"])
    assert business_review(conn, fx) == closed_review(fx, closed_by="verifier")


def test_draft_findings_and_proposed_reselections_do_not_block_and_stay_unchanged(conn, fx):
    create_review_finding(conn, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING)
    proposed = add_reselection(conn, fx, "PROPOSED")
    draft = add_reselection(conn, fx, "DRAFT")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review.review_findings WHERE review_id = %s", (fx["review_id"],))
        findings_before = cur.fetchall()
        cur.execute("SELECT * FROM review.claim_reselections WHERE id IN (%s, %s) ORDER BY id", (proposed, draft))
        reselections_before = cur.fetchall()

    close(conn, fx, final_decision="RESELEKSI")

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review.review_findings WHERE review_id = %s", (fx["review_id"],))
        assert cur.fetchall() == findings_before
        cur.execute("SELECT * FROM review.claim_reselections WHERE id IN (%s, %s) ORDER BY id", (proposed, draft))
        assert cur.fetchall() == reselections_before
    assert [row[0] for row in findings_before]
    assert findings_before[0][5] == "DRAFT"
    assert [row[10] for row in reselections_before] == ["PROPOSED", "DRAFT"]


def test_reseleksi_without_reselection_is_allowed(conn, fx):
    close(conn, fx, final_decision="RESELEKSI")
    assert business_review(conn, fx)["final_decision"] == "RESELEKSI"


def test_layak_with_a_reselection_is_allowed(conn, fx):
    add_reselection(conn, fx, "PROPOSED")
    close(conn, fx, final_decision="LAYAK")
    assert business_review(conn, fx)["final_decision"] == "LAYAK"


def test_claim_and_other_review_tables_are_untouched(conn, fx):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM core.claims WHERE id = %s", (fx["claim_id"],))
        claim_before = cur.fetchone()

    close(conn, fx)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM core.claims WHERE id = %s", (fx["claim_id"],))
        assert cur.fetchone() == claim_before
        for table in ("review_findings", "review_comments", "claim_reselections"):
            cur.execute(f"SELECT count(*) FROM review.{table} WHERE review_id = %s", (fx["review_id"],))
            assert cur.fetchone()[0] == 0, table


# ---------------------------------------------------------------------------
# Review CLOSED tidak diubah; cycle baru untuk review berikutnya
# ---------------------------------------------------------------------------

def test_closed_review_is_rejected_and_left_unchanged(conn, fx):
    close(conn, fx, final_decision="LAYAK", resolution_note="Pertama")
    row_before = review_row(conn, fx["review_id"])
    events_before = business_events(conn, fx)

    with pytest.raises(InvalidStateError, match="status saat ini adalah CLOSED"):
        close(conn, fx, closed_by=fx["verifier"], final_decision="TIDAK_LAYAK", resolution_note="Kedua")

    assert review_row(conn, fx["review_id"]) == row_before
    assert business_events(conn, fx) == events_before


def test_closing_twice_with_the_same_decision_is_still_rejected(conn, fx):
    close(conn, fx)
    with pytest.raises(InvalidStateError):
        close(conn, fx)
    assert count_closed_events(conn, fx) == 1


def test_new_cycle_after_close_leaves_the_closed_review_intact(conn, fx):
    close(conn, fx)
    closed_before = review_row(conn, fx["review_id"])
    events_before = business_events(conn, fx)

    opened = open_review_cycle(conn, nosjp=fx["nosjp"], opened_by=fx["verifier"])

    assert opened.created is True
    assert opened.review.id != fx["review_id"]
    assert opened.review.cycle_no == 2
    assert opened.review.status == "OPEN"
    assert opened.previous_review_ids == (fx["review_id"],)
    assert review_row(conn, fx["review_id"]) == closed_before
    assert business_events(conn, fx) == events_before
    with pytest.raises(InvalidStateError):
        create_review_finding(conn, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

def test_exactly_one_review_closed_event_with_old_keys_plus_review_id(conn, fx):
    result = close(conn, fx, final_decision="TIDAK_LAYAK")

    assert business_events(conn, fx) == [closed_event(fx, decision="TIDAK_LAYAK")]
    event = result.event
    assert event.event_type == "REVIEW_CLOSED"
    assert event.entity_type == "claim_review"
    assert event.entity_id == fx["review_id"]
    assert event.user_id == fx["closer"]
    assert set(event.event_data) == {"claim_id", "nosjp", "review_id", "final_decision", "resolution_note"}


def test_event_identity_comes_from_the_review(conn, fx):
    event = close(conn, fx).event
    assert event.event_data["claim_id"] == fx["claim_id"]
    assert event.event_data["nosjp"] == fx["nosjp"]
    assert event.event_data["review_id"] == fx["review_id"]


@pytest.mark.parametrize(
    "note",
    ['Kutip "LAYAK"', "Backslash di akhir \\", "Baris satu\nbaris dua\ttab", "Catatan dokter ✓ é ü 日本"],
)
def test_event_data_is_valid_json_for_special_characters(conn, fx, note):
    close(conn, fx, resolution_note=note)
    assert business_review(conn, fx)["resolution_note"] == note
    assert business_events(conn, fx)[0]["event_data"]["resolution_note"] == note


def test_earlier_events_are_kept(conn, fx):
    create_review_finding(conn, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING)
    before = business_events(conn, fx)

    close(conn, fx)

    after = business_events(conn, fx)
    assert after[:-1] == before
    assert [e["event_type"] for e in after] == ["FINDING_CREATED", "REVIEW_CLOSED"]


# ---------------------------------------------------------------------------
# Validasi
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["DITOLAK", "", "   ", "LAYAK_SEBAGIAN"])
def test_invalid_decision_is_rejected(conn, fx, decision):
    with pytest.raises(ValidationError, match="final_decision tidak valid"):
        close(conn, fx, final_decision=decision)
    assert business_review(conn, fx)["status"] == "OPEN"


@pytest.mark.parametrize("note", ["", "   ", "\n\t", None])
def test_resolution_note_is_required(conn, fx, note):
    with pytest.raises(ValidationError, match="resolution_note wajib diisi"):
        close(conn, fx, resolution_note=note)
    assert business_review(conn, fx)["status"] == "OPEN"
    assert count_closed_events(conn, fx) == 0


class _NoDatabase:
    def cursor(self):
        raise AssertionError("database tidak boleh disentuh")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"final_decision": "DITOLAK"}, "final_decision tidak valid"),
        ({"resolution_note": " "}, "resolution_note wajib diisi"),
        ({"final_decision": "DITOLAK", "resolution_note": ""}, "final_decision tidak valid"),
    ],
)
def test_input_is_rejected_before_the_database_is_read(overrides, message):
    kwargs = {"review_id": 1, "closed_by": 1, "final_decision": "LAYAK", "resolution_note": NOTE}
    kwargs.update(overrides)
    with pytest.raises(ValidationError, match=message):
        close_review(_NoDatabase(), **kwargs)


@pytest.mark.parametrize("field", ["final_decision", "resolution_note"])
def test_non_string_text_is_rejected(conn, fx, field):
    with pytest.raises(ValidationError, match="harus string"):
        close(conn, fx, **{field: 1})


@pytest.mark.parametrize("name, value", [("review_id", "1"), ("review_id", True), ("closed_by", 1.0), ("closed_by", None)])
def test_ids_must_be_integers(conn, fx, name, value):
    kwargs = {"review_id": fx["review_id"], "closed_by": fx["closer"], "final_decision": "LAYAK", "resolution_note": NOTE}
    kwargs[name] = value
    with pytest.raises(ValidationError, match=f"{name} harus integer"):
        close_review(conn, **kwargs)


def test_unknown_review_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="Review id="):
        close_review(conn, review_id=fx["review_id"] + 10_000, closed_by=fx["closer"],
                     final_decision="LAYAK", resolution_note=NOTE)


def test_unknown_user_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="User id="):
        close(conn, fx, closed_by=fx["inactive"] + 10_000)
    assert business_review(conn, fx)["status"] == "OPEN"


def test_inactive_user_is_rejected(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        close(conn, fx, closed_by=fx["inactive"])
    assert business_review(conn, fx)["status"] == "OPEN"
    assert count_closed_events(conn, fx) == 0


def test_user_is_validated_before_the_review(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        close_review(conn, review_id=fx["review_id"] + 10_000, closed_by=fx["inactive"],
                     final_decision="LAYAK", resolution_note=NOTE)


# ---------------------------------------------------------------------------
# Transaksi
# ---------------------------------------------------------------------------

def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    a = psycopg2.connect(**pg_dsn)
    b = psycopg2.connect(**pg_dsn)
    try:
        close(a, fx)
        assert business_review(b, fx)["status"] == "OPEN"
        assert count_closed_events(b, fx) == 0
    finally:
        a.rollback()
        a.close()
        b.close()


def test_callers_rollback_discards_close_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        close(conn, fx)
        conn.rollback()
        assert business_review(conn, fx)["status"] == "OPEN"
        assert count_closed_events(conn, fx) == 0
    finally:
        conn.close()


def test_transaction_commits_close_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            close(conn, fx)
    finally:
        conn.close()
    assert review_status(pg_dsn, fx) == ("CLOSED", 1)


def test_audit_failure_rolls_back_the_close(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx

    def failing_record_event(*args, **kwargs):
        raise AuditEventError("audit gagal")

    monkeypatch.setattr(audit_service, "record_event", failing_record_event)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                review_service.close_review(
                    conn, review_id=fx["review_id"], closed_by=fx["closer"],
                    final_decision="LAYAK", resolution_note=NOTE,
                )
    finally:
        conn.close()
    assert review_status(pg_dsn, fx) == ("OPEN", 0)


# ---------------------------------------------------------------------------
# Konkurensi
# ---------------------------------------------------------------------------

def _run_in_thread(pg_dsn, work):
    """Jalankan work(conn) di thread lain dalam transaksi sendiri."""
    outcome = {}

    def target():
        conn = psycopg2.connect(**pg_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '10s'")
            with transaction(conn):
                outcome["result"] = work(conn)
        except Exception as exc:  # diperiksa oleh tes
            outcome["error"] = exc
        finally:
            conn.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _assert_waiting(thread, what):
    thread.join(timeout=0.5)
    assert thread.is_alive(), f"{what} seharusnya menunggu"


def test_close_waits_for_a_finding_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    creator = psycopg2.connect(**pg_dsn)
    try:
        create_review_finding(creator, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING)

        thread, outcome = _run_in_thread(pg_dsn, lambda c: close(c, fx))
        _assert_waiting(thread, "penutupan review")
        creator.commit()
        thread.join(timeout=10)
    finally:
        creator.close()

    assert "error" not in outcome
    assert outcome["result"].review.status == "CLOSED"
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert [e["event_type"] for e in business_events(conn, fx)] == ["FINDING_CREATED", "REVIEW_CLOSED"]
    finally:
        conn.close()


def _create_finding(fx):
    return lambda c: create_review_finding(
        c, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING
    )


def test_finding_waits_for_close_and_is_rejected_after_commit(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)

        thread, outcome = _run_in_thread(pg_dsn, _create_finding(fx))
        _assert_waiting(thread, "pembuatan finding")
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert isinstance(outcome.get("error"), InvalidStateError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert [e["event_type"] for e in business_events(conn, fx)] == ["REVIEW_CLOSED"]
    finally:
        conn.close()


def test_finding_proceeds_if_close_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)

        thread, outcome = _run_in_thread(pg_dsn, _create_finding(fx))
        _assert_waiting(thread, "pembuatan finding")
        closer.rollback()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert "error" not in outcome
    assert outcome["result"].finding.status == "DRAFT"
    assert review_status(pg_dsn, fx) == ("OPEN", 0)


def test_concurrent_close_waits_and_is_rejected_after_first_commits(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        close(first, fx, closed_by=fx["closer"], final_decision="LAYAK", resolution_note="Pertama")

        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: close(c, fx, closed_by=fx["verifier"], final_decision="TIDAK_LAYAK",
                            resolution_note="Kedua"),
        )
        _assert_waiting(thread, "penutupan kedua")
        first.commit()
        thread.join(timeout=10)
    finally:
        first.close()

    assert isinstance(outcome.get("error"), InvalidStateError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert business_review(conn, fx) == closed_review(fx, note="Pertama")
        assert business_events(conn, fx) == [closed_event(fx, note="Pertama")]
    finally:
        conn.close()


def test_concurrent_close_proceeds_if_first_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        close(first, fx, final_decision="LAYAK", resolution_note="Pertama")

        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: close(c, fx, closed_by=fx["verifier"], final_decision="TIDAK_LAYAK",
                            resolution_note="Kedua"),
        )
        _assert_waiting(thread, "penutupan kedua")
        first.rollback()
        thread.join(timeout=10)
    finally:
        first.close()

    assert "error" not in outcome
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert business_review(conn, fx) == closed_review(
            fx, decision="TIDAK_LAYAK", note="Kedua", closed_by="verifier"
        )
        assert business_events(conn, fx) == [
            closed_event(fx, decision="TIDAK_LAYAK", note="Kedua", user="verifier")
        ]
    finally:
        conn.close()


def _open_cycle(fx):
    return lambda c: open_review_cycle(c, nosjp=fx["nosjp"], opened_by=fx["verifier"])


def test_open_cycle_waits_for_close_and_opens_the_next_cycle(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)

        thread, outcome = _run_in_thread(pg_dsn, _open_cycle(fx))
        _assert_waiting(thread, "pembukaan cycle")
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert "error" not in outcome
    opened = outcome["result"]
    assert opened.created is True
    assert opened.review.cycle_no == 2
    assert opened.previous_review_ids == (fx["review_id"],)
    assert review_status(pg_dsn, fx) == ("CLOSED", 1)


def test_open_cycle_returns_the_open_review_if_close_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)

        thread, outcome = _run_in_thread(pg_dsn, _open_cycle(fx))
        _assert_waiting(thread, "pembukaan cycle")
        closer.rollback()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert "error" not in outcome
    assert outcome["result"].created is False
    assert outcome["result"].review.id == fx["review_id"]
    assert review_status(pg_dsn, fx) == ("OPEN", 0)


def _lock_attempt(pg_dsn, sql, params):
    """Coba ambil lock tanpa menunggu; True jika berhasil."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(sql, params)
                return True
            except pg_errors.LockNotAvailable:
                return False
    finally:
        conn.rollback()
        conn.close()


def test_lock_mode_conflicts_with_share_but_not_with_key_share(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)
        review_sql = "SELECT id FROM review.claim_reviews WHERE id = %s FOR {} NOWAIT"
        claim_sql = "SELECT id FROM core.claims WHERE id = %s FOR {} NOWAIT"
        review = (fx["review_id"],)
        claim = (fx["claim_id"],)

        assert not _lock_attempt(pg_dsn, review_sql.format("SHARE"), review)
        assert not _lock_attempt(pg_dsn, review_sql.format("NO KEY UPDATE"), review)
        assert _lock_attempt(pg_dsn, review_sql.format("KEY SHARE"), review)
        # Baris klaim tidak dikunci sama sekali (OF cr).
        assert _lock_attempt(pg_dsn, claim_sql.format("KEY SHARE"), claim)
        assert _lock_attempt(pg_dsn, claim_sql.format("UPDATE"), claim)
    finally:
        closer.rollback()
        closer.close()


def test_inserts_referencing_the_claim_or_review_are_not_blocked(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    other = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)
        with other.cursor() as cur:
            cur.execute("SET lock_timeout = '2s'")
            cur.execute(
                "INSERT INTO review.preliminary_notes (claim_id, note, created_by) VALUES (%s, 'Catatan', %s)",
                (fx["claim_id"], fx["verifier"]),
            )
        add_reselection(other, fx, "DRAFT")
    finally:
        other.rollback()
        other.close()
        closer.rollback()
        closer.close()
