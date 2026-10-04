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
    ├── audit_service.py    satu-satunya penulis review.review_events
    ├── finding_service.py  use case finding review (create_review_finding)
    └── review_service.py   use case lifecycle review (open_review_cycle, close_review)
database/
├── schema/                 migration berurutan (001, 002, ...)
└── seeds/
tests/
└── regression/             regression test use case terhadap behavior script lama
```

Use case (`open_review_cycle`, `close_review`, `create_finding`, ...) dan CLI
ditambahkan bertahap; setiap use case disertai regression test terhadap
behavior script lama.

## Use case

| Use case | Menggantikan | Event |
|---|---|---|
| `review_service.open_review_cycle(conn, nosjp=, opened_by=, review_type=None)` | `create_new_review_cycle_v1.py` | `REVIEW_OPENED` |
| `finding_service.create_review_finding(conn, review_id=, finding_category=, finding_title=, finding_description=, created_by=)` | `create_review_finding_v1.py` | `FINDING_CREATED` |
| `review_service.close_review(conn, review_id=, closed_by=, final_decision=, resolution_note=)` | `close_review_v1.py` | `REVIEW_CLOSED` |

`open_review_cycle`: user harus ada dan aktif; klaim dicari lewat Nosjp. Jika
klaim sudah punya review OPEN, review itu dikembalikan (`created=False`, tanpa
event). Jika tidak, review OPEN baru dibuat dengan `cycle_no` = cycle terakhir
+ 1 dan `review_type` = parameter eksplisit atau `core.claims.claim_status` saat
ini. Baris klaim dikunci (`FOR NO KEY UPDATE`) supaya pembukaan cycle untuk satu
klaim berjalan berurutan; constraint unik menjadi pengaman terakhir
(`ConflictError`).

`create_review_finding`: category, title, dan description di-strip dan tidak
boleh kosong; user harus ada dan aktif; review harus ada dan OPEN (review
CLOSED ditolak). Finding disimpan DRAFT dengan `created_by`. Event
`FINDING_CREATED` memakai entity `review_finding` dan `event_data` berisi
identitas review + `finding_id`, `finding_category`, `status` (judul dan
deskripsi tidak ikut). Baris review dikunci `FOR SHARE`, sehingga penutupan
review dan pembuatan finding pada review yang sama berjalan berurutan,
sedangkan beberapa finding tetap bisa dibuat bersamaan.

`close_review`: `final_decision` di-strip dan di-upper, harus `LAYAK`,
`TIDAK_LAYAK`, atau `RESELEKSI`; `resolution_note` di-strip dan wajib diisi;
keduanya divalidasi sebelum database disentuh. User harus ada dan aktif; review
harus ada dan OPEN (review CLOSED tidak pernah diubah). Review menjadi CLOSED
dengan `final_decision`, `resolution_note`, `closed_by`, `closed_at`; finding,
reselection, komentar, dan klaim tidak disentuh. Event `REVIEW_CLOSED` memakai
entity `claim_review` dan `event_data` berisi identitas review +
`final_decision`, `resolution_note` (kunci sama dengan script lama). Baris
review dikunci `FOR NO KEY UPDATE OF cr`: berkonflik dengan `FOR SHARE` milik
`create_review_finding`, dengan penutupan lain, dan dengan `open_review_cycle`,
tetapi tidak menahan FK check dan tidak mengunci baris klaim. Klaim yang perlu
direview lagi mendapat cycle baru lewat `open_review_cycle`.

## Migration

| File | Status |
|---|---|
| `001_baseline.sql` | Locked. Schema aktual (pg_dump), source of truth awal. |
| `002_add_review_cycle.sql` | Locked. `cycle_no` dan `review_type` di `review.claim_reviews`. Belum dijalankan ke DEV. |

File migration yang sudah locked tidak diubah; perubahan schema berikutnya
menjadi file baru. File migration tidak berisi `BEGIN/COMMIT`; runner yang
memegang transaksi:

```
database/schema/*.sql -> migration runner -> transaction -> database
```

**Migration runner aplikasi/CLI masih TODO.** Fixture di `tests/conftest.py`
hanya memuat schema ke database tes sementara; itu bukan migration runner
aplikasi dan tidak boleh dipakai untuk DEV/produksi.

**PostgreSQL 14.** `001_baseline.sql` berasal dari pg_dump PostgreSQL 14.
001 + 002 dan seluruh tes sudah dijalankan di cluster sementara PostgreSQL
14.24 dan 16. Menjalankan 002 ke DEV tetap langkah terpisah yang menunggu
keputusan owner.

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
- `event_data` = metadata identitas (`claim_id`, `nosjp`, `review_id`) +
  payload dari use case, dibangun dengan `json.dumps`. Metadata identitas
  diambil dari review yang dirujuk dan tidak boleh ditimpa payload.
  `audit_service` tidak menambah business payload sendiri, sehingga tetap
  generik dan tidak mengetahui detail finding/reselection/review cycle.
- Event ditulis lewat koneksi caller setelah operasi berhasil, sehingga ikut
  rollback bersama transaksinya.
- Nama legacy hanya berlaku saat **READ** (alias untuk query history).
  **WRITE** hanya menerima nama kanonik; `audit_service` menolak nama legacy.
  Data historis tidak di-rewrite.

  | Arah | Legacy | Kanonik |
  |---|---|---|
  | READ | event `REVIEW_CYCLE_CREATED` | `REVIEW_OPENED` |
  | READ | entity `REVIEW_FINDING` | `review_finding` |
  | WRITE | nama legacy apa pun | ditolak (`AuditEventError`) |

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

Seluruh tes (termasuk memuat 001 + 002) lulus di PostgreSQL 16 dan
PostgreSQL 14.24:

```
DESKON_TEST_PG_BIN=/path/ke/postgresql-14/bin pytest
```
