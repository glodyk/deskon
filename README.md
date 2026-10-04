# DESKON

Sistem administrasi dan lifecycle review klaim BPJS Kesehatan.
Bukan sistem intelligence; analisis (CLAIRE) berada di luar repository ini.

## Struktur

```
deskon/                     package Python
├── config.py               konfigurasi dari environment / .env
├── constants.py            nilai kanonik (event_type, entity_type, status, review_type)
├── errors.py               exception domain
├── db.py                   koneksi psycopg2 dan batas transaksi caller
└── services/
    └── audit_service.py    satu-satunya penulis review.review_events
database/
├── schema/                 migration berurutan (001, 002, ...)
└── seeds/
tests/
```

Use case (`open_review_cycle`, `close_review`, `create_finding`, ...) dan CLI
ditambahkan bertahap; setiap use case disertai regression test terhadap
behavior script lama.

## Migration

| File | Status |
|---|---|
| `001_baseline.sql` | Locked. Schema aktual (pg_dump), source of truth awal. |
| `002_add_review_cycle.sql` | Locked. `cycle_no` dan `review_type` di `review.claim_reviews`. Belum dijalankan ke DEV. |

File migration yang sudah locked tidak diubah; perubahan schema berikutnya
menjadi file baru. File migration tidak berisi `BEGIN/COMMIT`; runner yang
memegang transaksi.

## Kontrak service layer

- Service berbasis use case, bukan berbasis tabel.
- Service menerima koneksi dari caller, tidak `commit`/`rollback`, tidak `print`,
  tidak bergantung pada CLI/Flask, mengembalikan dataclass, dan melempar
  exception dari `deskon.errors`.
- CLI/API adalah adapter: membuka koneksi, memegang transaksi
  (`deskon.db.transaction`), dan menampilkan hasil.

## Kontrak audit (`review.review_events`)

Prinsip: perubahan state bisnis review menghasilkan event; ingestion dan
infrastruktur (import batch, promote) tidak. `staging.import_batches` sudah
menjadi jejak import.

| event_type | entity_type | status |
|---|---|---|
| `REVIEW_OPENED` | `claim_review` | aktif |
| `REVIEW_CLOSED` | `claim_review` | aktif |
| `FINDING_CREATED` | `review_finding` | aktif |
| `RESELECTION_CREATED` | `claim_reselection` | aktif |
| `RESELECTION_RESOLVED` | `claim_reselection` | aktif; `AGREED`/`REJECTED` di `event_data.new_status` |
| `FINDING_PUBLISHED`, `FINDING_CORRECTED`, `FINDING_CANCELLED` | `review_finding` | cadangan |
| `COMMENT_CREATED`, `COMMENT_PUBLISHED`, `COMMENT_CORRECTED` | `review_comment` | cadangan |
| `RESELECTION_CORRECTED`, `RESELECTION_CANCELLED` | `claim_reselection` | cadangan |

- Event cadangan ada di `constants.py` tetapi ditolak `audit_service` sampai
  use case-nya ada.
- `preliminary_note` adalah entity_type kanonik tanpa event untuk saat ini
  (note bisa ada sebelum review, sedangkan `review_events.review_id` wajib).
- Setiap `event_data` baru memuat `claim_id`, `nosjp`, `review_id` ditambah
  payload use case, dibangun dengan `json.dumps`. `claim_id` dan `nosjp`
  diambil dari review yang dirujuk.
- Event ditulis lewat koneksi caller setelah operasi berhasil, sehingga ikut
  rollback bersama transaksinya.
- Data historis tidak di-rewrite. Nama legacy dipetakan hanya saat dibaca:
  `REVIEW_CYCLE_CREATED` -> `REVIEW_OPENED`, entity `REVIEW_FINDING` -> `review_finding`.

## Konfigurasi

```
cp .env.example .env    # isi DESKON_DB_*; .env tidak di-commit
```

Environment variable yang sudah di-set mengalahkan isi `.env`.
`DESKON_ENV_FILE` dapat menunjuk ke file `.env` lain.

## Tes

```
pip install -r requirements-dev.txt
pytest
```

Tes database membuat cluster PostgreSQL sementara (`initdb` + `pg_ctl`) di
direktori temp, memuat `database/schema/*.sql` berurutan, lalu menghapusnya.
Tidak menyentuh database DEV. Jika binary PostgreSQL tidak ditemukan, tes
database di-skip; set `DESKON_TEST_PG_BIN` (mis. `/usr/lib/postgresql/14/bin`)
bila perlu. `initdb` tidak bisa dijalankan sebagai root.
