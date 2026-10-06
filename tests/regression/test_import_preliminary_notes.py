"""Regression test import_preliminary_notes (preliminary_note_service).

Pengganti import_preliminary_notes_v1.py sesuai kontrak terkunci:
- notes = list/tuple pasangan (nosjp, note), di-strip, tidak boleh kosong;
  batch kosong valid;
- urutan: bentuk argumen -> created_by -> user ada dan aktif -> seluruh baris
  -> resolusi seluruh Nosjp -> INSERT (tidak ada INSERT sebelum semuanya valid);
- duplikat valid, tanpa syarat status klaim/review, tanpa lock, tanpa event;
- service tidak commit/rollback/print dan hanya menulis review.preliminary_notes.
"""

import inspect
import uuid

import psycopg2
import pytest

from deskon.errors import InvalidStateError, NotFoundError, ValidationError
from deskon.services import preliminary_note_service
from deskon.services.preliminary_note_service import (
    ImportPreliminaryNotesResult,
    PreliminaryNote,
    import_preliminary_notes,
)
from deskon.services.review_service import close_review, open_review_cycle
from tests.regression.test_create_reselection import (
    _NoDatabase,
    _run_in_thread,
)

PENDING = "PENDING"
VPK = "VERIFIKASI_PASCA_KLAIM"
AUDIT = "AUDIT_ADMINISTRASI_KLAIM"
MISSING = 999999999
OTHER_TABLES = (
    "core.claims",
    "review.claim_reviews",
    "review.review_findings",
    "review.claim_reselections",
    "review.review_events",
)


# ---------------------------------------------------------------------------
# Data uji
# ---------------------------------------------------------------------------

def seed(conn):
    """Tiga klaim: A review OPEN (PENDING), B review CLOSED (VPK), C tanpa review (AUDIT)."""
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
        claims = {}
        for key, status in (("A", PENDING), ("B", VPK), ("C", AUDIT)):
            nosjp = f"NOTE{key}{tag}"
            cur.execute(
                "INSERT INTO core.claims (nosjp, hospital_id, claim_status, kdppklayan, nmtkp, service_month) "
                "VALUES (%s, %s, %s, %s, 'RITL', '2025-10-01') RETURNING id",
                (nosjp, hospital_id, status, f"RS{tag}"),
            )
            claims[key] = (cur.fetchone()[0], nosjp)
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type) "
            "VALUES (%s, %s, 1, %s) RETURNING id",
            (claims["A"][0], users["active"], PENDING),
        )
        open_review = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO review.claim_reviews (claim_id, opened_by, cycle_no, review_type, status, "
            "final_decision, closed_by, closed_at) "
            "VALUES (%s, %s, 1, %s, 'CLOSED', 'LAYAK', %s, CURRENT_TIMESTAMP) RETURNING id",
            (claims["B"][0], users["active"], VPK, users["active"]),
        )
        closed_review = cur.fetchone()[0]
    return {
        "hospital_id": hospital_id,
        "active": users["active"],
        "inactive": users["inactive"],
        "claim": {k: v[0] for k, v in claims.items()},
        "nosjp": {k: v[1] for k, v in claims.items()},
        "open_review": open_review,
        "closed_review": closed_review,
    }


def cleanup(pg_dsn, fx):
    conn = psycopg2.connect(**pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            claim_ids = list(fx["claim"].values())
            cur.execute(
                "DELETE FROM review.review_events WHERE review_id IN "
                "(SELECT id FROM review.claim_reviews WHERE claim_id = ANY(%s))",
                (claim_ids,),
            )
            cur.execute("DELETE FROM review.preliminary_notes WHERE claim_id = ANY(%s)", (claim_ids,))
            cur.execute("DELETE FROM review.claim_reviews WHERE claim_id = ANY(%s)", (claim_ids,))
            cur.execute("DELETE FROM core.claims WHERE id = ANY(%s)", (claim_ids,))
            cur.execute("DELETE FROM core.users WHERE id = ANY(%s)", ([fx["active"], fx["inactive"]],))
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
    try:
        yield data
    finally:
        cleanup(pg_dsn, data)


def run(conn, fx, rows, created_by=None):
    return import_preliminary_notes(
        conn, created_by=fx["active"] if created_by is None else created_by, notes=rows
    )


def stored(conn, claim_ids):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, claim_id, note, created_by, created_at, updated_at "
            "FROM review.preliminary_notes WHERE claim_id = ANY(%s) ORDER BY id",
            (list(claim_ids),),
        )
        return cur.fetchall()


