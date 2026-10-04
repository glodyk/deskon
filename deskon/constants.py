"""Single source of truth untuk nilai-nilai kanonik DESKON.

Nilai status/tipe di sini mencerminkan CHECK constraint pada
database/schema (001_baseline.sql, 002_add_review_cycle.sql).
Kosakata audit event mengikuti kontrak audit tahap pertama.
"""


# ---------------------------------------------------------------------------
# Audit: event_type
# ---------------------------------------------------------------------------

class EventType:
    # Aktif: boleh dipancarkan oleh service.
    REVIEW_OPENED = "REVIEW_OPENED"
    REVIEW_CLOSED = "REVIEW_CLOSED"
    FINDING_CREATED = "FINDING_CREATED"
    RESELECTION_CREATED = "RESELECTION_CREATED"
    # AGREED / REJECTED dicatat di event_data["new_status"], bukan event terpisah.
    RESELECTION_RESOLVED = "RESELECTION_RESOLVED"

    # Cadangan: kosakata disepakati, belum boleh dipancarkan
    # sampai use case-nya benar-benar ada.
    FINDING_PUBLISHED = "FINDING_PUBLISHED"
    FINDING_CORRECTED = "FINDING_CORRECTED"
    FINDING_CANCELLED = "FINDING_CANCELLED"
    COMMENT_CREATED = "COMMENT_CREATED"
    COMMENT_PUBLISHED = "COMMENT_PUBLISHED"
    COMMENT_CORRECTED = "COMMENT_CORRECTED"
    RESELECTION_CORRECTED = "RESELECTION_CORRECTED"
    RESELECTION_CANCELLED = "RESELECTION_CANCELLED"


ACTIVE_EVENT_TYPES = frozenset({
    EventType.REVIEW_OPENED,
    EventType.REVIEW_CLOSED,
    EventType.FINDING_CREATED,
    EventType.RESELECTION_CREATED,
    EventType.RESELECTION_RESOLVED,
})

RESERVED_EVENT_TYPES = frozenset({
    EventType.FINDING_PUBLISHED,
    EventType.FINDING_CORRECTED,
    EventType.FINDING_CANCELLED,
    EventType.COMMENT_CREATED,
    EventType.COMMENT_PUBLISHED,
    EventType.COMMENT_CORRECTED,
    EventType.RESELECTION_CORRECTED,
    EventType.RESELECTION_CANCELLED,
})

ALL_EVENT_TYPES = ACTIVE_EVENT_TYPES | RESERVED_EVENT_TYPES


# ---------------------------------------------------------------------------
# Audit: entity_type
# ---------------------------------------------------------------------------

class EntityType:
    CLAIM_REVIEW = "claim_review"
    REVIEW_FINDING = "review_finding"
    REVIEW_COMMENT = "review_comment"
    CLAIM_RESELECTION = "claim_reselection"
    # Kanonik, tetapi belum punya event di review.review_events
    # (note bisa ada sebelum review, sedangkan review_events.review_id NOT NULL).
    PRELIMINARY_NOTE = "preliminary_note"


ALL_ENTITY_TYPES = frozenset({
    EntityType.CLAIM_REVIEW,
    EntityType.REVIEW_FINDING,
    EntityType.REVIEW_COMMENT,
    EntityType.CLAIM_RESELECTION,
    EntityType.PRELIMINARY_NOTE,
})

# entity_type yang wajib dipakai untuk setiap event_type.
EVENT_ENTITY_TYPE = {
    EventType.REVIEW_OPENED: EntityType.CLAIM_REVIEW,
    EventType.REVIEW_CLOSED: EntityType.CLAIM_REVIEW,
    EventType.FINDING_CREATED: EntityType.REVIEW_FINDING,
    EventType.FINDING_PUBLISHED: EntityType.REVIEW_FINDING,
    EventType.FINDING_CORRECTED: EntityType.REVIEW_FINDING,
    EventType.FINDING_CANCELLED: EntityType.REVIEW_FINDING,
    EventType.COMMENT_CREATED: EntityType.REVIEW_COMMENT,
    EventType.COMMENT_PUBLISHED: EntityType.REVIEW_COMMENT,
    EventType.COMMENT_CORRECTED: EntityType.REVIEW_COMMENT,
    EventType.RESELECTION_CREATED: EntityType.CLAIM_RESELECTION,
    EventType.RESELECTION_RESOLVED: EntityType.CLAIM_RESELECTION,
    EventType.RESELECTION_CORRECTED: EntityType.CLAIM_RESELECTION,
    EventType.RESELECTION_CANCELLED: EntityType.CLAIM_RESELECTION,
}

# Field minimum di setiap event_data baru.
EVENT_DATA_BASE_KEYS = ("claim_id", "nosjp", "review_id")


# ---------------------------------------------------------------------------
# Audit: alias legacy (hanya untuk query history; data lama tidak di-rewrite)
# ---------------------------------------------------------------------------

LEGACY_EVENT_TYPE_ALIASES = {
    "REVIEW_CYCLE_CREATED": EventType.REVIEW_OPENED,
}

LEGACY_ENTITY_TYPE_ALIASES = {
    "REVIEW_FINDING": EntityType.REVIEW_FINDING,
}


