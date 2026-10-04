"""Regression test create_reselection terhadap behavior create_reselection_v1.py.

Yang dibandingkan adalah hasil bisnis, bukan id mentah: field reselection,
status, created_by, event_type/entity_type/event_data, dan relasi FK. Id dari
database diterjemahkan ke label ("claim", "review", "reselection1", ...)
sebelum dibandingkan.

Behavior lama yang dipertahankan (script v1):
- action CHANGE/DROP dan target_type DIAGNOSIS/PROCEDURE di-strip + upper;
- original_code dan reason di-strip dan wajib; description kosong -> NULL;
- CHANGE wajib proposed_code; DROP menyimpan proposed_* NULL;
- user harus ada dan aktif, dicek sebelum review;
- review harus ada dan OPEN; claim_id diambil dari review;
- reselection disimpan PROPOSED dengan created_by, plus satu event
  RESELECTION_CREATED, dalam satu transaksi;
- tidak ada pemeriksaan keberadaan original_code pada klaim, original ==
  proposed, atau duplikat.

Perubahan yang disepakati: DROP yang membawa proposed_* ditolak (script tidak
punya jalur untuk itu), event_data lewat audit_service dengan tambahan
review_id, lock review FOR SHARE OF cr (bukan FOR UPDATE tanpa OF yang ikut
mengunci klaim), validasi panjang kode, tanpa print, tanpa commit.
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
from deskon.services import audit_service, reselection_service
from deskon.services.finding_service import create_review_finding
from deskon.services.reselection_service import create_reselection
from deskon.services.review_service import close_review, open_review_cycle

VPK = "VERIFIKASI_PASCA_KLAIM"

CHANGE = {
    "target_type": "DIAGNOSIS",
    "action": "CHANGE",
    "original_code": "G50.0",
    "original_description": "Trigeminal neuralgia",
    "proposed_code": "G50.1",
    "proposed_description": "Atypical facial pain",
    "reason": "Diagnosis tidak sesuai resume medis.",
}

DROP = {
    "target_type": "PROCEDURE",
    "action": "DROP",
    "original_code": "89.07",
    "original_description": "Konsultasi",
    "reason": "Prosedur tidak didukung dokumentasi.",
}


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn, review_status="OPEN"):
    """Hospital, user aktif, user tidak aktif, dua klaim, dan satu review di klaim pertama."""
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
        claims = []
        for suffix in ("A", "B"):
            nosjp = f"0001R001{tag}{suffix}"
            cur.execute(
                "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
                "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01') RETURNING id",
                (nosjp, hospital_id, VPK, f"RS{tag}"),
            )
            claims.append((cur.fetchone()[0], nosjp))
        claim_id, nosjp = claims[0]
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
        "other_claim_id": claims[1][0],
        "review_id": review_id,
        "verifier": users["verifier"],
        "inactive": users["inactive"],
    }


def cleanup(pg_dsn, fx):
    """Hapus data uji yang sudah di-commit."""
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            reviews = "(SELECT id FROM review.claim_reviews WHERE claim_id = %s)"
            cur.execute(f"DELETE FROM review.review_events WHERE review_id IN {reviews}", (fx["claim_id"],))
            cur.execute(f"DELETE FROM review.review_findings WHERE review_id IN {reviews}", (fx["claim_id"],))
            cur.execute("DELETE FROM review.claim_reselections WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM review.claim_reviews WHERE claim_id = %s", (fx["claim_id"],))
            cur.execute("DELETE FROM core.claims WHERE id IN (%s, %s)", (fx["claim_id"], fx["other_claim_id"]))
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
    base = DROP if str(overrides.get("action", "CHANGE")).strip().upper() == "DROP" else CHANGE
    fields = {**base, **overrides}
    return create_reselection(
        conn,
        review_id=fx["review_id"],
        created_by=fx["verifier"] if created_by is None else created_by,
        **fields,
    )


# ---------------------------------------------------------------------------
# Snapshot bisnis (tanpa id mentah)
# ---------------------------------------------------------------------------

def business_snapshot(conn, fx):
    """Reselection dan event untuk review fx, dengan id diganti label."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, claim_id, review_id, target_type, action, original_code,
                   original_description, proposed_code, proposed_description,
                   reason, status, created_by, agreed_by, agreed_at,
                   corrects_reselection_id
            FROM review.claim_reselections
            WHERE review_id = %s
            ORDER BY id
            """,
            (fx["review_id"],),
        )
        rows = cur.fetchall()
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

    label = {row[0]: f"reselection{n}" for n, row in enumerate(rows, start=1)}
    user_label = {fx["verifier"]: "verifier", fx["inactive"]: "inactive"}

    reselections = [
        {
            "reselection": label[rid],
            "claim": "claim" if claim_id == fx["claim_id"] else claim_id,
            "review": "review" if review_id == fx["review_id"] else review_id,
            "target_type": target_type,
            "action": action,
            "original_code": original_code,
            "original_description": original_description,
            "proposed_code": proposed_code,
            "proposed_description": proposed_description,
            "reason": reason,
            "status": status,
            "created_by": user_label[created_by],
            "agreed_by": agreed_by,
            "agreed_at": agreed_at,
            "corrects_reselection_id": corrects,
        }
        for (rid, claim_id, review_id, target_type, action, original_code,
             original_description, proposed_code, proposed_description, reason,
             status, created_by, agreed_by, agreed_at, corrects) in rows
    ]

    def label_data(data):
        data = dict(data)
        data["claim_id"] = "claim" if data["claim_id"] == fx["claim_id"] else data["claim_id"]
        data["review_id"] = "review" if data["review_id"] == fx["review_id"] else data["review_id"]
        if "reselection_id" in data:
            data["reselection_id"] = label[data["reselection_id"]]
        return data

    events = [
        {
            "review": "review" if review_id == fx["review_id"] else review_id,
            "user": user_label[user_id],
            "event_type": event_type,
            "entity_type": entity_type,
            "entity": label.get(entity_id, entity_id),
            "event_data": label_data(event_data),
        }
        for review_id, user_id, event_type, entity_type, entity_id, event_data in event_rows
    ]
    return {"reselections": reselections, "events": events}


def proposed_reselection(n, **fields):
    base = {
        "reselection": f"reselection{n}",
        "claim": "claim",
        "review": "review",
        "target_type": CHANGE["target_type"],
        "action": CHANGE["action"],
        "original_code": CHANGE["original_code"],
        "original_description": CHANGE["original_description"],
        "proposed_code": CHANGE["proposed_code"],
        "proposed_description": CHANGE["proposed_description"],
        "reason": CHANGE["reason"],
        "status": "PROPOSED",
        "created_by": "verifier",
        "agreed_by": None,
        "agreed_at": None,
        "corrects_reselection_id": None,
    }
    base.update(fields)
    return base


def created_event(fx, n, **data):
    event_data = {
        "claim_id": "claim",
        "nosjp": fx["nosjp"],
        "review_id": "review",
        "reselection_id": f"reselection{n}",
        "target_type": CHANGE["target_type"],
        "action": CHANGE["action"],
        "original_code": CHANGE["original_code"],
        "proposed_code": CHANGE["proposed_code"],
    }
    event_data.update(data)
    return {
        "review": "review",
        "user": "verifier",
        "event_type": "RESELECTION_CREATED",
        "entity_type": "claim_reselection",
        "entity": f"reselection{n}",
        "event_data": event_data,
    }


def count_rows(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM review.claim_reselections WHERE review_id = %s", (fx["review_id"],)
        )
        reselections = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM review.review_events WHERE review_id = %s", (fx["review_id"],)
        )
        events = cur.fetchone()[0]
    return reselections, events


def committed_counts(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        return count_rows(conn, fx)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reselection sukses
# ---------------------------------------------------------------------------

def test_change_is_created_as_proposed_on_open_review(conn, fx):
    result = create(conn, fx)

    assert business_snapshot(conn, fx) == {
        "reselections": [proposed_reselection(1)],
        "events": [created_event(fx, 1)],
    }
    reselection = result.reselection
    assert reselection.review_id == fx["review_id"]
    assert reselection.claim_id == fx["claim_id"]
    assert reselection.nosjp == fx["nosjp"]
    assert reselection.status == "PROPOSED"
    assert reselection.created_by == fx["verifier"]
    assert reselection.created_at is not None
    assert reselection.agreed_by is None and reselection.agreed_at is None
    assert reselection.corrects_reselection_id is None
    assert result.event.event_type == "RESELECTION_CREATED"


def test_drop_is_created_with_null_proposed_fields(conn, fx):
    create(conn, fx, **DROP)

    snapshot = business_snapshot(conn, fx)
    assert snapshot["reselections"] == [
        proposed_reselection(
            1,
            target_type="PROCEDURE",
            action="DROP",
            original_code="89.07",
            original_description="Konsultasi",
            proposed_code=None,
            proposed_description=None,
            reason=DROP["reason"],
        )
    ]
    assert snapshot["events"] == [
        created_event(fx, 1, target_type="PROCEDURE", action="DROP",
                      original_code="89.07", proposed_code=None)
    ]


@pytest.mark.parametrize("target_type", ["DIAGNOSIS", "PROCEDURE"])
@pytest.mark.parametrize("action", ["CHANGE", "DROP"])
def test_every_target_type_and_action_combination(conn, fx, target_type, action):
    fields = {**(CHANGE if action == "CHANGE" else DROP), "target_type": target_type, "action": action}
    create(conn, fx, **fields)
    row = business_snapshot(conn, fx)["reselections"][0]
    assert (row["target_type"], row["action"], row["status"]) == (target_type, action, "PROPOSED")


def test_text_is_normalised_like_the_old_script(conn, fx):
    create(
        conn, fx,
        target_type=" diagnosis ", action="\tchange\n",
        original_code="  g50.0  ", original_description="   ",
        proposed_code=" G50.1\n", proposed_description="\t",
        reason="  Alasan\nbaris dua  ",
    )
    assert business_snapshot(conn, fx)["reselections"] == [
        proposed_reselection(
            1,
            original_code="g50.0",  # kode tidak di-upper-case, hanya di-strip
            original_description=None,
            proposed_code="G50.1",
            proposed_description=None,
            reason="Alasan\nbaris dua",
        )
    ]


def test_descriptions_are_optional(conn, fx):
    create(conn, fx, original_description=None, proposed_description=None)
    row = business_snapshot(conn, fx)["reselections"][0]
    assert row["original_description"] is None and row["proposed_description"] is None


def test_claim_id_comes_from_the_review(conn, fx):
    result = create(conn, fx)
    assert result.reselection.claim_id == fx["claim_id"]
    assert result.reselection.claim_id != fx["other_claim_id"]
    assert result.event.event_data["claim_id"] == fx["claim_id"]
    assert result.event.event_data["nosjp"] == fx["nosjp"]


def test_claim_id_cannot_be_supplied_by_the_caller(conn, fx):
    with pytest.raises(TypeError):
        create_reselection(
            conn, review_id=fx["review_id"], created_by=fx["verifier"],
            claim_id=fx["other_claim_id"], **CHANGE,
        )
    assert count_rows(conn, fx) == (0, 0)


def test_status_is_proposed_and_correction_fields_are_null(conn, fx):
    create(conn, fx)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, corrects_reselection_id, agreed_by, agreed_at "
            "FROM review.claim_reselections WHERE review_id = %s",
            (fx["review_id"],),
        )
        assert cur.fetchone() == ("PROPOSED", None, None, None)


def test_corrects_reselection_id_is_not_a_parameter(conn, fx):
    with pytest.raises(TypeError):
        create(conn, fx, corrects_reselection_id=1)


def test_status_is_not_a_parameter(conn, fx):
    with pytest.raises(TypeError):
        create(conn, fx, status="DRAFT")


def test_several_reselections_on_the_same_review_including_duplicates(conn, fx):
    create(conn, fx)
    create(conn, fx)
    create(conn, fx, **DROP)
    snapshot = business_snapshot(conn, fx)
    assert [r["reselection"] for r in snapshot["reselections"]] == ["reselection1", "reselection2", "reselection3"]
    assert [e["event_type"] for e in snapshot["events"]] == ["RESELECTION_CREATED"] * 3


def test_no_new_business_rules_original_equal_to_proposed_and_unknown_codes(conn, fx):
    # Tidak ada baris claim_diagnoses sama sekali: original_code tidak diverifikasi.
    create(conn, fx, original_code="ZZZ.9", proposed_code="ZZZ.9")
    row = business_snapshot(conn, fx)["reselections"][0]
    assert (row["original_code"], row["proposed_code"]) == ("ZZZ.9", "ZZZ.9")


def test_review_and_other_tables_stay_unchanged(conn, fx):
    create_review_finding(
        conn, review_id=fx["review_id"], created_by=fx["verifier"],
        finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
        review_before = cur.fetchone()
        cur.execute("SELECT * FROM core.claims WHERE id = %s", (fx["claim_id"],))
        claim_before = cur.fetchone()
        cur.execute("SELECT * FROM review.review_findings WHERE review_id = %s", (fx["review_id"],))
        findings_before = cur.fetchall()

    create(conn, fx)

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
        assert cur.fetchone() == review_before
        cur.execute("SELECT * FROM core.claims WHERE id = %s", (fx["claim_id"],))
        assert cur.fetchone() == claim_before
        cur.execute("SELECT * FROM review.review_findings WHERE review_id = %s", (fx["review_id"],))
        assert cur.fetchall() == findings_before


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

def test_event_uses_canonical_names_and_the_creator(conn, fx):
    result = create(conn, fx)
    event = result.event
    assert event.event_type == "RESELECTION_CREATED"
    assert event.entity_type == "claim_reselection"
    assert event.entity_id == result.reselection.id
    assert event.user_id == fx["verifier"]
    assert event.review_id == fx["review_id"]


def test_event_data_keeps_the_old_keys_plus_review_id(conn, fx):
    event = create(conn, fx).event
    assert set(event.event_data) == {
        "reselection_id", "claim_id", "nosjp", "target_type", "action",
        "original_code", "proposed_code", "review_id",
    }
    assert event.event_data["review_id"] == fx["review_id"]


def test_event_data_proposed_code_is_json_null_for_drop(conn, fx):
    event = create(conn, fx, **DROP).event
    assert "proposed_code" in event.event_data
    assert event.event_data["proposed_code"] is None


def test_descriptions_and_reason_are_not_in_event_data(conn, fx):
    event = create(conn, fx).event
    for key in ("reason", "original_description", "proposed_description", "status"):
        assert key not in event.event_data


@pytest.mark.parametrize(
    "code",
    ['G50 "0"', "Backslash \\", "Baris\nbaru\ttab", "Kode ✓ é 日本"],
)
def test_event_data_is_valid_json_for_special_characters(conn, fx, code):
    create(conn, fx, original_code=code, proposed_code=code, reason=code)
    row = business_snapshot(conn, fx)
    assert row["reselections"][0]["original_code"] == code
    assert row["reselections"][0]["reason"] == code
    assert row["events"][0]["event_data"]["original_code"] == code
    assert row["events"][0]["event_data"]["proposed_code"] == code


def test_earlier_events_are_kept(conn, fx):
    create_review_finding(
        conn, review_id=fx["review_id"], created_by=fx["verifier"],
        finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
    )
    before = business_snapshot(conn, fx)["events"]
    create(conn, fx)
    after = business_snapshot(conn, fx)["events"]
    assert after[:-1] == before
    assert [e["event_type"] for e in after] == ["FINDING_CREATED", "RESELECTION_CREATED"]


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def test_closed_review_is_rejected(conn):
    fx = seed(conn, review_status="CLOSED")
    with pytest.raises(InvalidStateError, match="tidak OPEN. Status saat ini: CLOSED"):
        create(conn, fx)
    assert count_rows(conn, fx) == (0, 0)


def test_review_closed_after_a_reselection_rejects_the_next(conn, fx):
    create(conn, fx)
    close_review(conn, review_id=fx["review_id"], closed_by=fx["verifier"],
                 final_decision="RESELEKSI", resolution_note="Selesai.")
    with pytest.raises(InvalidStateError):
        create(conn, fx)
    assert count_rows(conn, fx)[0] == 1


def test_unknown_review_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="Review id="):
        create_reselection(conn, review_id=fx["review_id"] + 10_000, created_by=fx["verifier"], **CHANGE)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

def test_unknown_user_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="User id="):
        create(conn, fx, created_by=fx["inactive"] + 10_000)
    assert count_rows(conn, fx) == (0, 0)


def test_inactive_user_is_rejected(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        create(conn, fx, created_by=fx["inactive"])
    assert count_rows(conn, fx) == (0, 0)


def test_user_is_validated_before_the_review(conn, fx):
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        create_reselection(conn, review_id=fx["review_id"] + 10_000, created_by=fx["inactive"], **CHANGE)


# ---------------------------------------------------------------------------
# Validasi input
# ---------------------------------------------------------------------------

class _NoDatabase:
    def cursor(self):
        raise AssertionError("database tidak boleh disentuh")


def reject(overrides, message, base=CHANGE):
    kwargs = {"review_id": 1, "created_by": 1, **base}
    kwargs.update(overrides)
    with pytest.raises(ValidationError, match=message):
        create_reselection(_NoDatabase(), **kwargs)


@pytest.mark.parametrize("action", ["UBAH", "", "   ", "change_drop"])
def test_invalid_action_is_rejected_before_the_database_is_read(action):
    reject({"action": action}, "action tidak valid")


@pytest.mark.parametrize("target_type", ["LAB", "", "   ", "DIAG"])
def test_invalid_target_type_is_rejected_before_the_database_is_read(target_type):
    reject({"target_type": target_type}, "target_type tidak valid")


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_missing_original_code_is_rejected_before_the_database_is_read(value):
    reject({"original_code": value}, "original_code wajib diisi")


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_missing_reason_is_rejected_before_the_database_is_read(value):
    reject({"reason": value}, "reason wajib diisi")


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_change_without_proposed_code_is_rejected_before_the_database_is_read(value):
    reject({"proposed_code": value}, "proposed_code wajib diisi untuk action CHANGE")


@pytest.mark.parametrize(
    "overrides",
    [
        {"proposed_code": "G50.1"},
        {"proposed_description": "Deskripsi"},
        {"proposed_code": "G50.1", "proposed_description": "Deskripsi"},
    ],
)
def test_drop_with_proposed_fields_is_rejected_not_ignored(conn, fx, overrides):
    with pytest.raises(ValidationError, match="tidak boleh diisi untuk action DROP"):
        create(conn, fx, **{**DROP, **overrides})
    assert count_rows(conn, fx) == (0, 0)


@pytest.mark.parametrize("overrides", [{}, {"proposed_code": "  ", "proposed_description": "\n"}, {"proposed_code": None}])
def test_drop_with_blank_proposed_fields_is_accepted(conn, fx, overrides):
    create(conn, fx, **{**DROP, **overrides})
    row = business_snapshot(conn, fx)["reselections"][0]
    assert (row["proposed_code"], row["proposed_description"]) == (None, None)


def test_validation_order_follows_the_old_script():
    # action, lalu target_type, original_code, reason, proposed_code.
    everything_wrong = {
        "action": "X", "target_type": "Y", "original_code": "", "reason": "", "proposed_code": "",
    }
    reject(everything_wrong, "action tidak valid")
    reject({**everything_wrong, "action": "CHANGE"}, "target_type tidak valid")
    reject({**everything_wrong, "action": "CHANGE", "target_type": "DIAGNOSIS"}, "original_code wajib diisi")
    reject({**everything_wrong, "action": "CHANGE", "target_type": "DIAGNOSIS", "original_code": "A"},
           "reason wajib diisi")
    reject({**everything_wrong, "action": "CHANGE", "target_type": "DIAGNOSIS", "original_code": "A",
            "reason": "R"}, "proposed_code wajib diisi")


@pytest.mark.parametrize("field", ["original_code", "proposed_code"])
def test_code_longer_than_the_column_is_rejected(conn, fx, field):
    create(conn, fx, **{field: "K" * 100})
    with pytest.raises(ValidationError, match=f"{field} maksimal 100 karakter"):
        create(conn, fx, **{field: "K" * 101})
    assert count_rows(conn, fx)[0] == 1


@pytest.mark.parametrize(
    "field",
    ["target_type", "action", "original_code", "original_description",
     "proposed_code", "proposed_description", "reason"],
)
def test_non_string_text_is_rejected(field):
    reject({field: 1}, "harus string")


@pytest.mark.parametrize(
    "name, value",
    [("review_id", "1"), ("review_id", True), ("created_by", 1.0), ("created_by", None)],
)
def test_ids_must_be_integers(name, value):
    reject({name: value}, f"{name} harus integer")


# ---------------------------------------------------------------------------
# Transaksi
# ---------------------------------------------------------------------------

def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    a = psycopg2.connect(**pg_dsn)
    b = psycopg2.connect(**pg_dsn)
    try:
        create(a, fx)
        assert count_rows(b, fx) == (0, 0)
    finally:
        a.rollback()
        a.close()
        b.close()


def test_callers_rollback_discards_reselection_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        create(conn, fx)
        assert count_rows(conn, fx) == (1, 1)
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
                raise RuntimeError("gagal setelah service")
        assert count_rows(conn, fx) == (0, 0)
    finally:
        conn.close()


def test_transaction_commits_reselection_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            create(conn, fx)
    finally:
        conn.close()
    assert committed_counts(pg_dsn, fx) == (1, 1)


def test_audit_failure_rolls_back_the_new_reselection(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx

    def failing_record_event(*args, **kwargs):
        raise AuditEventError("audit gagal")

    monkeypatch.setattr(audit_service, "record_event", failing_record_event)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                reselection_service.create_reselection(
                    conn, review_id=fx["review_id"], created_by=fx["verifier"], **CHANGE
                )
    finally:
        conn.close()
    assert committed_counts(pg_dsn, fx) == (0, 0)


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


def _close(fx):
    return lambda c: close_review(
        c, review_id=fx["review_id"], closed_by=fx["verifier"],
        final_decision="LAYAK", resolution_note="Selesai.",
    )


def _create(fx, **overrides):
    return lambda c: create(c, fx, **overrides)


def _review_state(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_reselection_waits_for_a_closing_review_and_is_then_rejected(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        _close(fx)(closer)

        thread, outcome = _run_in_thread(pg_dsn, _create(fx))
        _assert_waiting(thread, "pembuatan reselection")
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert isinstance(outcome.get("error"), InvalidStateError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM review.claim_reselections WHERE review_id = %s", (fx["review_id"],))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT array_agg(event_type ORDER BY id) FROM review.review_events WHERE review_id = %s",
                (fx["review_id"],),
            )
            assert cur.fetchone()[0] == ["REVIEW_CLOSED"]
    finally:
        conn.close()


def test_reselection_proceeds_if_the_closing_review_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        _close(fx)(closer)

        thread, outcome = _run_in_thread(pg_dsn, _create(fx))
        _assert_waiting(thread, "pembuatan reselection")
        closer.rollback()
        thread.join(timeout=10)
    finally:
        closer.close()

    assert "error" not in outcome
    assert outcome["result"].reselection.status == "PROPOSED"
    assert committed_counts(pg_dsn, fx) == (1, 1)
    assert _review_state(pg_dsn, fx) == "OPEN"


def test_closing_waits_for_a_reselection_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    creator = psycopg2.connect(**pg_dsn)
    try:
        create(creator, fx)

        thread, outcome = _run_in_thread(pg_dsn, _close(fx))
        _assert_waiting(thread, "penutupan review")
        creator.commit()
        thread.join(timeout=10)
    finally:
        creator.close()

    assert "error" not in outcome
    assert outcome["result"].review.status == "CLOSED"
    conn = psycopg2.connect(**pg_dsn)
    try:
        # PROPOSED yang sudah ter-commit tetap apa adanya setelah close.
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM review.claim_reselections WHERE review_id = %s", (fx["review_id"],))
            assert cur.fetchall() == [("PROPOSED",)]
    finally:
        conn.close()


def _open_cycle(fx):
    return lambda c: open_review_cycle(c, nosjp=fx["nosjp"], opened_by=fx["verifier"])


def test_open_cycle_waits_for_a_reselection_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    creator = psycopg2.connect(**pg_dsn)
    try:
        create(creator, fx)

        thread, outcome = _run_in_thread(pg_dsn, _open_cycle(fx))
        _assert_waiting(thread, "pembukaan cycle")
        creator.commit()
        thread.join(timeout=10)
    finally:
        creator.close()

    assert "error" not in outcome
    # Review OPEN yang sama dikembalikan; tidak ada cycle baru.
    assert outcome["result"].created is False
    assert outcome["result"].review.id == fx["review_id"]
    assert committed_counts(pg_dsn, fx) == (1, 1)


def test_reselection_waits_for_open_cycle_and_proceeds_on_the_same_review(pg_dsn, committed_fx):
    fx = committed_fx
    opener = psycopg2.connect(**pg_dsn)
    try:
        result = _open_cycle(fx)(opener)
        assert result.created is False

        thread, outcome = _run_in_thread(pg_dsn, _create(fx))
        _assert_waiting(thread, "pembuatan reselection")
        opener.commit()
        thread.join(timeout=10)
    finally:
        opener.close()

    assert "error" not in outcome
    assert committed_counts(pg_dsn, fx) == (1, 1)


def test_open_cycle_after_close_leaves_the_reselection_on_its_own_cycle(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            create(conn, fx)
            _close(fx)(conn)
        with transaction(conn):
            opened = _open_cycle(fx)(conn)
        assert opened.created is True and opened.review.cycle_no == 2
        with pytest.raises(InvalidStateError):
            create(conn, fx)  # review lama sudah CLOSED
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT review_id, status FROM review.claim_reselections WHERE claim_id = %s",
                        (fx["claim_id"],))
            assert cur.fetchall() == [(fx["review_id"], "PROPOSED")]
    finally:
        conn.close()


def test_concurrent_reselections_and_findings_on_the_same_review_do_not_block(pg_dsn, committed_fx):
    fx = committed_fx
    first = psycopg2.connect(**pg_dsn)
    try:
        create(first, fx)

        thread, outcome = _run_in_thread(pg_dsn, _create(fx, **DROP))
        thread.join(timeout=10)
        assert not thread.is_alive(), "reselection kedua tidak boleh menunggu reselection pertama"
        assert "error" not in outcome

        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: create_review_finding(
                c, review_id=fx["review_id"], created_by=fx["verifier"],
                finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
            ),
        )
        thread.join(timeout=10)
        assert not thread.is_alive(), "finding tidak boleh menunggu reselection"
        assert "error" not in outcome
        first.commit()
    finally:
        first.close()

    assert committed_counts(pg_dsn, fx) == (2, 3)


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


def test_lock_mode_is_share_on_the_review_and_leaves_the_claim_alone(pg_dsn, committed_fx):
    fx = committed_fx
    creator = psycopg2.connect(**pg_dsn)
    try:
        create(creator, fx)
        review_sql = "SELECT id FROM review.claim_reviews WHERE id = %s FOR {} NOWAIT"
        claim_sql = "SELECT id FROM core.claims WHERE id = %s FOR {} NOWAIT"
        review = (fx["review_id"],)
        claim = (fx["claim_id"],)

        assert not _lock_attempt(pg_dsn, review_sql.format("NO KEY UPDATE"), review)
        assert not _lock_attempt(pg_dsn, review_sql.format("UPDATE"), review)
        assert _lock_attempt(pg_dsn, review_sql.format("SHARE"), review)
        assert _lock_attempt(pg_dsn, review_sql.format("KEY SHARE"), review)
        # Baris klaim hanya dipegang FOR KEY SHARE oleh FK check INSERT; tidak
        # ada FOR SHARE atau lebih kuat (FOR NO KEY UPDATE tetap berhasil).
        assert _lock_attempt(pg_dsn, claim_sql.format("KEY SHARE"), claim)
        assert _lock_attempt(pg_dsn, claim_sql.format("SHARE"), claim)
        assert _lock_attempt(pg_dsn, claim_sql.format("NO KEY UPDATE"), claim)
    finally:
        creator.rollback()
        creator.close()