def counts(conn):
    out = {}
    with conn.cursor() as cur:
        for table in OTHER_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def snapshot_other(conn, fx):
    with conn.cursor() as cur:
        ids = list(fx["claim"].values())
        cur.execute("SELECT id, claim_status, updated_at FROM core.claims WHERE id = ANY(%s) ORDER BY id", (ids,))
        claims = cur.fetchall()
        cur.execute("SELECT * FROM review.claim_reviews WHERE claim_id = ANY(%s) ORDER BY id", (ids,))
        reviews = cur.fetchall()
    return counts(conn), claims, reviews


class Recorder:
    """Pembungkus koneksi: mencatat SQL dan menolak commit/rollback."""

    def __init__(self, conn):
        self._conn = conn
        self.sql = []

    def cursor(self):
        return _RecCursor(self._conn.cursor(), self.sql)

    def commit(self):
        raise AssertionError("service tidak boleh commit")

    def rollback(self):
        raise AssertionError("service tidak boleh rollback")

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RecCursor:
    def __init__(self, cur, log):
        self._cur = cur
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(" ".join(sql.split()))
        return self._cur.execute(sql, params)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def inserts(rec):
    return [s for s in rec.sql if s.startswith("INSERT")]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_single_row(conn, fx):
    result = run(conn, fx, [(fx["nosjp"]["A"], "Catatan awal")])
    assert isinstance(result, ImportPreliminaryNotesResult)
    assert result.created_by == fx["active"]
    assert result.rows_total == result.created == len(result.notes) == 1
    item = result.notes[0]
    assert isinstance(item, PreliminaryNote)
    assert item.claim_id == fx["claim"]["A"] and item.nosjp == fx["nosjp"]["A"]
    rows = stored(conn, fx["claim"].values())
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (item.note_id, fx["claim"]["A"], "Catatan awal", fx["active"])
    ]


def test_multiple_rows_keep_input_order(conn, fx):
    rows_in = [(fx["nosjp"]["C"], "c1"), (fx["nosjp"]["A"], "a1"), (fx["nosjp"]["B"], "b1")]
    result = run(conn, fx, rows_in)
    assert result.rows_total == result.created == len(result.notes) == 3
    assert [n.nosjp for n in result.notes] == [fx["nosjp"][k] for k in "CAB"]
    assert [n.claim_id for n in result.notes] == [fx["claim"][k] for k in "CAB"]
    ids = [n.note_id for n in result.notes]
    assert ids == sorted(ids) and len(set(ids)) == 3
    by_id = {r[0]: r for r in stored(conn, fx["claim"].values())}
    assert [by_id[i][2] for i in ids] == ["c1", "a1", "b1"]


def test_timestamps_are_populated_by_database_defaults(conn, fx):
    run(conn, fx, [(fx["nosjp"]["A"], "x"), (fx["nosjp"]["B"], "y")])
    rows = stored(conn, fx["claim"].values())
    assert all(r[4] is not None and r[5] is not None and r[4] == r[5] for r in rows)
    assert len({r[4] for r in rows}) == 1  # satu transaksi, satu CURRENT_TIMESTAMP


def test_tuple_input_and_list_pairs_are_accepted(conn, fx):
    result = run(conn, fx, ([fx["nosjp"]["A"], "a"], (fx["nosjp"]["B"], "b")))
    assert result.created == 2


def test_result_is_frozen(conn, fx):
    result = run(conn, fx, [(fx["nosjp"]["A"], "a")])
    with pytest.raises(Exception):
        result.created = 5
    with pytest.raises(Exception):
        result.notes[0].note_id = 5
    assert isinstance(result.notes, tuple)


def test_empty_batch_succeeds_without_insert(conn, fx):
    for empty in ([], ()):
        rec = Recorder(conn)
        result = import_preliminary_notes(rec, created_by=fx["active"], notes=empty)
        assert (result.rows_total, result.created, result.notes) == (0, 0, ())
        assert result.created_by == fx["active"]
        assert inserts(rec) == []
    assert stored(conn, fx["claim"].values()) == []


# ---------------------------------------------------------------------------
# Validasi argumen
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("created_by", [True, False, "1", 1.0, None])
def test_created_by_must_be_integer_before_database(created_by):
    with pytest.raises(ValidationError, match="created_by harus integer"):
        import_preliminary_notes(_NoDatabase(), created_by=created_by, notes=[])


def _gen():
    yield ("a", "b")


