"""Use case reselection klaim (review.claim_reselections).

Saat ini berisi `create_reselection` dan `resolve_reselection`. Correct dan
cancel belum ada; RESELECTION_CORRECTED dan RESELECTION_CANCELLED masih
cadangan.

Semua fungsi menerima koneksi dari caller, tidak commit/rollback, tidak
print, mengembalikan dataclass, dan melempar exception dari deskon.errors.
"""

from dataclasses import dataclass
from datetime import datetime

from deskon.constants import (
    RESELECTION_ACTIONS,
    RESELECTION_TARGET_TYPES,
    EntityType,
    EventType,
    ReselectionAction,
    ReselectionStatus,
    ReviewStatus,
)
from deskon.errors import ConflictError, InvalidStateError, NotFoundError, ValidationError
from deskon.services import audit_service, user_service
from deskon.services.audit_service import ReviewEvent


@dataclass(frozen=True)
class ClaimReselection:
    id: int
    review_id: int
    claim_id: int
    nosjp: str
    target_type: str
    action: str
    original_code: str
    original_description: str | None
    proposed_code: str | None
    proposed_description: str | None
    reason: str
    status: str
    created_by: int
    created_at: datetime
    updated_at: datetime
    agreed_by: int | None
    agreed_at: datetime | None
    corrects_reselection_id: int | None


@dataclass(frozen=True)
class CreateReselectionResult:
    reselection: ClaimReselection
    event: ReviewEvent


_RESELECTION_COLUMNS = (
    "id, review_id, claim_id, target_type, action, original_code, "
    "original_description, proposed_code, proposed_description, reason, status, "
    "created_by, created_at, updated_at, agreed_by, agreed_at, corrects_reselection_id"
)

_CODE_MAX_LENGTH = 100


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} harus integer, bukan {value!r}")


def _text(name, value):
    """Strip; kosong menjadi None. Bukan string (selain None) ditolak."""
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} harus string, bukan {value!r}")
    value = value.strip() if value is not None else ""
    return value or None


def _required(name, value):
    value = _text(name, value)
    if value is None:
        raise ValidationError(f"{name} wajib diisi.")
    return value


def _max_length(name, value, limit):
    """max_length mengikuti kolom review.claim_reselections (001_baseline.sql)."""
    if value is not None and len(value) > limit:
        raise ValidationError(f"{name} maksimal {limit} karakter, diberikan {len(value)}.")


def _choice(name, value, allowed):
    """Seperti script lama: strip + upper, harus salah satu nilai yang diizinkan."""
    if not isinstance(value, str):
        raise ValidationError(f"{name} harus string, bukan {value!r}")
    value = value.strip().upper()
    if value not in allowed:
        raise ValidationError(f"{name} tidak valid: {value!r}; harus salah satu dari {', '.join(sorted(allowed))}")
    return value


