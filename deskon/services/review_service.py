"""Use case lifecycle review klaim (review.claim_reviews).

Saat ini berisi `open_review_cycle` dan `close_review`. Use case lifecycle
lain ditambahkan di modul ini secara bertahap.

Semua fungsi menerima koneksi dari caller, tidak commit/rollback, tidak
print, mengembalikan dataclass, dan melempar exception dari deskon.errors.
"""

from dataclasses import dataclass
from datetime import datetime

from psycopg2 import errors as pg_errors

from deskon.constants import (
    FINAL_DECISIONS,
    REVIEW_TYPES,
    EntityType,
    EventType,
    ReviewStatus,
)
from deskon.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from deskon.services import audit_service
from deskon.services.audit_service import ReviewEvent


class ReviewTypeSource:
    """Asal review_type sebuah cycle baru; dicatat di event_data."""

    EXPLICIT = "explicit"
    CLAIM_STATUS = "claim_status"


@dataclass(frozen=True)
class ClaimReview:
    id: int
    claim_id: int
    nosjp: str
    status: str
    review_type: str
    cycle_no: int
    opened_by: int
    opened_at: datetime


@dataclass(frozen=True)
class OpenReviewCycleResult:
    review: ClaimReview
    # False: klaim sudah punya review OPEN; review itu yang dikembalikan,
    # tidak ada yang dibuat dan tidak ada event.
    created: bool
    # Hanya diisi jika created: review sebelumnya untuk klaim ini, urut cycle.
    previous_review_ids: tuple[int, ...]
    event: ReviewEvent | None


@dataclass(frozen=True)
class ClosedReview:
    id: int
    claim_id: int
    nosjp: str
    status: str
    review_type: str
    cycle_no: int
    final_decision: str
    resolution_note: str
    opened_by: int
    opened_at: datetime
    closed_by: int
    closed_at: datetime


@dataclass(frozen=True)
class CloseReviewResult:
    review: ClosedReview
    event: ReviewEvent


_REVIEW_COLUMNS = "id, claim_id, status, review_type, cycle_no, opened_by, opened_at"


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} harus integer, bukan {value!r}")


def _claim_review(row, nosjp):
    review_id, claim_id, status, review_type, cycle_no, opened_by, opened_at = row
    return ClaimReview(
        id=review_id,
        claim_id=claim_id,
        nosjp=nosjp,
        status=status,
        review_type=review_type,
        cycle_no=cycle_no,
        opened_by=opened_by,
        opened_at=opened_at,
    )