@pytest.mark.parametrize("notes", [None, "abc", _gen(), {"a": "b"}, {("a", "b")}, 5])
def test_notes_must_be_list_or_tuple_before_database(notes):
    with pytest.raises(ValidationError, match="notes harus list atau tuple"):
        import_preliminary_notes(_NoDatabase(), created_by=1, notes=notes)


@pytest.mark.parametrize(
    "row",
    ["ab", None, 5, {"a": "b"}, ("a",), ("a", "b", "c"), [], {"a", "b"}],
)
def test_row_must_be_a_pair(conn, fx, row):
    with pytest.raises(ValidationError, match=r"Baris 1: harus berupa pasangan"):
        run(conn, fx, [row])


@pytest.mark.parametrize("nosjp", [None, 5, b"x", "", "   ", "\t\n"])
def test_invalid_nosjp(conn, fx, nosjp):
    with pytest.raises(ValidationError, match=r"Baris 1: Nosjp tidak boleh kosong"):
        run(conn, fx, [(nosjp, "catatan")])


@pytest.mark.parametrize("note", [None, 5, b"x", "", "   ", "\t\n"])
def test_invalid_note(conn, fx, note):
    with pytest.raises(ValidationError, match=r"Baris 1: note tidak boleh kosong"):
        run(conn, fx, [(fx["nosjp"]["A"], note)])


def test_error_names_the_row_number(conn, fx):
    rows = [(fx["nosjp"]["A"], "ok"), (fx["nosjp"]["B"], "ok"), (fx["nosjp"]["C"], "  ")]
    with pytest.raises(ValidationError, match=r"Baris 3: note"):
        run(conn, fx, rows)
    assert stored(conn, fx["claim"].values()) == []


def test_first_invalid_row_is_reported(conn, fx):
    rows = [(fx["nosjp"]["A"], "ok"), ("", "x"), (fx["nosjp"]["B"], " ")]
    with pytest.raises(ValidationError, match=r"Baris 2: Nosjp"):
        run(conn, fx, rows)


def test_values_are_trimmed(conn, fx):
    nosjp = fx["nosjp"]["A"]
    result = run(conn, fx, [(f" \t{nosjp}\n ", "\n  isi catatan \t")])
    assert result.notes[0].nosjp == nosjp
    assert result.notes[0].claim_id == fx["claim"]["A"]
    assert stored(conn, fx["claim"].values())[0][2] == "isi catatan"


def test_inner_whitespace_is_kept(conn, fx):
    run(conn, fx, [(fx["nosjp"]["A"], "baris 1\n  baris 2")])
    assert stored(conn, fx["claim"].values())[0][2] == "baris 1\n  baris 2"


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

def test_unknown_user(conn, fx):
    rec = Recorder(conn)
    with pytest.raises(NotFoundError) as exc:
        import_preliminary_notes(rec, created_by=MISSING, notes=[(fx["nosjp"]["A"], "x")])
    assert str(exc.value) == f"User id={MISSING} tidak ditemukan."
    assert inserts(rec) == []
    assert stored(conn, fx["claim"].values()) == []


def test_inactive_user(conn, fx):
    rec = Recorder(conn)
    with pytest.raises(InvalidStateError) as exc:
        import_preliminary_notes(rec, created_by=fx["inactive"], notes=[(fx["nosjp"]["A"], "x")])
    assert str(exc.value) == f"User id={fx['inactive']} tidak aktif."
    assert inserts(rec) == []
    assert stored(conn, fx["claim"].values()) == []


# ---------------------------------------------------------------------------
# Klaim
# ---------------------------------------------------------------------------

def test_unknown_nosjp(conn, fx):
    with pytest.raises(NotFoundError, match=r"Baris 1: Claim dengan Nosjp=TIDAK-ADA tidak ditemukan"):
        run(conn, fx, [("TIDAK-ADA", "x")])


def test_missing_nosjp_in_last_row_leaves_no_insert(conn, fx):
    rec = Recorder(conn)
    rows = [(fx["nosjp"]["A"], "a"), (fx["nosjp"]["B"], "b"), ("TIDAK-ADA", "c")]
    with pytest.raises(NotFoundError, match="Baris 3"):
        import_preliminary_notes(rec, created_by=fx["active"], notes=rows)
    # Bukan sekadar rollback caller: INSERT tidak pernah dikirim.
    assert inserts(rec) == []
    assert stored(conn, fx["claim"].values()) == []


