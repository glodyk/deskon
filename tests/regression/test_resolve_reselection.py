"""Regression test resolve_reselection terhadap behavior resolve_reselection_v1.py.

Behavior lama yang dipertahankan (script v1):
- decision di-strip + upper, hanya AGREED atau REJECTED;
- user harus ada dan aktif, dicek sebelum reselection;
- reselection harus ada dan PROPOSED (status diperiksa sebelum review);
- review induk harus OPEN;
- AGREED mengisi agreed_by/agreed_at; REJECTED menyimpan keduanya NULL;
- satu event RESELECTION_RESOLVED dengan kunci payload yang sama dengan script
  lama, dalam satu transaksi.

Perubahan yang disepakati: event_data lewat audit_service, lock review FOR
SHARE OF cr lalu reselection FOR NO KEY UPDATE OF r (script lama memakai
FOR UPDATE tanpa OF yang ikut mengunci review dan klaim), tanpa print, tanpa
commit, ConflictError bila guard status gagal.
"""

import psycopg2
import pytest

from deskon.db import transaction
from deskon.errors import (
    AuditEventError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service, reselection_service
from deskon.services.finding_service import create_review_finding
from deskon.services.reselection_service import (
    create_reselection,
    resolve_reselection,
)
from deskon.services.review_service import close_review, open_review_cycle
from tests.regression.test_create_reselection import (
    CHANGE,
    DROP,
    _assert_waiting,
    _lock_attempt,
    _NoDatabase,
    _run_in_thread,
    cleanup,
    seed,
)

OLD_PAYLOAD_KEYS = {
    "reselection_id", "claim_id", "review_id", "nosjp", "target_type", "action",
    "original_code", "proposed_code", "previous_status", "new_status",
}


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

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


def propose(conn, fx, **overrides):
    base = DROP if str(overrides.get("action", "CHANGE")).upper() == "DROP" else CHANGE
    return create_reselection(
        conn, review_id=fx["review_id"], created_by=fx["verifier"], **{**base, **overrides}
    ).reselection


def set_status(conn, reselection_id, status):
    """Paksa status lewat SQL (jalur DRAFT/CORRECTED/CANCELLED belum punya use case)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE review.claim_reselections SET status = %s WHERE id = %s", (status, reselection_id))


def resolve(conn, fx, reselection_id, decision="AGREED", resolved_by=None):
    return resolve_reselection(
        conn,
        reselection_id=reselection_id,
        resolved_by=fx["verifier"] if resolved_by is None else resolved_by,
        decision=decision,
    )


def row(conn, reselection_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, agreed_by, agreed_at, updated_at, claim_id, review_id,
                   target_type, action, original_code, original_description,
                   proposed_code, proposed_description, reason, created_by,
                   corrects_reselection_id
            FROM review.claim_reselections WHERE id = %s
            """,
            (reselection_id,),
        )
        return cur.fetchone()


def events(conn, fx, event_type=None):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, entity_type, entity_id, user_id, event_data "
            "FROM review.review_events WHERE review_id = %s ORDER BY id",
            (fx["review_id"],),
        )
        rows = cur.fetchall()
    return [r for r in rows if event_type is None or r[0] == event_type]


def event_count(conn, fx):
    return len(events(conn, fx))


def close(conn, fx):
    return close_review(
        conn, review_id=fx["review_id"], closed_by=fx["verifier"],
        final_decision="LAYAK", resolution_note="Selesai.",
    )


# ---------------------------------------------------------------------------
# Sukses
# ---------------------------------------------------------------------------

