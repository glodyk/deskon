"""Konfigurasi DESKON dari environment variable (opsional lewat file .env).

Tidak ada password di kode. Urutan prioritas:
1. environment variable yang sudah di-set;
2. file .env (default: <root repo>/.env, atau path di DESKON_ENV_FILE);
3. default non-rahasia di bawah.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from deskon.errors import ConfigurationError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

DEFAULTS = {
    "DESKON_DB_HOST": "localhost",
    "DESKON_DB_PORT": "5432",
    "DESKON_DB_NAME": "deskon_db",
    "DESKON_DB_USER": "deskon_app",
}


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    # None: libpq memakai ~/.pgpass, PGPASSWORD, atau metode auth lain.
    password: str | None = field(default=None, repr=False)

    def connect_kwargs(self):
        kwargs = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
        }
        if self.password is not None:
            kwargs["password"] = self.password
        return kwargs


def read_env_file(path):
    """Baca file .env sederhana (KEY=VALUE, '#' komentar). File tidak ada -> {}."""
    path = Path(path)
    if not path.is_file():
        return {}

    values = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigurationError(f"{path}:{lineno}: baris tidak valid, harus KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(environ=None, env_file=None):
    """Gabungkan default, file .env, dan environment (environment menang)."""
    environ = os.environ if environ is None else environ
    if env_file is None:
        env_file = environ.get("DESKON_ENV_FILE") or DEFAULT_ENV_FILE

    settings = dict(DEFAULTS)
    settings.update(read_env_file(env_file))
    settings.update({k: v for k, v in environ.items() if k.startswith("DESKON_")})
    return settings


def load_database_config(environ=None, env_file=None):
    settings = load_settings(environ, env_file)

    port_raw = settings.get("DESKON_DB_PORT", "")
    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigurationError(f"DESKON_DB_PORT harus angka, bukan {port_raw!r}") from None

    for key in ("DESKON_DB_HOST", "DESKON_DB_NAME", "DESKON_DB_USER"):
        if not settings.get(key):
            raise ConfigurationError(f"{key} wajib diisi")

    return DatabaseConfig(
        host=settings["DESKON_DB_HOST"],
        port=port,
        dbname=settings["DESKON_DB_NAME"],
        user=settings["DESKON_DB_USER"],
        password=settings.get("DESKON_DB_PASSWORD") or None,
    )
