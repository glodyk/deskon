"""Use case catatan pendahuluan klaim (review.preliminary_notes).

Saat ini berisi `import_preliminary_notes`. Catatan bersifat klaim-level dan
hanya bertambah (append-only); tidak terhubung ke review atau cycle.

Fungsi menerima koneksi dari caller, tidak commit/rollback, tidak print,
mengembalikan dataclass, dan melempar exception dari deskon.errors.
"""

from dataclasses import dataclass

from deskon.errors import NotFoundError, ValidationError
from deskon.services import user_service


@dataclass(frozen=True)
class PreliminaryNote:
    note_id: int
    claim_id: int
    nosjp: str


@dataclass(frozen=True)
class ImportPreliminaryNotesResult:
    created_by: int
    rows_total: int
    created: int
    notes: tuple[PreliminaryNote, ...]


def _require_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} harus integer, bukan {value!r}")


def _clean_rows(notes):
    """Validasi seluruh baris; kembalikan list (nosjp, note) yang sudah di-strip."""
    rows = []
    for row_number, row in enumerate(notes, start=1):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValidationError(
                f"Baris {row_number}: harus berupa pasangan (nosjp, note), bukan {row!r}"
            )
        nosjp, note = row
        if not isinstance(nosjp, str) or not nosjp.strip():
            raise ValidationError(f"Baris {row_number}: Nosjp tidak boleh kosong.")
        if not isinstance(note, str) or not note.strip():
            raise ValidationError(f"Baris {row_number}: note tidak boleh kosong.")
        rows.append((nosjp.strip(), note.strip()))
    return rows


def import_preliminary_notes(conn, *, created_by, notes):
    """Tambahkan catatan pendahuluan klaim untuk beberapa Nosjp. Tidak commit.

    `notes` adalah list/tuple pasangan (nosjp, note). Nosjp dan note di-strip
    dan tidak boleh kosong. Batch kosong valid dan tidak menulis apa pun.

    Urutan: bentuk argumen, created_by, user ada dan aktif, seluruh baris,
    resolusi seluruh Nosjp ke claim_id, baru INSERT satu baris per input.
    Tidak ada INSERT sebelum seluruh input dan seluruh klaim valid. Jika
    beberapa Nosjp tidak ditemukan, yang pertama menurut urutan input yang
    dilaporkan.

    Satu-satunya prasyarat klaim adalah Nosjp ada: tanpa syarat status klaim
    atau review, tanpa lock, tanpa event, dan tanpa deteksi duplikat (note
    yang sama boleh ditambahkan lagi, termasuk saat batch diulang).
    Transaksi dipegang caller.
    """
    if not isinstance(notes, (list, tuple)):
        raise ValidationError(f"notes harus list atau tuple, bukan {type(notes).__name__}")
    _require_int("created_by", created_by)

    with conn.cursor() as cur:
        user_service.require_active_user(conn, created_by)

        rows = _clean_rows(notes)

        # Fase resolusi: seluruh Nosjp sebelum INSERT pertama.
        claim_ids = {}
        for row_number, (nosjp, _note) in enumerate(rows, start=1):
            if nosjp in claim_ids:
                continue
            cur.execute("SELECT id FROM core.claims WHERE nosjp = %s", (nosjp,))
            claim = cur.fetchone()
            if claim is None:
                raise NotFoundError(
                    f"Baris {row_number}: Claim dengan Nosjp={nosjp} tidak ditemukan."
                )
            claim_ids[nosjp] = claim[0]

        # Fase persistence.
        created = []
        for nosjp, note in rows:
            cur.execute(
                """
                INSERT INTO review.preliminary_notes (claim_id, note, created_by)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (claim_ids[nosjp], note, created_by),
            )
            created.append(
                PreliminaryNote(note_id=cur.fetchone()[0], claim_id=claim_ids[nosjp], nosjp=nosjp)
            )

    return ImportPreliminaryNotesResult(
        created_by=created_by,
        rows_total=len(rows),
        created=len(created),
        notes=tuple(created),
    )