def test_proposed_is_agreed(conn, fx):
    proposed = propose(conn, fx)
    before = row(conn, proposed.id)

    result = resolve(conn, fx, proposed.id, "AGREED")

    after = row(conn, proposed.id)
    assert after[0] == "AGREED"
    assert after[1] == fx["verifier"]
    assert after[2] is not None
    assert after[3] >= before[3]
    # kolom lain tidak berubah
    assert after[4:] == before[4:]
    assert after[14] is None  # corrects_reselection_id
    assert result.reselection.status == "AGREED"
    assert result.reselection.agreed_by == fx["verifier"]
    assert result.reselection.agreed_at == after[2]
    assert result.reselection.nosjp == fx["nosjp"]
    assert [e[0] for e in events(conn, fx)] == ["RESELECTION_CREATED", "RESELECTION_RESOLVED"]


def test_proposed_is_rejected_with_null_agreed_fields(conn, fx):
    proposed = propose(conn, fx)
    before = row(conn, proposed.id)

    result = resolve(conn, fx, proposed.id, "REJECTED")

    after = row(conn, proposed.id)
    assert after[0] == "REJECTED"
    assert after[1] is None and after[2] is None
    assert after[3] >= before[3]
    assert after[4:] == before[4:]
    assert result.reselection.agreed_by is None and result.reselection.agreed_at is None
    assert [e[0] for e in events(conn, fx)] == ["RESELECTION_CREATED", "RESELECTION_RESOLVED"]


@pytest.mark.parametrize("decision", ["AGREED", "REJECTED"])
@pytest.mark.parametrize("fields", [CHANGE, DROP, {**CHANGE, "target_type": "PROCEDURE"}, {**DROP, "target_type": "DIAGNOSIS"}])
def test_every_action_and_target_type_can_be_resolved(conn, fx, fields, decision):
    proposed = propose(conn, fx, **fields)
    result = resolve(conn, fx, proposed.id, decision)
    assert result.reselection.status == decision
    assert result.reselection.proposed_code == proposed.proposed_code
    data = events(conn, fx, "RESELECTION_RESOLVED")[0][4]
    assert data["proposed_code"] == proposed.proposed_code
    assert data["target_type"] == fields["target_type"]
    assert data["action"] == fields["action"]


@pytest.mark.parametrize("raw, canonical", [(" agreed ", "AGREED"), ("rejected", "REJECTED"), ("\tAgreed\n", "AGREED")])
def test_decision_is_stripped_and_uppercased(conn, fx, raw, canonical):
    proposed = propose(conn, fx)
    assert resolve(conn, fx, proposed.id, raw).reselection.status == canonical


def test_resolving_one_reselection_leaves_the_others_alone(conn, fx):
    first = propose(conn, fx)
    second = propose(conn, fx, **DROP)
    before = row(conn, second.id)

    resolve(conn, fx, first.id, "AGREED")

    assert row(conn, second.id) == before
    assert len(events(conn, fx, "RESELECTION_RESOLVED")) == 1


def test_creator_may_resolve_and_another_user_too(conn, fx):
    other_user = None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core.users (full_name, email, password_hash, role, is_active) "
            "VALUES ('lain', %s, 'x', 'BPJS_VERIFIER', true) RETURNING id",
            (f"lain-{fx['verifier']}@test.local",),
        )
        other_user = cur.fetchone()[0]
    first = propose(conn, fx)
    second = propose(conn, fx)
    resolve(conn, fx, first.id, "AGREED", resolved_by=fx["verifier"])
    resolve(conn, fx, second.id, "AGREED", resolved_by=other_user)
    assert row(conn, second.id)[1] == other_user


# ---------------------------------------------------------------------------
# Validasi
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["APPROVED", "", "   ", "PROPOSED", "CANCELLED", "DRAFT"])
def test_invalid_decision_is_rejected_before_the_database_is_read(decision):
    with pytest.raises(ValidationError, match="decision tidak valid"):
        resolve_reselection(_NoDatabase(), reselection_id=1, resolved_by=1, decision=decision)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reselection_id": "1"}, {"reselection_id": True}, {"reselection_id": 1.0}, {"reselection_id": None},
        {"resolved_by": "1"}, {"resolved_by": False}, {"resolved_by": None},
        {"decision": None}, {"decision": 1},
    ],
)
def test_wrong_types_are_rejected_before_the_database_is_read(kwargs):
    args = {"reselection_id": 1, "resolved_by": 1, "decision": "AGREED", **kwargs}
    with pytest.raises(ValidationError):
        resolve_reselection(_NoDatabase(), **args)