def test_first_missing_nosjp_by_input_order_is_reported(conn, fx):
    rows = [(fx["nosjp"]["A"], "a"), ("HILANG-2", "b"), ("HILANG-3", "c")]
    with pytest.raises(NotFoundError, match="HILANG-2"):
        run(conn, fx, rows)
    rows = [("HILANG-3", "a"), ("HILANG-2", "b")]
    with pytest.raises(NotFoundError, match="HILANG-3"):
        run(conn, fx, rows)


@pytest.mark.parametrize("key", ["A", "B", "C"])
def test_any_claim_is_accepted_regardless_of_review_or_status(conn, fx, key):
    # A: review OPEN (PENDING), B: hanya review CLOSED (VPK), C: tanpa review (AUDIT).
    result = run(conn, fx, [(fx["nosjp"][key], "catatan")])
    assert result.notes[0].claim_id == fx["claim"][key]


def test_claim_status_does_not_matter(conn, fx):
    with conn.cursor() as cur:
        cur.execute("UPDATE core.claims SET claim_status = %s WHERE id = %s", (AUDIT, fx["claim"]["A"]))
    assert run(conn, fx, [(fx["nosjp"]["A"], "x")]).created == 1


# ---------------------------------------------------------------------------
# Urutan validasi
# ---------------------------------------------------------------------------

def test_notes_shape_is_checked_before_created_by():
    with pytest.raises(ValidationError, match="notes harus list atau tuple"):
        import_preliminary_notes(_NoDatabase(), created_by=True, notes="abc")


def test_created_by_type_is_checked_before_the_user_lookup():
    with pytest.raises(ValidationError, match="created_by harus integer"):
        import_preliminary_notes(_NoDatabase(), created_by="1", notes=["bukan pasangan"])


def test_user_is_checked_before_rows(conn, fx):
    with pytest.raises(NotFoundError, match="User id="):
        import_preliminary_notes(conn, created_by=MISSING, notes=["bukan pasangan"])
    with pytest.raises(InvalidStateError, match="tidak aktif"):
        import_preliminary_notes(conn, created_by=fx["inactive"], notes=[("", "")])


def test_all_rows_are_validated_before_any_claim_is_resolved(conn, fx):
    rec = Recorder(conn)
    rows = [("HILANG", "a"), (fx["nosjp"]["A"], "  ")]
    with pytest.raises(ValidationError, match="Baris 2"):
        import_preliminary_notes(rec, created_by=fx["active"], notes=rows)
    assert not any("core.claims" in s for s in rec.sql)
    assert inserts(rec) == []


def test_user_lookup_comes_first_then_all_claims_then_inserts(conn, fx):
    rec = Recorder(conn)
    rows = [(fx["nosjp"][k], "n") for k in "ABCA"]
    import_preliminary_notes(rec, created_by=fx["active"], notes=rows)
    assert "core.users" in rec.sql[0]
    claim_idx = [i for i, s in enumerate(rec.sql) if "core.claims" in s]
    insert_idx = [i for i, s in enumerate(rec.sql) if s.startswith("INSERT")]
    assert len(insert_idx) == 4
    assert max(claim_idx) < min(insert_idx)
    assert len(claim_idx) == 3  # Nosjp berulang dilookup sekali


# ---------------------------------------------------------------------------
# Duplikat
# ---------------------------------------------------------------------------

def test_same_claim_with_different_notes(conn, fx):
    n = fx["nosjp"]["A"]
    result = run(conn, fx, [(n, "N1"), (n, "N2")])
    assert result.created == 2
    assert [r[2] for r in stored(conn, [fx["claim"]["A"]])] == ["N1", "N2"]


def test_identical_rows_in_the_same_batch(conn, fx):
    n = fx["nosjp"]["A"]
    result = run(conn, fx, [(n, "N1"), (n, "N1")])
    assert result.rows_total == result.created == len(result.notes) == 2
    assert result.notes[0].note_id != result.notes[1].note_id
    assert result.notes[0].claim_id == result.notes[1].claim_id


def test_rerunning_the_same_batch_creates_new_notes(conn, fx):
    rows = [(fx["nosjp"]["A"], "N1"), (fx["nosjp"]["B"], "N2")]
    first = run(conn, fx, rows)
    second = run(conn, fx, rows)
    assert {n.note_id for n in first.notes}.isdisjoint({n.note_id for n in second.notes})
    assert len(stored(conn, fx["claim"].values())) == 4


