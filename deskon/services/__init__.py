"""Use-case services.

Aturan service layer:
- menerima koneksi database dari caller;
- tidak commit/rollback (caller pemilik transaksi);
- tidak print, tidak bergantung pada CLI/web framework;
- mengembalikan dataclass dan melempar exception dari deskon.errors.
"""