def test_unknown_reselection_is_rejected(conn, fx):
    with pytest.raises(NotFoundError, match="Reselection"):
        resolve(conn, fx, 999999999)
    assert event_count(conn, fx) == 0


def test_unknown_user_is_rejected(conn, fx):
    proposed = propose(conn, fx)
    before = (row(conn, proposed.id), event_count(conn, fx))
    with pytest.raises(NotFoundError, match="User"):
        resolve(conn, fx, proposed.id, resolved_by=999999999)
    assert (row(conn, proposed.id), event_count(conn, fx)) == before


def test_inactive_user_is_rejected(conn, fx):
    proposed = propose(conn, fx)
    before = (row(conn, proposed.id), event_count(conn, fx))
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        resolve(conn, fx, proposed.id, resolved_by=fx["inactive"])
    assert (row(conn, proposed.id), event_count(conn, fx)) == before


def test_validation_order_decision_then_user_then_reselection(conn, fx):
    with pytest.raises(ValidationError, match="decision"):
        resolve_reselection(conn, reselection_id=999999999, resolved_by=999999999, decision="X")
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        resolve(conn, fx, 999999999, resolved_by=fx["inactive"])
    with pytest.raises(NotFoundError, match="User"):
        resolve(conn, fx, 999999999, resolved_by=999999999)


def test_claim_and_review_are_not_parameters(conn, fx):
    proposed = propose(conn, fx)
    with pytest.raises(TypeError):
        resolve_reselection(conn, reselection_id=proposed.id, resolved_by=fx["verifier"], decision="AGREED", review_id=1)
    with pytest.raises(TypeError):
        resolve_reselection(conn, reselection_id=proposed.id, resolved_by=fx["verifier"], decision="AGREED", claim_id=1)


# ---------------------------------------------------------------------------
# Status reselection dan review
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["DRAFT", "CORRECTED", "CANCELLED"])
def test_non_proposed_source_status_is_rejected(conn, fx, status):
    proposed = propose(conn, fx)
    set_status(conn, proposed.id, status)
    before = (row(conn, proposed.id), event_count(conn, fx))
    with pytest.raises(InvalidStateError, match=status):
        resolve(conn, fx, proposed.id)
    assert (row(conn, proposed.id), event_count(conn, fx)) == before


@pytest.mark.parametrize("first, second", [("AGREED", "AGREED"), ("AGREED", "REJECTED"), ("REJECTED", "AGREED"), ("REJECTED", "REJECTED")])
def test_resolving_twice_is_rejected_and_keeps_the_first_result(conn, fx, first, second):
    proposed = propose(conn, fx)
    resolve(conn, fx, proposed.id, first)
    snapshot = (row(conn, proposed.id), events(conn, fx))
    with pytest.raises(InvalidStateError, match=first):
        resolve(conn, fx, proposed.id, second)
    assert (row(conn, proposed.id), events(conn, fx)) == snapshot


def test_closed_review_rejects_resolving_a_proposed_reselection(conn, fx):
    proposed = propose(conn, fx)
    close(conn, fx)
    before = (row(conn, proposed.id), event_count(conn, fx))
    with pytest.raises(InvalidStateError, match="tidak OPEN"):
        resolve(conn, fx, proposed.id)
    assert (row(conn, proposed.id), event_count(conn, fx)) == before
    assert row(conn, proposed.id)[0] == "PROPOSED"


def test_status_error_takes_precedence_over_review_error(conn, fx):
    proposed = propose(conn, fx)
    resolve(conn, fx, proposed.id, "AGREED")
    close(conn, fx)
    with pytest.raises(InvalidStateError, match="Hanya status PROPOSED"):
        resolve(conn, fx, proposed.id)


