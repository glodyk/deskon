"""Exception DESKON.

Service melempar exception dari modul ini; adapter (CLI/API) yang
menerjemahkannya menjadi pesan, exit code, atau HTTP status.
"""


class DeskonError(Exception):
    """Base untuk semua error DESKON."""


class ConfigurationError(DeskonError):
    """Konfigurasi (environment/.env) tidak lengkap atau tidak valid."""


class DomainError(DeskonError):
    """Base untuk pelanggaran aturan domain di service layer."""


class ValidationError(DomainError):
    """Input ke service tidak valid."""


class NotFoundError(DomainError):
    """Entitas yang dirujuk tidak ada."""


class InvalidStateError(DomainError):
    """Operasi tidak diizinkan pada status entitas saat ini."""


class ConflictError(DomainError):
    """Operasi bertabrakan dengan data yang sudah ada."""


class AuditEventError(ValidationError):
    """Event audit tidak sesuai kontrak (event_type, entity_type, event_data)."""