def open_review_cycle(conn, *, nosjp, opened_by, review_type=None):
    """Buka review cycle baru untuk klaim `nosjp`. Tidak commit.

    - User `opened_by` harus ada dan aktif.
    - Klaim dicari lewat Nosjp; tidak ada atau lebih dari satu = error.
    - Jika klaim sudah punya review OPEN: tidak membuat apa pun dan
      mengembalikan review itu (created=False, tanpa event).
    - Jika tidak: review OPEN baru dengan cycle_no = cycle terakhir + 1 dan
      review_type = parameter eksplisit, atau core.claims.claim_status saat
      ini jika tidak diberikan. Event REVIEW_OPENED ditulis lewat
      audit_service di transaksi yang sama.

    Konkurensi: baris klaim dikunci (FOR NO KEY UPDATE) sebelum memeriksa
    review OPEN dan menghitung cycle_no, sehingga dua pemanggil untuk klaim
    yang sama berjalan berurutan. Pemanggil kedua menunggu, lalu melihat
    review OPEN milik pemanggil pertama. UNIQUE (claim_id, cycle_no) dan
    index unik review OPEN per klaim tetap menjadi pengaman terakhir untuk
    penulis lain yang tidak mengambil kunci ini; pelanggarannya dilempar
    sebagai ConflictError dan transaksi caller harus di-rollback.
    """
    if not isinstance(nosjp, str) or not nosjp.strip():
        raise ValidationError("Nosjp tidak boleh kosong.")
    nosjp = nosjp.strip()
    _require_int("opened_by", opened_by)
    if review_type is not None and review_type not in REVIEW_TYPES:
        raise ValidationError(
            f"review_type tidak valid: {review_type!r}; "
            f"harus salah satu dari {', '.join(sorted(REVIEW_TYPES))}"
        )

    with conn.cursor() as cur:
        # 1. User
        cur.execute("SELECT is_active FROM core.users WHERE id = %s", (opened_by,))
        user = cur.fetchone()
        if user is None:
            raise NotFoundError(f"User id={opened_by} tidak ditemukan.")
        if not user[0]:
            raise InvalidStateError(f"User id={opened_by} tidak aktif.")

        # 2. Klaim, dikunci untuk menserialkan pembukaan cycle per klaim.
        # FOR NO KEY UPDATE (bukan FOR UPDATE) agar insert lain yang hanya
        # merujuk klaim lewat foreign key tidak ikut tertahan.
        cur.execute(
            """
            SELECT id, nosjp, claim_status
            FROM core.claims
            WHERE nosjp = %s
            FOR NO KEY UPDATE
            """,
            (nosjp,),
        )
        claims = cur.fetchall()
        if not claims:
            raise NotFoundError(f"Claim dengan Nosjp={nosjp} tidak ditemukan.")
        if len(claims) > 1:
            raise ConflictError(
                f"Ditemukan {len(claims)} claim dengan Nosjp={nosjp}. "
                "Nosjp seharusnya unik di core.claims."
            )
        claim_id, claim_nosjp, claim_status = claims[0]

        # 3. Review OPEN yang sudah ada
        cur.execute(
            f"""
            SELECT {_REVIEW_COLUMNS}
            FROM review.claim_reviews
            WHERE claim_id = %s
              AND status = %s
            FOR UPDATE
            """,
            (claim_id, ReviewStatus.OPEN),
        )
        existing = cur.fetchone()
        if existing is not None:
            return OpenReviewCycleResult(
                review=_claim_review(existing, claim_nosjp),
                created=False,
                previous_review_ids=(),
                event=None,
            )

        # 4. History dan cycle berikutnya
        cur.execute(
            """
            SELECT id, cycle_no
            FROM review.claim_reviews
            WHERE claim_id = %s
            ORDER BY cycle_no, id
            """,
            (claim_id,),
        )
        history = cur.fetchall()
        previous_review_ids = tuple(row[0] for row in history)
        cycle_no = max((row[1] for row in history), default=0) + 1

        if review_type is not None:
            resolved_type, type_source = review_type, ReviewTypeSource.EXPLICIT
        else:
            resolved_type, type_source = claim_status, ReviewTypeSource.CLAIM_STATUS
            if resolved_type not in REVIEW_TYPES:
                raise InvalidStateError(
                    f"claim_status {claim_status!r} tidak bisa dipakai sebagai review_type."
                )

        # 5. Review baru
        try:
            cur.execute(
                f"""
                INSERT INTO review.claim_reviews (
                    claim_id, status, opened_by, cycle_no, review_type
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_REVIEW_COLUMNS}
                """,
                (claim_id, ReviewStatus.OPEN, opened_by, cycle_no, resolved_type),
            )
        except pg_errors.UniqueViolation as exc:
            raise ConflictError(
                f"Review cycle untuk Nosjp={claim_nosjp} bertabrakan dengan "
                f"penulis lain ({exc.diag.constraint_name}); rollback dan ulangi."
            ) from exc
        review = _claim_review(cur.fetchone(), claim_nosjp)

    # 6. Audit, setelah review berhasil dibuat.
    event = audit_service.record_event(
        conn,
        review_id=review.id,
        user_id=opened_by,
        event_type=EventType.REVIEW_OPENED,
        entity_type=EntityType.CLAIM_REVIEW,
        entity_id=review.id,
        payload={
            "new_review_id": review.id,
            "cycle_no": review.cycle_no,
            "review_type": review.review_type,
            "review_type_source": type_source,
            "previous_review_count": len(previous_review_ids),
            "previous_review_ids": list(previous_review_ids),
        },
    )

    return OpenReviewCycleResult(
        review=review,
        created=True,
        previous_review_ids=previous_review_ids,
        event=event,
    )