def test_review_finding_claim_and_other_reselections_are_untouched(conn, fx):
    proposed = propose(conn, fx)
    other = propose(conn, fx, **DROP)
    create_review_finding(
        conn, review_id=fx["review_id"], created_by=fx["verifier"],
        finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
    )

    def snapshot():
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
            review = cur.fetchone()
            cur.execute("SELECT * FROM review.review_findings WHERE review_id = %s ORDER BY id", (fx["review_id"],))
            findings = cur.fetchall()
            cur.execute("SELECT * FROM core.claims WHERE id = %s", (fx["claim_id"],))
            claim = cur.fetchone()
        return review, findings, claim, row(conn, other.id)

    before = snapshot()
    resolve(conn, fx, proposed.id)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["AGREED", "REJECTED"])
def test_event_identity_and_payload_match_the_old_script(conn, fx, decision):
    proposed = propose(conn, fx)
    result = resolve(conn, fx, proposed.id, decision)

    resolved = events(conn, fx, "RESELECTION_RESOLVED")
    assert len(resolved) == 1
    event_type, entity_type, entity_id, user_id, data = resolved[0]
    assert (event_type, entity_type, entity_id, user_id) == (
        "RESELECTION_RESOLVED", "claim_reselection", proposed.id, fx["verifier"],
    )
    assert set(data) == OLD_PAYLOAD_KEYS
    assert data == {
        "reselection_id": proposed.id,
        "claim_id": fx["claim_id"],
        "review_id": fx["review_id"],
        "nosjp": fx["nosjp"],
        "target_type": "DIAGNOSIS",
        "action": "CHANGE",
        "original_code": "G50.0",
        "proposed_code": "G50.1",
        "previous_status": "PROPOSED",
        "new_status": decision,
    }
    assert result.event.event_type == "RESELECTION_RESOLVED"
    assert result.event.entity_id == proposed.id


def test_drop_event_has_null_proposed_code(conn, fx):
    proposed = propose(conn, fx, **DROP)
    resolve(conn, fx, proposed.id)
    assert events(conn, fx, "RESELECTION_RESOLVED")[0][4]["proposed_code"] is None


def test_event_has_no_description_reason_or_agreed_fields(conn, fx):
    proposed = propose(conn, fx)
    resolve(conn, fx, proposed.id)
    data = events(conn, fx, "RESELECTION_RESOLVED")[0][4]
    for key in ("reason", "original_description", "proposed_description", "agreed_by", "agreed_at", "resolved_by"):
        assert key not in data


def test_event_order_keeps_the_created_event_first(conn, fx):
    first = propose(conn, fx)
    second = propose(conn, fx)
    resolve(conn, fx, second.id, "REJECTED")
    resolve(conn, fx, first.id, "AGREED")
    assert [(e[0], e[2]) for e in events(conn, fx)] == [
        ("RESELECTION_CREATED", first.id),
        ("RESELECTION_CREATED", second.id),
        ("RESELECTION_RESOLVED", second.id),
        ("RESELECTION_RESOLVED", first.id),
    ]


def test_update_guard_raises_conflict_when_the_row_changed_underneath(conn, fx, monkeypatch):
    """Guard UPDATE ... AND status='PROPOSED' tidak terjangkau lewat kunci biasa;
    paksa dengan mengubah status tepat setelah baris dikunci."""
    proposed = propose(conn, fx)

    real = reselection_service.audit_service.record_event
    calls = []
    monkeypatch.setattr(
        reselection_service.audit_service, "record_event",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )

    class Spy:
        def __init__(self, inner):
            self.inner = inner
            self.armed = False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def cursor(self):
            cur = self.inner.cursor()
            outer = self

            class Cur:
                def __enter__(self_inner):
                    cur.__enter__()
                    return self_inner

                def __exit__(self_inner, *a):
                    return cur.__exit__(*a)

                def execute(self_inner, sql, params=None):
                    if sql.lstrip().upper().startswith("UPDATE REVIEW.CLAIM_RESELECTIONS"):
                        cur.execute(
                            "UPDATE review.claim_reselections SET status = 'CANCELLED' WHERE id = %s",
                            (proposed.id,),
                        )
                    return cur.execute(sql, params)

                def __getattr__(self_inner, name):
                    return getattr(cur, name)

            return Cur()

    with pytest.raises(ConflictError):
        resolve_reselection(Spy(conn), reselection_id=proposed.id, resolved_by=fx["verifier"], decision="AGREED")
    assert calls == []
    assert row(conn, proposed.id)[0] == "CANCELLED"


