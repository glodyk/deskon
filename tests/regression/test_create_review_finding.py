"""Regression test create_review_finding terhadap behavior create_review_finding_v1.py.

Yang dibandingkan adalah hasil bisnis, bukan id mentah: field finding, status,
created_by, event_type/entity_type/event_data, dan relasi FK. Id dari database
diterjemahkan ke label ("claim", "review", "finding1", "verifier", ...) oleh
business_snapshot() sebelum dibandingkan.

Behavior lama yang dipertahankan (script v1):
- category, title, description di-strip dan tidak boleh kosong;
- user harus ada dan aktif;
- review harus ada dan OPEN; review CLOSED ditolak;
- finding disimpan DRAFT dengan created_by, plus satu event FINDING_CREATED,
  dalam satu transaksi.

Perubahan yang disepakati: event_data lewat audit_service (json.dumps, bukan
string sambung) dengan identitas claim_id/nosjp/review_id dari review,
entity_type kanonik review_finding (bukan REVIEW_FINDING), tanpa print,
tanpa commit.
"""

import json
import threading
import uuid

import psycopg2
import pytest

from deskon.constants import EntityType, EventType
from deskon.db import transaction
from deskon.errors import (
    AuditEventError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service, finding_service
from deskon.services.finding_service import create_review_finding

VPK = "VERIFIKASI_PASCA_KLAIM"

FINDING = {
    "finding_category": "KODING",
    "finding_title": "Diagnosis sekunder tidak didukung",
    "finding_description": "G50.0 tidak didukung resume medis.",
}


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn, review_status="OPEN"):
    """Hospital, user aktif, user tidak aktif, klaim, dan satu review."""
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
            (nosjp, hospital_id, VPK, f"RS{tag}"),
        )
        claim_id = cur.fetchone()[0]
        if review_status == "OPEN":
            cur.execute(
                "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
                "VALUES (%s, %s, 1, %s) RETURNING id",
                (claim_id, users["verifier"], VPK),
            )
        else:
            cur.execute(
                """
                INSERT INTO review.claim_reviews (
                    claim_id, status, final_decision, opened_by, closed_by,
                    closed_at, cycle_no, review_type
                )
                VALUES (%s, 'CLOSED', 'LAYAK', %s, %s, CURRENT_TIMESTAMP, 1, %s)
                RETURNING id
                """,
                (claim_id, users["verifier"], users["verifier"], VPK),
            )
        review_id = cur.fetchone()[0]
    return {
        "nosjp": nosjp,
        "hospital_id": hospital_id,
        "claim_id": claim_id,
        "review_id": review_id,
        "verifier": users["verifier"],
        "inactive": users["inactive"],
    }


def close_review(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE review.claim_reviews "
            "SET status = 'CLOSED', final_decision = 'LAYAK', closed_by = %s, "
            "closed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (fx["verifier"], fx["review_id"]),
        )