def canonical_event_type(value):
    """Nama kanonik untuk event_type yang tersimpan (legacy atau baru)."""
    return LEGACY_EVENT_TYPE_ALIASES.get(value, value)


def canonical_entity_type(value):
    """Nama kanonik untuk entity_type yang tersimpan (legacy atau baru)."""
    return LEGACY_ENTITY_TYPE_ALIASES.get(value, value)


# ---------------------------------------------------------------------------
# core.claims.claim_status dan review.claim_reviews.review_type
# ---------------------------------------------------------------------------

class ClaimStatus:
    PENDING = "PENDING"
    VERIFIKASI_PASCA_KLAIM = "VERIFIKASI_PASCA_KLAIM"
    AUDIT_ADMINISTRASI_KLAIM = "AUDIT_ADMINISTRASI_KLAIM"


CLAIM_STATUSES = frozenset({
    ClaimStatus.PENDING,
    ClaimStatus.VERIFIKASI_PASCA_KLAIM,
    ClaimStatus.AUDIT_ADMINISTRASI_KLAIM,
})

# review_type = tujuan historis sebuah cycle, ditetapkan saat cycle dibuka.
# Nilainya sama dengan claim_status, tetapi tidak mengikuti perubahan
# core.claims.claim_status setelah cycle dibuat.
ReviewType = ClaimStatus
REVIEW_TYPES = CLAIM_STATUSES


# ---------------------------------------------------------------------------
# review.claim_reviews
# ---------------------------------------------------------------------------

class ReviewStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


REVIEW_STATUSES = frozenset({ReviewStatus.OPEN, ReviewStatus.CLOSED})


class FinalDecision:
    LAYAK = "LAYAK"
    TIDAK_LAYAK = "TIDAK_LAYAK"
    RESELEKSI = "RESELEKSI"


FINAL_DECISIONS = frozenset({
    FinalDecision.LAYAK,
    FinalDecision.TIDAK_LAYAK,
    FinalDecision.RESELEKSI,
})


# ---------------------------------------------------------------------------
# review.review_findings dan review.review_comments
# ---------------------------------------------------------------------------

class FindingStatus:
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CORRECTED = "CORRECTED"
    CANCELLED = "CANCELLED"


FINDING_STATUSES = frozenset({
    FindingStatus.DRAFT,
    FindingStatus.PUBLISHED,
    FindingStatus.CORRECTED,
    FindingStatus.CANCELLED,
})

CommentStatus = FindingStatus
COMMENT_STATUSES = FINDING_STATUSES


# ---------------------------------------------------------------------------
# review.claim_reselections
# ---------------------------------------------------------------------------

class ReselectionStatus:
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    AGREED = "AGREED"
    REJECTED = "REJECTED"
    CORRECTED = "CORRECTED"
    CANCELLED = "CANCELLED"


RESELECTION_STATUSES = frozenset({
    ReselectionStatus.DRAFT,
    ReselectionStatus.PROPOSED,
    ReselectionStatus.AGREED,
    ReselectionStatus.REJECTED,
    ReselectionStatus.CORRECTED,
    ReselectionStatus.CANCELLED,
})


class ReselectionAction:
    CHANGE = "CHANGE"
    DROP = "DROP"


RESELECTION_ACTIONS = frozenset({ReselectionAction.CHANGE, ReselectionAction.DROP})


class ReselectionTargetType:
    DIAGNOSIS = "DIAGNOSIS"
    PROCEDURE = "PROCEDURE"


RESELECTION_TARGET_TYPES = frozenset({
    ReselectionTargetType.DIAGNOSIS,
    ReselectionTargetType.PROCEDURE,
})


# ---------------------------------------------------------------------------
# core.users
# ---------------------------------------------------------------------------

class UserRole:
    BPJS_VERIFIER = "BPJS_VERIFIER"
    HOSPITAL_USER = "HOSPITAL_USER"
    ADMIN = "ADMIN"


USER_ROLES = frozenset({UserRole.BPJS_VERIFIER, UserRole.HOSPITAL_USER, UserRole.ADMIN})


# ---------------------------------------------------------------------------
# core.claim_topups
# ---------------------------------------------------------------------------

TOPUP_TYPES = frozenset({"SA", "SD", "SI", "SP", "SR"})


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------

class ImportBatchStatus:
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    TRANSFORMING = "TRANSFORMING"
    IMPORTED = "IMPORTED"
    FAILED = "FAILED"


IMPORT_BATCH_STATUSES = frozenset({
    ImportBatchStatus.UPLOADED,
    ImportBatchStatus.VALIDATING,
    ImportBatchStatus.VALIDATED,
    ImportBatchStatus.TRANSFORMING,
    ImportBatchStatus.IMPORTED,
    ImportBatchStatus.FAILED,
})

IMPORT_SOURCE_TYPES = frozenset({"CSV", "TXT", "XLSX", "OTHER"})


class ValidationStatus:
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"


VALIDATION_STATUSES = frozenset({
    ValidationStatus.PENDING,
    ValidationStatus.VALID,
    ValidationStatus.INVALID,
})