# ---------------------------------------------------------------------------
# Transaksi
# ---------------------------------------------------------------------------

def _committed_proposed(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            return propose(conn, fx).id
    finally:
        conn.close()


def _committed_state(pg_dsn, reselection_id, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        return row(conn, reselection_id)[0], len(events(conn, fx, "RESELECTION_RESOLVED"))
    finally:
        conn.close()


def test_service_does_not_commit(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    a = psycopg2.connect(**pg_dsn)
    try:
        resolve(a, fx, rid)
        assert _committed_state(pg_dsn, rid, fx) == ("PROPOSED", 0)
    finally:
        a.rollback()
        a.close()


def test_callers_rollback_discards_update_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    conn = psycopg2.connect(**pg_dsn)
    try:
        resolve(conn, fx, rid)
        assert row(conn, rid)[0] == "AGREED"
        conn.rollback()
        assert row(conn, rid)[0] == "PROPOSED"
        assert len(events(conn, fx, "RESELECTION_RESOLVED")) == 0
    finally:
        conn.close()


def test_transaction_commits_update_and_event_together(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with transaction(conn):
            resolve(conn, fx, rid, "REJECTED")
    finally:
        conn.close()
    assert _committed_state(pg_dsn, rid, fx) == ("REJECTED", 1)


def test_exception_inside_transaction_block_rolls_back_both(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(RuntimeError):
            with transaction(conn):
                resolve(conn, fx, rid)
                raise RuntimeError("gagal setelah service")
    finally:
        conn.close()
    assert _committed_state(pg_dsn, rid, fx) == ("PROPOSED", 0)


def test_audit_failure_rolls_back_the_update(pg_dsn, committed_fx, monkeypatch):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)

    def failing_record_event(*args, **kwargs):
        raise AuditEventError("audit gagal")

    monkeypatch.setattr(audit_service, "record_event", failing_record_event)
    conn = psycopg2.connect(**pg_dsn)
    try:
        with pytest.raises(AuditEventError):
            with transaction(conn):
                resolve(conn, fx, rid)
    finally:
        conn.close()
    assert _committed_state(pg_dsn, rid, fx) == ("PROPOSED", 0)


# ---------------------------------------------------------------------------
# Konkurensi
# ---------------------------------------------------------------------------

def _resolve_job(fx, rid, decision="AGREED"):
    return lambda c: resolve(c, fx, rid, decision)


def _close_job(fx):
    return lambda c: close(c, fx)


def _final(pg_dsn, fx, rid):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM review.claim_reviews WHERE id = %s", (fx["review_id"],))
            review_status = cur.fetchone()[0]
        return review_status, row(conn, rid)[0], len(events(conn, fx, "RESELECTION_RESOLVED"))
    finally:
        conn.close()


def test_resolve_waits_for_a_closing_review_and_is_then_rejected(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, rid))
        _assert_waiting(thread, "resolve")
        closer.commit()
        thread.join(timeout=10)
    finally:
        closer.close()
    assert isinstance(outcome.get("error"), InvalidStateError)
    assert _final(pg_dsn, fx, rid) == ("CLOSED", "PROPOSED", 0)


def test_resolve_proceeds_if_the_closing_review_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    closer = psycopg2.connect(**pg_dsn)
    try:
        close(closer, fx)
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, rid))
        _assert_waiting(thread, "resolve")
        closer.rollback()
        thread.join(timeout=10)
    finally:
        closer.close()
    assert "error" not in outcome
    assert _final(pg_dsn, fx, rid) == ("OPEN", "AGREED", 1)


def test_closing_waits_for_a_resolve_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    resolver = psycopg2.connect(**pg_dsn)
    try:
        resolve(resolver, fx, rid, "REJECTED")
        thread, outcome = _run_in_thread(pg_dsn, _close_job(fx))
        _assert_waiting(thread, "close_review")
        resolver.commit()
        thread.join(timeout=10)
    finally:
        resolver.close()
    assert "error" not in outcome
    assert _final(pg_dsn, fx, rid) == ("CLOSED", "REJECTED", 1)


def test_two_resolves_of_the_same_reselection_run_in_turn(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    first = psycopg2.connect(**pg_dsn)
    try:
        resolve(first, fx, rid, "AGREED")
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, rid, "REJECTED"))
        _assert_waiting(thread, "resolve kedua")
        first.commit()
        thread.join(timeout=10)
    finally:
        first.close()
    assert isinstance(outcome.get("error"), InvalidStateError)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert row(conn, rid)[0] == "AGREED"
        assert row(conn, rid)[1] == fx["verifier"]
        assert len(events(conn, fx, "RESELECTION_RESOLVED")) == 1
    finally:
        conn.close()