def create_reselection(
    conn,
    *,
    review_id,
    created_by,
    target_type,
    action,
    original_code,
    reason,
    original_description=None,
    proposed_code=None,
    proposed_description=None,
):
    """Buat reselection PROPOSED pada review OPEN. Tidak commit.

    - action CHANGE/DROP dan target_type DIAGNOSIS/PROCEDURE di-strip dan
      di-upper; original_code dan reason di-strip dan wajib; description
      kosong disimpan NULL.
    - CHANGE: proposed_code wajib. DROP: proposed_code dan
      proposed_description harus kosong (jika diberikan, ditolak) dan
      disimpan NULL.
    - User `created_by` harus ada dan aktif.
    - Review harus ada dan OPEN; claim_id diambil dari review, bukan dari
      pemanggil. Reselection disimpan PROPOSED dengan created_by;
      agreed_by/agreed_at dan corrects_reselection_id NULL.
    - Tidak ada pemeriksaan baru: original_code tidak dicocokkan dengan data
      klaim, proposed_code boleh sama dengan original_code, dan usulan
      ganda diizinkan (seperti script lama).
    - Event RESELECTION_CREATED (entity claim_reselection) ditulis lewat
      audit_service di transaksi yang sama.

    Konkurensi: baris review dikunci FOR SHARE (OF cr) sebelum status
    diperiksa; baris klaim tidak dikunci. Penutupan review menunggu sampai
    transaksi ini selesai, dan sebaliknya pembuatan reselection menunggu
    penutupan yang sedang berjalan, lalu membaca status terbaru (CLOSED =
    ditolak). Beberapa reselection dan finding pada review yang sama tetap
    bisa dibuat bersamaan.
    """
    _require_int("review_id", review_id)
    _require_int("created_by", created_by)
    # Urutan seperti script lama: action, target_type, original_code,
    # reason, lalu proposed_code untuk CHANGE.
    action = _choice("action", action, RESELECTION_ACTIONS)
    target_type = _choice("target_type", target_type, RESELECTION_TARGET_TYPES)
    original_code = _required("original_code", original_code)
    original_description = _text("original_description", original_description)
    reason = _required("reason", reason)
    proposed_code = _text("proposed_code", proposed_code)
    proposed_description = _text("proposed_description", proposed_description)
    if action == ReselectionAction.CHANGE:
        if proposed_code is None:
            raise ValidationError("proposed_code wajib diisi untuk action CHANGE.")
    elif proposed_code is not None or proposed_description is not None:
        raise ValidationError("proposed_code dan proposed_description tidak boleh diisi untuk action DROP.")
    _max_length("original_code", original_code, _CODE_MAX_LENGTH)
    _max_length("proposed_code", proposed_code, _CODE_MAX_LENGTH)

    with conn.cursor() as cur:
        # 1. User
        user_service.require_active_user(conn, created_by)

        # 2. Review, dikunci agar tidak ditutup selagi reselection dibuat.
        # OF cr: baris klaim tidak dikunci.
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
                f"Review id={review_id} tidak OPEN. Status saat ini: {review_status}"
            )

        # 3. Reselection
        cur.execute(
            f"""
            INSERT INTO review.claim_reselections (
                claim_id, review_id, target_type, action, original_code,
                original_description, proposed_code, proposed_description,
                reason, status, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_RESELECTION_COLUMNS}
            """,
            (
                claim_id,
                review_id,
                target_type,
                action,
                original_code,
                original_description,
                proposed_code,
                proposed_description,
                reason,
                ReselectionStatus.PROPOSED,
                created_by,
            ),
        )
        (
            reselection_id, row_review_id, row_claim_id, row_target_type, row_action,
            row_original_code, row_original_description, row_proposed_code,
            row_proposed_description, row_reason, status, row_created_by,
            created_at, updated_at, agreed_by, agreed_at, corrects_reselection_id,
        ) = cur.fetchone()
        reselection = ClaimReselection(
            id=reselection_id,
            review_id=row_review_id,
            claim_id=row_claim_id,
            nosjp=nosjp,
            target_type=row_target_type,
            action=row_action,
            original_code=row_original_code,
            original_description=row_original_description,
            proposed_code=row_proposed_code,
            proposed_description=row_proposed_description,
            reason=row_reason,
            status=status,
            created_by=row_created_by,
            created_at=created_at,
            updated_at=updated_at,
            agreed_by=agreed_by,
            agreed_at=agreed_at,
            corrects_reselection_id=corrects_reselection_id,
        )

    # 4. Audit, setelah reselection berhasil dibuat.
    event = audit_service.record_event(
        conn,
        review_id=review_id,
        user_id=created_by,
        event_type=EventType.RESELECTION_CREATED,
        entity_type=EntityType.CLAIM_RESELECTION,
        entity_id=reselection.id,
        payload={
            # Nama kunci sama dengan event script lama; claim_id dan nosjp
            # datang dari audit_service. proposed_code null pada DROP.
            "reselection_id": reselection.id,
            "target_type": reselection.target_type,
            "action": reselection.action,
            "original_code": reselection.original_code,
            "proposed_code": reselection.proposed_code,
        },
    )

    return CreateReselectionResult(reselection=reselection, event=event)


@dataclass(frozen=True)
class ResolveReselectionResult:
    reselection: ClaimReselection
    event: ReviewEvent


_RESOLVE_DECISIONS = frozenset({ReselectionStatus.AGREED, ReselectionStatus.REJECTED})


