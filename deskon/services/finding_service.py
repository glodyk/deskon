"""Use case finding review (review.review_findings).

Saat ini berisi `create_review_finding`. Publish/correct/cancel belum ada;
event FINDING_PUBLISHED/CORRECTED/CANCELLED masih cadangan.

Semua fungsi menerima koneksi dari caller, tidak commit/rollback, tidak
print, mengembalikan dataclass, dan melempar exception dari deskon.errors.
"""

from dataclasses import dataclass
from datetime import datetime

from deskon.constants import EntityType, EventType, FindingStatus, ReviewStatus
from deskon.errors import InvalidStateError, NotFoundError, ValidationError
from deskon.services import audit_service, user_service
from deskon.services.audit_service import ReviewEvent


@dataclass(frozen=True)
class ReviewFinding:
    id: int
    review_id: int
    claim_id: int
    nosjp: str
    finding_category: str
    finding_title: str
    finding_description: str
    status: str
    created_by: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CreateReviewFindingResult:
    finding: ReviewFinding
    event: ReviewEvent


_FINDING_COLUMNS = (
    "id, review_id, finding_category, finding_title, finding_description, "
    "status, created_by, created_at, updated_at"
)


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} harus integer, bukan {value!r}")


def _clean_text(name, value, max_length):
    """Seperti clean() di script lama: strip, kosong = tidak boleh.

    max_length mengikuti kolom review.review_findings (001_baseline.sql).
    """
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} harus string, bukan {value!r}")
    value = value.strip() if value is not None else ""
    if not value:
        raise ValidationError(f"{name} tidak boleh kosong.")
    if max_length is not None and len(value) > max_length:
        raise ValidationError(f"{name} maksimal {max_length} karakter, diberikan {len(value)}.")
    return value


def create_review_finding(
    conn,
    *,
    review_id,
    finding_category,
    finding_title,
    finding_description,
    created_by,
):
    """Buat finding DRAFT pada review OPEN. Tidak commit.

    - category, title, description di-strip dan tidak boleh kosong.
    - User `created_by` harus ada dan aktif.
    - Review harus ada dan berstatus OPEN; review CLOSED ditolak.
    - Finding disimpan dengan status DRAFT dan created_by. Event
      FINDING_CREATED (entity review_finding) ditulis lewat audit_service
      di transaksi yang sama.

    Konkurensi: baris review dikunci FOR SHARE sebelum status diperiksa.
    Penutupan review (UPDATE status) menunggu sampai transaksi ini selesai,
    dan sebaliknya pembuatan finding menunggu penutupan yang sedang berjalan,
    lalu membaca status terbaru (CLOSED = ditolak). Beberapa finding pada
    review yang sama tetap bisa dibuat bersamaan.
    """
    _require_int("review_id", review_id)
    _require_int("created_by", created_by)
    finding_category = _clean_text("finding_category", finding_category, 100)
    finding_title = _clean_text("finding_title", finding_title, 255)
    finding_description = _clean_text("finding_description", finding_description, None)

    with conn.cursor() as cur:
        # 1. User
        user_service.require_active_user(conn, created_by)

        # 2. Review, dikunci agar tidak ditutup selagi finding dibuat.
        cur.execute(
            """
            SELECT cr.claim_id, cr.status, c.nosjp
            FROM review.claim_reviews AS cr
            JOIN core.claims AS c ON c.id = cr.claim_id
            WHERE cr.id = %s
            FOR SHARE OF cr
            """,
            (review_id,),
        )
        review = cur.fetchone()
        if review is None:
            raise NotFoundError(f"Review id={review_id} tidak ditemukan.")
        claim_id, review_status, nosjp = review
        if review_status != ReviewStatus.OPEN:
            raise InvalidStateError(
                f"Review id={review_id} sudah {review_status}. "
                "Finding baru hanya dapat dibuat pada review OPEN."
            )

        # 3. Finding
        cur.execute(
            f"""
            INSERT INTO review.review_findings (
                review_id, finding_category, finding_title,
                finding_description, status, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_FINDING_COLUMNS}
            """,
            (
                review_id,
                finding_category,
                finding_title,
                finding_description,
                FindingStatus.DRAFT,
                created_by,
            ),
        )
        (
            finding_id, finding_review_id, category, title, description,
            status, finding_created_by, created_at, updated_at,
        ) = cur.fetchone()
        finding = ReviewFinding(
            id=finding_id,
            review_id=finding_review_id,
            claim_id=claim_id,
            nosjp=nosjp,
            finding_category=category,
            finding_title=title,
            finding_description=description,
            status=status,
            created_by=finding_created_by,
            created_at=created_at,
            updated_at=updated_at,
        )

    # 4. Audit, setelah finding berhasil dibuat.
    event = audit_service.record_event(
        conn,
        review_id=review_id,
        user_id=created_by,
        event_type=EventType.FINDING_CREATED,
        entity_type=EntityType.REVIEW_FINDING,
        entity_id=finding.id,
        payload={
            "finding_id": finding.id,
            # Nama kunci sama dengan event script lama.
            "finding_category": finding.finding_category,
            "status": finding.status,
        },
    )

    return CreateReviewFindingResult(finding=finding, event=event)