def test_second_resolve_succeeds_if_the_first_rolls_back(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    first = psycopg2.connect(**pg_dsn)
    try:
        resolve(first, fx, rid, "AGREED")
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, rid, "REJECTED"))
        _assert_waiting(thread, "resolve kedua")
        first.rollback()
        thread.join(timeout=10)
    finally:
        first.close()
    assert "error" not in outcome
    assert _final(pg_dsn, fx, rid) == ("OPEN", "REJECTED", 1)


def test_resolves_of_different_reselections_do_not_block(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            first_id = propose(conn, fx).id
            second_id = propose(conn, fx, **DROP).id
    finally:
        conn.close()
    holder = psycopg2.connect(**pg_dsn)
    try:
        resolve(holder, fx, first_id)
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, second_id, "REJECTED"))
        thread.join(timeout=10)
        assert not thread.is_alive(), "resolve reselection lain tidak boleh menunggu"
        assert "error" not in outcome
        holder.commit()
    finally:
        holder.close()
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert (row(conn, first_id)[0], row(conn, second_id)[0]) == ("AGREED", "REJECTED")
        assert len(events(conn, fx, "RESELECTION_RESOLVED")) == 2
    finally:
        conn.close()


def _open_job(fx):
    return lambda c: open_review_cycle(c, nosjp=fx["nosjp"], opened_by=fx["verifier"])


def test_open_cycle_waits_for_a_resolve_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    resolver = psycopg2.connect(**pg_dsn)
    try:
        resolve(resolver, fx, rid)
        thread, outcome = _run_in_thread(pg_dsn, _open_job(fx))
        _assert_waiting(thread, "open_review_cycle")
        resolver.commit()
        thread.join(timeout=10)
    finally:
        resolver.close()
    assert "error" not in outcome
    assert outcome["result"].created is False
    assert outcome["result"].review.id == fx["review_id"]
    assert _final(pg_dsn, fx, rid) == ("OPEN", "AGREED", 1)


def test_resolve_waits_for_open_cycle_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    opener = psycopg2.connect(**pg_dsn)
    try:
        result = _open_job(fx)(opener)
        assert result.created is False
        thread, outcome = _run_in_thread(pg_dsn, _resolve_job(fx, rid))
        _assert_waiting(thread, "resolve")
        opener.commit()
        thread.join(timeout=10)
    finally:
        opener.close()
    assert "error" not in outcome
    assert _final(pg_dsn, fx, rid) == ("OPEN", "AGREED", 1)