def resolve_reselection(conn, *, reselection_id, resolved_by, decision):
    """Selesaikan reselection PROPOSED menjadi AGREED atau REJECTED. Tidak commit.

    - decision di-strip dan di-upper; hanya AGREED atau REJECTED.
    - User `resolved_by` harus ada dan aktif.
    - Reselection harus ada dan berstatus PROPOSED; status lain (DRAFT,
      AGREED, REJECTED, CORRECTED, CANCELLED) ditolak dengan
      InvalidStateError. Status diperiksa lebih dulu, baru review.
    - Review induk harus OPEN; review CLOSED menolak resolve (seperti script
      lama). Reselection PROPOSED tetap apa adanya setelah review ditutup.
    - AGREED mengisi agreed_by = resolved_by dan agreed_at; REJECTED
      menyimpan agreed_by/agreed_at NULL. Siapa dan kapan REJECTED tercatat
      di event (user_id, created_at).
    - Tidak ada pemeriksaan kesesuaian claim_id reselection dengan review.
    - Event RESELECTION_RESOLVED (entity claim_reselection) ditulis lewat
      audit_service di transaksi yang sama.

    Konkurensi: review_id dibaca tanpa kunci, lalu urutan kunci induk dulu:
    review FOR SHARE OF cr, kemudian reselection FOR NO KEY UPDATE OF r;
    baris klaim tidak dikunci. close_review menunggu resolve yang berjalan
    (dan sebaliknya resolve menunggu penutupan, lalu membaca CLOSED dan
    ditolak). Dua resolve pada reselection yang sama berjalan berurutan;
    yang kedua melihat status baru dan ditolak. Resolve pada reselection
    berbeda di review yang sama berjalan bersamaan.
    """
    _require_int("reselection_id", reselection_id)
    _require_int("resolved_by", resolved_by)
    decision = _choice("decision", decision, _RESOLVE_DECISIONS)

    with conn.cursor() as cur:
        # 1. User
        user_service.require_active_user(conn, resolved_by)

        # 2. Cari review induk tanpa kunci agar urutan kunci induk dulu.
        cur.execute(
            "SELECT review_id FROM review.claim_reselections WHERE id = %s",
            (reselection_id,),
        )
        found = cur.fetchone()
        if found is None:
            raise NotFoundError(f"Reselection id={reselection_id} tidak ditemukan.")
        review_id = found[0]

        # 3. Review dikunci FOR SHARE (OF cr: klaim tidak dikunci).
        cur.execute(
            """
            SELECT cr.status, c.nosjp
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
        review_status, nosjp = review

        # 4. Reselection dikunci; review_id dicocokkan dengan review yang
        # sudah dikunci agar baris tidak berpindah induk.
        cur.execute(
            f"""
            SELECT {_RESELECTION_COLUMNS}
            FROM review.claim_reselections AS r
            WHERE r.id = %s AND r.review_id = %s
            FOR NO KEY UPDATE OF r
            """,
            (reselection_id, review_id),
        )
        current = cur.fetchone()
        if current is None:
            raise NotFoundError(f"Reselection id={reselection_id} tidak ditemukan.")
        previous_status = current[10]

        # 5. Status reselection dulu, baru review (urutan script lama).
        if previous_status != ReselectionStatus.PROPOSED:
            raise InvalidStateError(
                f"Reselection id={reselection_id} tidak dapat diproses. "
                f"Status saat ini: {previous_status}. "
                "Hanya status PROPOSED yang dapat diubah menjadi AGREED atau REJECTED."
            )
        if review_status != ReviewStatus.OPEN:
            raise InvalidStateError(
                f"Review id={review_id} tidak OPEN. Status saat ini: {review_status}. "
                "Reselection hanya dapat diselesaikan ketika review masih OPEN."
            )

        # 6. Satu UPDATE dengan guard status.
        agreed = decision == ReselectionStatus.AGREED
        cur.execute(
            f"""
            UPDATE review.claim_reselections
            SET status = %s,
                agreed_by = %s,
                agreed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = %s
            RETURNING {_RESELECTION_COLUMNS}
            """,
            (
                decision,
                resolved_by if agreed else None,
                agreed,
                reselection_id,
                ReselectionStatus.PROPOSED,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise ConflictError(
                f"Reselection id={reselection_id} berubah saat diproses; ulangi."
            )
        (
            row_id, row_review_id, row_claim_id, row_target_type, row_action,
            row_original_code, row_original_description, row_proposed_code,
            row_proposed_description, row_reason, status, row_created_by,
            created_at, updated_at, agreed_by, agreed_at, corrects_reselection_id,
        ) = row
        reselection = ClaimReselection(
            id=row_id,
            review_id=row_review_id,
            claim_id=row_claim_id,
            nosjp=nosjp,
            target_type=row_target_type,
            action=row_action,
            original_code=row_original_code,
            original_description=row_original_description,
            proposed_code=row_proposed_code,
            proposed_description=row_proposed_description,
            reason=row_reason,
            status=status,
            created_by=row_created_by,
            created_at=created_at,
            updated_at=updated_at,
            agreed_by=agreed_by,
            agreed_at=agreed_at,
            corrects_reselection_id=corrects_reselection_id,
        )

    # 7. Audit, setelah UPDATE berhasil.
    event = audit_service.record_event(
        conn,
        review_id=review_id,
        user_id=resolved_by,
        event_type=EventType.RESELECTION_RESOLVED,
        entity_type=EntityType.CLAIM_RESELECTION,
        entity_id=reselection.id,
        payload={
            # Kunci sama dengan event script lama; claim_id, review_id, dan
            # nosjp datang dari audit_service.
            "reselection_id": reselection.id,
            "target_type": reselection.target_type,
            "action": reselection.action,
            "original_code": reselection.original_code,
            "proposed_code": reselection.proposed_code,
            "previous_status": previous_status,
            "new_status": reselection.status,
        },
    )

    return ResolveReselectionResult(reselection=reselection, event=event)
