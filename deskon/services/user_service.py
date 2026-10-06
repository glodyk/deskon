"""Validasi user bersama untuk use case (core.users).

Menggantikan blok "user ada, lalu aktif" yang sebelumnya ditulis inline di
setiap use case. Perilaku, pesan, dan SQL-nya sama dengan blok itu: satu
SELECT tanpa kunci, tanpa pemeriksaan role, tanpa commit/rollback/print.

Pemeriksaan tipe id (integer murni, bukan bool) tetap di batas tiap use
case dan terjadi sebelum fungsi ini dipanggil.
"""

from deskon.errors import InvalidStateError, NotFoundError


def require_active_user(conn, user_id):
    """User `user_id` harus ada (`NotFoundError`) dan aktif (`InvalidStateError`).

    Tidak mengunci baris user dan tidak mengembalikan apa pun.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT is_active FROM core.users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    if user is None:
        raise NotFoundError(f"User id={user_id} tidak ditemukan.")
    if not user[0]:
        raise InvalidStateError(f"User id={user_id} tidak aktif.")