def test_existing_notes_do_not_prevent_import(conn, fx):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review.preliminary_notes (claim_id, note, created_by) VALUES (%s, 'lama', %s)",
            (fx["claim"]["A"], fx["active"]),
        )
    run(conn, fx, [(fx["nosjp"]["A"], "baru")])
    assert [r[2] for r in stored(conn, [fx["claim"]["A"]])] == ["lama", "baru"]


# ---------------------------------------------------------------------------
# Transaksi
# ---------------------------------------------------------------------------

def test_no_commit_rollback_savepoint_or_print(conn, fx, capsys):
    rec = Recorder(conn)  # commit/rollback melempar AssertionError
    import_preliminary_notes(rec, created_by=fx["active"], notes=[(fx["nosjp"]["A"], "x")])
    assert not any("SAVEPOINT" in s.upper() for s in rec.sql)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_source_has_no_commit_rollback_print_or_savepoint():
    src = inspect.getsource(preliminary_note_service)
    for token in (".commit(", ".rollback(", "print(", "SAVEPOINT", "savepoint"):
        assert token not in src


def test_caller_rollback_removes_the_whole_successful_batch(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    try:
        run(conn, fx, [(fx["nosjp"]["A"], "a"), (fx["nosjp"]["B"], "b"), (fx["nosjp"]["A"], "c")])
        assert len(stored(conn, fx["claim"].values())) == 3
        conn.rollback()
        assert stored(conn, fx["claim"].values()) == []
    finally:
        conn.close()


def test_caller_commit_persists_the_batch(pg_dsn, committed_fx):
    fx = committed_fx
    conn = psycopg2.connect(**pg_dsn)
    other = psycopg2.connect(**pg_dsn)
    try:
        run(conn, fx, [(fx["nosjp"]["A"], "a"), (fx["nosjp"]["B"], "b")])
        assert stored(other, fx["claim"].values()) == []
        other.rollback()
        conn.commit()
        assert len(stored(other, fx["claim"].values())) == 2
    finally:
        conn.close()
        other.close()


def test_failure_leaves_nothing_even_without_caller_rollback(conn, fx):
    for rows, error in (
        ([(fx["nosjp"]["A"], "a"), ("HILANG", "b")], NotFoundError),
        ([(fx["nosjp"]["A"], "a"), (fx["nosjp"]["B"], "")], ValidationError),
    ):
        with pytest.raises(error):
            run(conn, fx, rows)
        assert stored(conn, fx["claim"].values()) == []


def test_database_error_during_persistence_is_propagated(conn, fx, monkeypatch):
    # created_by valid tetapi note melanggar CHECK DB bila lolos validasi service.
    monkeypatch.setattr(preliminary_note_service, "_clean_rows", lambda notes: [(fx["nosjp"]["A"], "   ")])
    with pytest.raises(psycopg2.errors.CheckViolation):
        run(conn, fx, [(fx["nosjp"]["A"], "x")])


def test_check_constraint_is_still_active(conn, fx):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO review.preliminary_notes (claim_id, note, created_by) VALUES (%s, '  ', %s)",
                (fx["claim"]["A"], fx["active"]),
            )


# ---------------------------------------------------------------------------
# Efek samping
# ---------------------------------------------------------------------------

def test_no_other_table_changes(conn, fx):
    before = snapshot_other(conn, fx)
    run(conn, fx, [(fx["nosjp"][k], "n") for k in "ABCA"])
    assert snapshot_other(conn, fx) == before


def test_no_event_is_written(conn, fx):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review.review_events")
        before = cur.fetchone()[0]
    run(conn, fx, [(fx["nosjp"]["A"], "n")])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review.review_events")
        assert cur.fetchone()[0] == before


def test_only_the_notes_table_is_written(conn, fx):
    rec = Recorder(conn)
    import_preliminary_notes(rec, created_by=fx["active"], notes=[(fx["nosjp"]["A"], "n")])
    writes = [s for s in rec.sql if s.split()[0] in ("INSERT", "UPDATE", "DELETE")]
    assert writes and all("review.preliminary_notes" in s for s in writes)


# ---------------------------------------------------------------------------
# Locking dan konkurensi
# ---------------------------------------------------------------------------

def test_claim_lookup_has_no_locking_clause(conn, fx):
    rec = Recorder(conn)
    import_preliminary_notes(rec, created_by=fx["active"], notes=[(fx["nosjp"]["A"], "n")])
    for sql in rec.sql:
        assert " FOR " not in sql.upper() or sql.startswith("INSERT"), sql
    lookups = [s for s in rec.sql if "core.claims" in s]
    assert lookups and all("FOR" not in s.split("WHERE")[-1] for s in lookups)