def test_resolve_does_not_block_create_reselection_or_finding(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    resolver = psycopg2.connect(**pg_dsn)
    try:
        resolve(resolver, fx, rid)

        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: create_reselection(c, review_id=fx["review_id"], created_by=fx["verifier"], **DROP),
        )
        thread.join(timeout=10)
        assert not thread.is_alive(), "create_reselection tidak boleh menunggu resolve"
        assert "error" not in outcome
        assert outcome["result"].reselection.status == "PROPOSED"

        thread, outcome = _run_in_thread(
            pg_dsn,
            lambda c: create_review_finding(
                c, review_id=fx["review_id"], created_by=fx["verifier"],
                finding_category="KODING", finding_title="Judul", finding_description="Deskripsi",
            ),
        )
        thread.join(timeout=10)
        assert not thread.is_alive(), "finding tidak boleh menunggu resolve"
        assert "error" not in outcome
        resolver.commit()
    finally:
        resolver.close()


def test_lock_modes_while_resolving(pg_dsn, committed_fx):
    fx = committed_fx
    rid = _committed_proposed(pg_dsn, fx)
    resolver = psycopg2.connect(**pg_dsn)
    try:
        resolve(resolver, fx, rid)
        review_sql = "SELECT id FROM review.claim_reviews WHERE id = %s FOR {} NOWAIT"
        resel_sql = "SELECT id FROM review.claim_reselections WHERE id = %s FOR {} NOWAIT"
        claim_sql = "SELECT id FROM core.claims WHERE id = %s FOR {} NOWAIT"
        review, resel, claim = (fx["review_id"],), (rid,), (fx["claim_id"],)

        # review: FOR SHARE
        assert not _lock_attempt(pg_dsn, review_sql.format("NO KEY UPDATE"), review)
        assert not _lock_attempt(pg_dsn, review_sql.format("UPDATE"), review)
        assert _lock_attempt(pg_dsn, review_sql.format("SHARE"), review)
        assert _lock_attempt(pg_dsn, review_sql.format("KEY SHARE"), review)
        # reselection: FOR NO KEY UPDATE (UPDATE tidak mengubah kunci)
        assert not _lock_attempt(pg_dsn, resel_sql.format("NO KEY UPDATE"), resel)
        assert not _lock_attempt(pg_dsn, resel_sql.format("SHARE"), resel)
        assert _lock_attempt(pg_dsn, resel_sql.format("KEY SHARE"), resel)
        # klaim: hanya KEY SHARE dari FK; tidak dikunci service
        assert _lock_attempt(pg_dsn, claim_sql.format("KEY SHARE"), claim)
        assert _lock_attempt(pg_dsn, claim_sql.format("SHARE"), claim)
        assert _lock_attempt(pg_dsn, claim_sql.format("NO KEY UPDATE"), claim)
    finally:
        resolver.rollback()
        resolver.close()


def test_resolve_close_open_and_create_together_do_not_deadlock(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn:
            ids = [propose(conn, fx).id for _ in range(3)]
    finally:
        conn.close()

    jobs = [
        _resolve_job(fx, ids[0]),
        _resolve_job(fx, ids[1], "REJECTED"),
        _resolve_job(fx, ids[2]),
        _close_job(fx),
        _open_job(fx),
        lambda c: create_reselection(c, review_id=fx["review_id"], created_by=fx["verifier"], **DROP),
    ]
    runs = [_run_in_thread(pg_dsn, job) for job in jobs]
    for thread, _ in runs:
        thread.join(timeout=20)
        assert not thread.is_alive(), "kemungkinan deadlock atau menunggu tanpa akhir"
    for _, outcome in runs:
        error = outcome.get("error")
        # Penolakan bisnis (review sudah CLOSED) sah; deadlock/timeout tidak.
        assert error is None or isinstance(error, (InvalidStateError, NotFoundError)), error