def cleanup(pg_dsn, fx):
    """Hapus data uji yang sudah di-commit."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM review.review_events WHERE review_id = %s", (fx["review_id"],))
            cur.execute("DELETE FROM review.review_findings WHERE review_id = %s", (fx["review_id"],))
            cur.execute("DELETE FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
            cur.execute("DELETE FROM core.claims WHERE id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM core.users WHERE id IN (%s, %s)", (fx["verifier"], fx["inactive"]))
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


def create(conn, fx, created_by=None, **overrides):
    fields = {**FINDING, **overrides}
    return create_review_finding(
        conn,
        review_id=fx["review_id"],
        created_by=fx["verifier"] if created_by is None else created_by,
        **fields,
    )


# ---------------------------------------------------------------------------
# Snapshot bisnis (tanpa id mentah)
# ---------------------------------------------------------------------------

def business_snapshot(conn, fx):
    """Finding dan event untuk review fx, dengan id diganti label."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, review_id, finding_category, finding_title,
                   finding_description, status, created_by, published_by,
                   published_at, corrects_finding_id
            FROM review.review_findings
            WHERE review_id = %s
            ORDER BY id
            """,
            (fx["review_id"],),
        )
        finding_rows = cur.fetchall()
        cur.execute(
            """
            SELECT review_id, user_id, event_type, entity_type, entity_id, event_data
            FROM review.review_events
            WHERE review_id = %s
            ORDER BY id
            """,
            (fx["review_id"],),
        )
        event_rows = cur.fetchall()

    finding_label = {row[0]: f"finding{n}" for n, row in enumerate(finding_rows, start=1)}
    user_label = {fx["verifier"]: "verifier", fx["inactive"]: "inactive"}
    review_label = {fx["review_id"]: "review"}

    findings = [
        {
            "finding": finding_label[fid],
            "review": review_label[review_id],
            "finding_category": category,
            "finding_title": title,
            "finding_description": description,
            "status": status,
            "created_by": user_label[created_by],
            "published_by": published_by,
            "published_at": published_at,
            "corrects_finding_id": corrects,
        }
        for (fid, review_id, category, title, description, status,
             created_by, published_by, published_at, corrects) in finding_rows
    ]

    def label_data(data):
        data = dict(data)
        data["claim_id"] = "claim" if data["claim_id"] == fx["claim_id"] else data["claim_id"]
        data["review_id"] = review_label[data["review_id"]]
        data["finding_id"] = finding_label[data["finding_id"]]
        return data

    events = [
        {
            "review": review_label[review_id],
            "user": user_label[user_id],
            "event_type": event_type,
            "entity_type": entity_type,
            "entity": finding_label[entity_id],
            "event_data": label_data(event_data),
        }
        for review_id, user_id, event_type, entity_type, entity_id, event_data in event_rows
    ]
    return {"findings": findings, "events": events}


def draft_finding(n, **fields):
    return {
        "finding": f"finding{n}",
        "review": "review",
        **{**FINDING, **fields},
        "status": "DRAFT",
        "created_by": "verifier",
        "published_by": None,
        "published_at": None,
        "corrects_finding_id": None,
    }


def created_event(fx, n, category=FINDING["finding_category"]):
    finding = f"finding{n}"
    return {
        "review": "review",
        "user": "verifier",
        "event_type": "FINDING_CREATED",
        "entity_type": "review_finding",
        "entity": finding,
        "event_data": {
            "claim_id": "claim",
            "nosjp": fx["nosjp"],
            "review_id": "review",
            "finding_id": finding,
            "finding_category": category,
            "status": "DRAFT",
        },
    }


def count_rows(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM review.review_findings WHERE review_id = %s", (fx["review_id"],)
        )
        findings = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM review.review_events WHERE review_id = %s", (fx["review_id"],)
        )
        events = cur.fetchone()[0]
    return findings, events


# ---------------------------------------------------------------------------
# Finding sukses
# ---------------------------------------------------------------------------

def test_finding_is_created_as_draft_on_open_review(conn, fx):
    result = create(conn, fx)

    finding = result.finding
    assert finding.review_id == fx["review_id"]
    assert finding.claim_id == fx["claim_id"]
    assert finding.nosjp == fx["nosjp"]
    assert finding.finding_category == FINDING["finding_category"]
    assert finding.finding_title == FINDING["finding_title"]
    assert finding.finding_description == FINDING["finding_description"]
    assert finding.status == "DRAFT"
    assert finding.created_by == fx["verifier"]
    assert finding.created_at is not None
    assert finding.updated_at is not None

    assert result.event.event_type == EventType.FINDING_CREATED
    assert result.event.entity_type == EntityType.REVIEW_FINDING
    assert result.event.entity_id == finding.id
    assert result.event.review_id == fx["review_id"]
    assert result.event.user_id == fx["verifier"]

    assert business_snapshot(conn, fx) == {
        "findings": [draft_finding(1)],
        "events": [created_event(fx, 1)],
    }


def test_text_fields_are_stripped_like_the_old_script(conn, fx):
    result = create(
        conn,
        fx,
        finding_category="  KODING \n",
        finding_title="\tJudul  ",
        finding_description="  Deskripsi\nbaris dua  ",
    )
    assert result.finding.finding_category == "KODING"
    assert result.finding.finding_title == "Judul"
    assert result.finding.finding_description == "Deskripsi\nbaris dua"
    assert business_snapshot(conn, fx) == {
        "findings": [draft_finding(
            1,
            finding_category="KODING",
            finding_title="Judul",
            finding_description="Deskripsi\nbaris dua",
        )],
        "events": [created_event(fx, 1, category="KODING")],
    }


def test_several_findings_on_the_same_review(conn, fx):
    create(conn, fx)
    create(conn, fx, finding_category="ADMINISTRASI")

    assert business_snapshot(conn, fx) == {
        "findings": [draft_finding(1), draft_finding(2, finding_category="ADMINISTRASI")],
        "events": [created_event(fx, 1), created_event(fx, 2, category="ADMINISTRASI")],
    }


def test_review_stays_open_and_unchanged(conn, fx):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
        before = cur.fetchone()
        create(conn, fx)
        cur.execute("SELECT * FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
        assert cur.fetchone() == before


# ---------------------------------------------------------------------------
# Event: payload dan entity_type
# ---------------------------------------------------------------------------

def test_event_uses_canonical_entity_type_not_legacy(conn, fx):
    result = create(conn, fx)
    assert result.event.entity_type == "review_finding"
    assert result.event.entity_type != "REVIEW_FINDING"
    assert result.event.canonical_entity_type == "review_finding"


def test_event_keeps_the_old_scripts_event_data_keys(conn, fx):
    """event_data lama: finding_category dan status. Keduanya tetap ada dengan
    arti yang sama; identitas dan finding_id ditambahkan (aditif)."""
    result = create(conn, fx)
    data = result.event.event_data

    assert data["finding_category"] == FINDING["finding_category"]
    assert data["status"] == "DRAFT"
    assert data["claim_id"] == fx["claim_id"]
    assert data["nosjp"] == fx["nosjp"]
    assert data["review_id"] == fx["review_id"]
    assert data["finding_id"] == result.finding.id
    assert set(data) == {"claim_id", "nosjp", "review_id", "finding_id", "finding_category", "status"}


def test_event_identity_comes_from_the_review(conn, fx):
    result = create(conn, fx)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.nosjp FROM review.claim_reviews cr "
            "JOIN core.claims c ON c.id = cr.claim_id WHERE cr.id = %s",
            (fx["review_id"],),
        )
        claim_id, nosjp = cur.fetchone()
    assert result.event.event_data["claim_id"] == claim_id
    assert result.event.event_data["nosjp"] == nosjp


@pytest.mark.parametrize(
    "category",
    ['KODING "RESUME"', "KODING\\G50", "KODING\nBARIS", "KODING ü é"],
)
def test_event_data_is_valid_json_for_special_characters(conn, fx, category):
    """Script lama menyambung string; backslash/baris baru merusak JSON-nya."""
    result = create(conn, fx, finding_category=category)
    assert result.event.event_data["finding_category"] == category
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_data::text FROM review.review_events WHERE id = %s",
            (result.event.id,),
        )
        assert json.loads(cur.fetchone()[0])["finding_category"] == category


def test_title_and_description_are_not_in_event_data(conn, fx):
    result = create(conn, fx)
    assert "finding_title" not in result.event.event_data
    assert "finding_description" not in result.event.event_data


# ---------------------------------------------------------------------------
# Review tidak OPEN / tidak ada
# ---------------------------------------------------------------------------

def test_closed_review_is_rejected(conn):
    fx = seed(conn, review_status="CLOSED")
    with pytest.raises(InvalidStateError, match="sudah CLOSED"):
        create(conn, fx)
    assert count_rows(conn, fx) == (0, 0)


def test_review_closed_after_a_finding_rejects_the_next(conn, fx):
    create(conn, fx)
    close_review(conn, fx)
    with pytest.raises(InvalidStateError, match="hanya dapat dibuat pada review OPEN"):
        create(conn, fx)
    assert count_rows(conn, fx) == (1, 1)


def test_unknown_review_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="tidak ditemukan"):
        create_review_finding(
            conn, review_id=fx["review_id"] + 10_000, created_by=fx["verifier"], **FINDING
        )
    assert count_rows(conn, fx) == (0, 0)


# ---------------------------------------------------------------------------
# Validasi user
# ---------------------------------------------------------------------------

def test_unknown_user_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="User .* tidak ditemukan"):
        create(conn, fx, created_by=fx["verifier"] + 10_000)
    assert count_rows(conn, fx) == (0, 0)


def test_inactive_user_is_rejected(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        create(conn, fx, created_by=fx["inactive"])
    assert count_rows(conn, fx) == (0, 0)


def test_user_is_validated_before_the_review(conn, fx):
    """Seperti script lama: user dicek dulu, baru review."""
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        create_review_finding(
            conn, review_id=fx["review_id"] + 10_000, created_by=fx["inactive"], **FINDING
        )


# ---------------------------------------------------------------------------
# Validasi input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", list(FINDING))
@pytest.mark.parametrize("value", ["", "   ", "\n\t", None])
def test_empty_text_is_rejected(conn, fx, field, value):
    with pytest.raises(ValidationError, match=f"{field} tidak boleh kosong"):
        create(conn, fx, **{field: value})
    assert count_rows(conn, fx) == (0, 0)


def test_empty_text_is_rejected_before_the_database_is_read(fx):
    """Seperti script lama: input dicek sebelum query apa pun."""

    class NoDatabase:
        def cursor(self):
            raise AssertionError("database tidak boleh disentuh")

    with pytest.raises(ValidationError):
        create_review_finding(
            NoDatabase(), review_id=fx["review_id"], created_by=fx["verifier"],
            finding_category=" ", finding_title="x", finding_description="x",
        )


@pytest.mark.parametrize("field", list(FINDING))
def test_non_string_text_is_rejected(conn, fx, field):
    with pytest.raises(ValidationError, match="harus string"):
        create(conn, fx, **{field: 123})


@pytest.mark.parametrize("field,limit", [("finding_category", 100), ("finding_title", 255)])
def test_text_longer_than_the_column_is_rejected(conn, fx, field, limit):
    create(conn, fx, **{field: "x" * limit})
    with pytest.raises(ValidationError, match=f"{field} maksimal {limit}"):
        create(conn, fx, **{field: "x" * (limit + 1)})
    assert count_rows(conn, fx) == (1, 1)


@pytest.mark.parametrize("name", ["review_id", "created_by"])
@pytest.mark.parametrize("value", [None, "1", 1.0, True])
def test_ids_must_be_integers(conn, fx, name, value):
    kwargs = {"review_id": fx["review_id"], "created_by": fx["verifier"], **FINDING}
    kwargs[name] = value
    with pytest.raises(ValidationError, match=f"{name} harus integer"):
        create_review_finding(conn, **kwargs)


# ---------------------------------------------------------------------------
# Transaksi dan rollback
# ---------------------------------------------------------------------------

def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    a = psycopg2.connect(**pg_dsn)
    b = psycopg2.connect(**pg_dsn)
    try:
        create(a, fx)
        assert count_rows(a, fx) == (1, 1)
        assert count_rows(b, fx) == (0, 0)
        a.commit()
        b.rollback()
        assert count_rows(b, fx) == (1, 1)
    finally:
        a.close()
        b.close()


def test_callers_rollback_discards_finding_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        create(conn, fx)
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
                create(conn, fx)
                raise RuntimeError("langkah berikutnya gagal")
        assert count_rows(conn, fx) == (0, 0)

        with transaction(conn):
            create(conn, fx)
        assert business_snapshot(conn, fx) == {
            "findings": [draft_finding(1)],
            "events": [created_event(fx, 1)],
        }
    finally:
        conn.close()


def test_audit_failure_rolls_back_the_new_finding(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx

    def failing_record_event(*args, **kwargs):
        raise AuditEventError("audit gagal")

    monkeypatch.setattr(audit_service, "record_event", failing_record_event)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                finding_service.create_review_finding(
                    conn, review_id=fx["review_id"], created_by=fx["verifier"], **FINDING
                )
        assert count_rows(conn, fx) == (0, 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Konkurensi dengan penutupan review
# ---------------------------------------------------------------------------

def _create_in_thread(pg_dsn, fx, outcome):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '10s'")
        with transaction(conn):
            outcome["result"] = create(conn, fx)
    except Exception as exc:  # diperiksa oleh tes
        outcome["error"] = exc
    finally:
        conn.close()


def _close_in_thread(pg_dsn, fx, outcome):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '10s'")
        with transaction(conn):
            close_review(conn, fx)
        outcome["closed"] = True
    except Exception as exc:  # diperiksa oleh tes
        outcome["error"] = exc
    finally:
        conn.close()


def _start(target, pg_dsn, fx):
    outcome = {}
    thread = threading.Thread(target=target, args=(pg_dsn, fx, outcome))
    thread.start()
    return thread, outcome


def test_finding_waits_for_a_closing_review_and_is_then_rejected(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close_review(closer, fx)

        thread, outcome = _start(_create_in_thread, pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive(), "pembuatan finding seharusnya menunggu penutupan review"
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert isinstance(outcome.get("error"), InvalidStateError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (0, 0)
    finally:
        conn.close()


def test_finding_proceeds_if_the_closing_review_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close_review(closer, fx)

        thread, outcome = _start(_create_in_thread, pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive()
        closer.rollback()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert "error" not in outcome
    assert outcome["result"].finding.status == "DRAFT"
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (1, 1)
    finally:
        conn.close()


def test_closing_waits_for_a_finding_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    creator = psycopg2.connect(**pg_dsn)
    try:
        create(creator, fx)

        thread, outcome = _start(_close_in_thread, pg_dsn, fx)
        thread.join(timeout=0.5)
        assert thread.is_alive(), "penutupan review seharusnya menunggu finding selesai"
        creator.commit()
        thread.join(timeout=10)
    finally:
        creator.close()

    assert outcome == {"closed": True}
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (1, 1)
    finally:
        conn.close()


def test_concurrent_findings_on_the_same_review_do_not_block(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        create(first, fx)

        thread, outcome = _start(_create_in_thread, pg_dsn, fx)
        thread.join(timeout=10)
        assert not thread.is_alive(), "finding kedua tidak boleh menunggu finding pertama"
        assert "error" not in outcome
        first.commit()
    finally:
        first.close()

    conn = psycopg2.connect(**pg_dsn)
    try:
        assert count_rows(conn, fx) == (2, 2)
    finally:
        conn.close()