def test_user_lookup_has_no_locking_clause(conn, fx):
    rec = Recorder(conn)
    import_preliminary_notes(rec, created_by=fx["active"], notes=[])
    assert "FOR" not in rec.sql[0].split("WHERE")[-1]


def test_import_is_not_blocked_by_open_review_cycle_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    opener = psycopg2.connect(**pg_dsn)
    try:
        # C tanpa review: membuka cycle menahan kunci klaim C (FOR NO KEY UPDATE) dan review baru.
        open_review_cycle(opener, nosjp=fx["nosjp"]["C"], opened_by=fx["active"])
        thread, outcome = _run_in_thread(
            pg_dsn, lambda c: run(c, fx, [(fx["nosjp"]["C"], "saat open"), (fx["nosjp"]["A"], "x")])
        )
        thread.join(timeout=5)
        assert not thread.is_alive(), "import tidak boleh menunggu open_review_cycle"
        assert "error" not in outcome and outcome["result"].created == 2
    finally:
        opener.rollback()
        opener.close()


def test_open_review_cycle_is_not_blocked_by_import_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    importer = psycopg2.connect(**pg_dsn)
    try:
        run(importer, fx, [(fx["nosjp"]["C"], "saat import")])
        thread, outcome = _run_in_thread(
            pg_dsn, lambda c: open_review_cycle(c, nosjp=fx["nosjp"]["C"], opened_by=fx["active"])
        )
        thread.join(timeout=5)
        assert not thread.is_alive(), "open_review_cycle tidak boleh menunggu import"
        assert "error" not in outcome and outcome["result"].created is True
    finally:
        importer.rollback()
        importer.close()


def test_import_is_not_blocked_by_close_review_in_progress(pg_dsn, committed_fx):
    fx = committed_fx
    closer = psycopg2.connect(**pg_dsn)
    try:
        close_review(
            closer, review_id=fx["open_review"], closed_by=fx["active"],
            final_decision="LAYAK", resolution_note="Selesai.",
        )
        thread, outcome = _run_in_thread(pg_dsn, lambda c: run(c, fx, [(fx["nosjp"]["A"], "saat close")]))
        thread.join(timeout=5)
        assert not thread.is_alive(), "import tidak boleh menunggu close_review"
        assert "error" not in outcome and outcome["result"].created == 1
    finally:
        closer.rollback()
        closer.close()


def test_concurrent_imports_do_not_deadlock(pg_dsn, committed_fx):
    fx = committed_fx
    keys_a, keys_b = "ABC" * 3, "CBA" * 3
    threads, outcomes = [], []
    for i in range(6):
        keys = keys_a if i % 2 == 0 else keys_b
        rows = [(fx["nosjp"][k], f"t{i}-{j}") for j, k in enumerate(keys)]
        thread, outcome = _run_in_thread(pg_dsn, lambda c, rows=rows: run(c, fx, rows))
        threads.append(thread)
        outcomes.append(outcome)
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "import konkuren macet"
    assert all("error" not in o and o["result"].created == 9 for o in outcomes)
    conn = psycopg2.connect(**pg_dsn)
    try:
        assert len(stored(conn, fx["claim"].values())) == 54
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Arsitektur
# ---------------------------------------------------------------------------

def test_shared_user_helper_is_used_and_no_inline_user_query():
    src = inspect.getsource(preliminary_note_service)
    assert "user_service.require_active_user" in src
    assert "core.users" not in src


def test_user_validation_goes_through_the_helper(conn, fx, monkeypatch):
    calls = []
    real = preliminary_note_service.user_service.require_active_user

    def spy(c, user_id):
        calls.append(user_id)
        return real(c, user_id)

    monkeypatch.setattr(preliminary_note_service.user_service, "require_active_user", spy)
    run(conn, fx, [])
    assert calls == [fx["active"]]


def test_result_dataclasses_are_frozen():
    assert ImportPreliminaryNotesResult.__dataclass_params__.frozen
    assert PreliminaryNote.__dataclass_params__.frozen


def test_audit_layer_is_untouched_by_the_service():
    src = inspect.getsource(preliminary_note_service)
    assert "audit_service" not in src and "review_events" not in src
    from deskon import constants

    assert constants.EntityType.PRELIMINARY_NOTE not in constants.EVENT_ENTITY_TYPE.values()