def close_review(conn, *, review_id, closed_by, final_decision, resolution_note):
    """Tutup review OPEN dengan keputusan akhir. Tidak commit.

    - final_decision di-strip dan di-upper (seperti script lama), lalu harus
      LAYAK, TIDAK_LAYAK, atau RESELEKSI.
    - resolution_note di-strip dan wajib tidak kosong (seperti script lama).
    - User `closed_by` harus ada dan aktif.
    - Review harus ada dan berstatus OPEN; review CLOSED tidak pernah diubah.
    - Review menjadi CLOSED dengan final_decision, resolution_note, closed_by,
      dan closed_at. Finding, reselection, komentar, dan klaim tidak disentuh.
      Event REVIEW_CLOSED ditulis lewat audit_service di transaksi yang sama.

    Konkurensi: baris review dikunci FOR NO KEY UPDATE sebelum status
    diperiksa. Mode ini berkonflik dengan FOR SHARE milik
    create_review_finding, dengan penutupan lain, dan dengan FOR UPDATE milik
    open_review_cycle, sehingga semuanya berjalan berurutan dan pihak yang
    menunggu membaca status terbaru. FK check (FOR KEY SHARE) dari insert
    lain tidak tertahan, dan baris klaim tidak dikunci.
    """
    _require_int("review_id", review_id)
    _require_int("closed_by", closed_by)
    if not isinstance(final_decision, str):
        raise ValidationError(f"final_decision harus string, bukan {final_decision!r}")
    final_decision = final_decision.strip().upper()
    if final_decision not in FINAL_DECISIONS:
        raise ValidationError(
            f"final_decision tidak valid: {final_decision!r}; "
            f"harus salah satu dari {', '.join(sorted(FINAL_DECISIONS))}"
        )
    if resolution_note is not None and not isinstance(resolution_note, str):
        raise ValidationError(f"resolution_note harus string, bukan {resolution_note!r}")
    resolution_note = resolution_note.strip() if resolution_note is not None else ""
    if not resolution_note:
        raise ValidationError("resolution_note wajib diisi ketika review ditutup.")

    with conn.cursor() as cur:
        # 1. User
        cur.execute("SELECT is_active FROM core.users WHERE id = %s", (closed_by,))
        user = cur.fetchone()
        if user is None:
            raise NotFoundError(f"User id={closed_by} tidak ditemukan.")
        if not user[0]:
            raise InvalidStateError(f"User id={closed_by} tidak aktif.")

        # 2. Review, dikunci agar finding, penutupan lain, dan pembukaan
        # cycle baru untuk review ini menunggu. OF cr: klaim tidak dikunci.
        cur.execute(
            """
            SELECT cr.status, c.nosjp
            FROM review.claim_reviews AS cr
            JOIN core.claims AS c ON c.id = cr.claim_id
            WHERE cr.id = %s
            FOR NO KEY UPDATE OF cr
            """,
            (review_id,),
        )
        review = cur.fetchone()
        if review is None:
            raise NotFoundError(f"Review id={review_id} tidak ditemukan.")
        review_status, nosjp = review
        if review_status != ReviewStatus.OPEN:
            raise InvalidStateError(
                f"Review id={review_id} tidak dapat ditutup karena status saat ini "
                f"adalah {review_status}."
            )

        # 3. Penutupan. Syarat status OPEN tetap ada sebagai pengaman untuk
        # penulis lain yang tidak mengambil kunci di atas.
        cur.execute(
            """
            UPDATE review.claim_reviews
            SET status = %s,
                final_decision = %s,
                resolution_note = %s,
                closed_by = %s,
                closed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND status = %s
            RETURNING id, claim_id, status, review_type, cycle_no, final_decision,
                      resolution_note, opened_by, opened_at, closed_by, closed_at
            """,
            (
                ReviewStatus.CLOSED,
                final_decision,
                resolution_note,
                closed_by,
                review_id,
                ReviewStatus.OPEN,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise ConflictError(
                f"Review id={review_id} diubah penulis lain saat ditutup; rollback dan ulangi."
            )
        (
            closed_id, claim_id, status, review_type, cycle_no, decision,
            note, opened_by, opened_at, review_closed_by, closed_at,
        ) = row
        closed = ClosedReview(
            id=closed_id,
            claim_id=claim_id,
            nosjp=nosjp,
            status=status,
            review_type=review_type,
            cycle_no=cycle_no,
            final_decision=decision,
            resolution_note=note,
            opened_by=opened_by,
            opened_at=opened_at,
            closed_by=review_closed_by,
            closed_at=closed_at,
        )

    # 4. Audit, setelah review berhasil ditutup.
    event = audit_service.record_event(
        conn,
        review_id=closed.id,
        user_id=closed_by,
        event_type=EventType.REVIEW_CLOSED,
        entity_type=EntityType.CLAIM_REVIEW,
        entity_id=closed.id,
        payload={
            # Nama kunci sama dengan event script lama.
            "final_decision": closed.final_decision,
            "resolution_note": closed.resolution_note,
        },
    )

    return CloseReviewResult(review=closed, event=event)
