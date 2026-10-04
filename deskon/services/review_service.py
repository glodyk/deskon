"""Use case lifecycle review klaim (review.claim_reviews).

Saat ini berisi `open_review_cycle`. Use case lain (`close_review`, ...)
ditambahkan di modul ini secara bertahap.

Semua fungsi menerima koneksi dari caller, tidak commit/rollback, tidak
print, mengembalikan dataclass, dan melempar exception dari deskon.errors.
"""

from dataclasses import dataclass
from datetime import datetime

from psycopg2 import errors as pg_errors

from deskon.constants import REVIEW_TYPES, EntityType, EventType, ReviewStatus
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
