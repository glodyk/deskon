"""Audit trail review: satu-satunya penulis review.review_events.

Prinsip:
- perubahan state bisnis review -> event di review.review_events;
- ingestion/infrastruktur (import batch, promote) -> bukan review_events;
- event ditulis lewat koneksi caller, setelah operasi bisnis berhasil,
  sehingga ikut rollback bersama transaksinya;
- event_data = metadata identitas + payload dari use case, dibangun dengan
  json.dumps. Metadata identitas (claim_id, nosjp, review_id) diambil dari
  review yang dirujuk dan tidak boleh ditimpa payload. Modul ini tidak
  menambah business payload sendiri dan tidak mengetahui detail
  finding/reselection/review cycle;
- WRITE hanya menerima nama kanonik; nama legacy ditolak.
  Data historis tidak pernah di-rewrite; nama legacy hanya
  diterjemahkan saat READ (list_review_events).
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from deskon.constants import (
    ACTIVE_EVENT_TYPES,
    EVENT_DATA_BASE_KEYS,
    EVENT_ENTITY_TYPE,
    RESERVED_EVENT_TYPES,
    canonical_entity_type,
    canonical_event_type,
)
from deskon.errors import AuditEventError, NotFoundError


@dataclass(frozen=True)
class ReviewEvent:
    id: int
    review_id: int
    user_id: int
    # Nilai seperti tersimpan di database (bisa nama legacy).
    event_type: str
    entity_type: str | None
    entity_id: int | None
    event_data: dict | None
    created_at: datetime

    @property
    def canonical_event_type(self):
        return canonical_event_type(self.event_type)

    @property
    def canonical_entity_type(self):
        return canonical_entity_type(self.entity_type)


_COLUMNS = (
    "id, review_id, user_id, event_type, entity_type, entity_id, event_data, created_at"
)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} tidak bisa diserialisasi ke event_data")


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditEventError(f"{name} harus integer, bukan {value!r}")


def build_event_data(claim_id, nosjp, review_id, payload=None):
    """event_data = metadata identitas + payload use case, tanpa tambahan lain.

    Payload tidak boleh menimpa metadata identitas.
    """
    payload = dict(payload or {})
    overlap = sorted(set(payload) & set(EVENT_DATA_BASE_KEYS))
    if overlap:
        raise AuditEventError(
            f"payload tidak boleh berisi field dasar event_data: {', '.join(overlap)}"
        )
    data = {"claim_id": claim_id, "nosjp": nosjp, "review_id": review_id}
    data.update(payload)
    return data


def record_event(
    conn,
    *,
    review_id,
    user_id,
    event_type,
    entity_type,
    entity_id,
    payload=None,
):
    """Tulis satu event review lewat koneksi caller. Tidak commit.

    claim_id dan nosjp dibaca dari review yang dirujuk, sehingga
    event_data selalu konsisten dengan review_id.
    """
    if event_type in RESERVED_EVENT_TYPES:
        raise AuditEventError(
            f"event_type {event_type} masih cadangan dan belum boleh dipancarkan"
        )
    if event_type not in ACTIVE_EVENT_TYPES:
        raise AuditEventError(f"event_type tidak dikenal: {event_type!r}")

    expected_entity = EVENT_ENTITY_TYPE[event_type]
    if entity_type != expected_entity:
        raise AuditEventError(
            f"entity_type untuk {event_type} harus {expected_entity!r}, bukan {entity_type!r}"
        )

    _require_int("review_id", review_id)
    _require_int("user_id", user_id)
    _require_int("entity_id", entity_id)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.nosjp
            FROM review.claim_reviews AS cr
            JOIN core.claims AS c ON c.id = cr.claim_id
            WHERE cr.id = %s
            """,
            (review_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(f"review {review_id} tidak ditemukan")
        claim_id, nosjp = row

        event_data = build_event_data(claim_id, nosjp, review_id, payload)
        try:
            event_json = json.dumps(event_data, ensure_ascii=False, default=_json_default)
        except (TypeError, ValueError) as exc:
            raise AuditEventError(f"event_data tidak valid: {exc}") from exc

        cur.execute(
            f"""
            INSERT INTO review.review_events (
                review_id, user_id, event_type, entity_type, entity_id, event_data
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING {_COLUMNS}
            """,
            (review_id, user_id, event_type, entity_type, entity_id, event_json),
        )
        return ReviewEvent(*cur.fetchone())


def list_review_events(conn, review_id):
    """Semua event untuk satu review, urut kronologis (termasuk event legacy)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS}
            FROM review.review_events
            WHERE review_id = %s
            ORDER BY created_at, id
            """,
            (review_id,),
        )
        return [ReviewEvent(*row) for row in cur.fetchall()]
