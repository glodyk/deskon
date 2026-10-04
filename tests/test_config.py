import pytest

from deskon.config import load_database_config, read_env_file
from deskon.errors import ConfigurationError


def test_defaults_without_password(tmp_path):
    cfg = load_database_config(environ={}, env_file=tmp_path / "missing.env")
    assert (cfg.host, cfg.port, cfg.dbname, cfg.user) == ("localhost", 5432, "deskon_db", "deskon_app")
    assert cfg.password is None
    assert "password" not in cfg.connect_kwargs()


def test_env_file_is_read_and_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# komentar\n"
        "DESKON_DB_HOST=db.local\n"
        "export DESKON_DB_PORT=6543\n"
        'DESKON_DB_PASSWORD="rahasia"\n',
        encoding="utf-8",
    )
    cfg = load_database_config(environ={"DESKON_DB_HOST": "env.local"}, env_file=env_file)
    assert cfg.host == "env.local"
    assert cfg.port == 6543
    assert cfg.password == "rahasia"


def test_password_not_in_repr(tmp_path):
    cfg = load_database_config(environ={"DESKON_DB_PASSWORD": "rahasia"}, env_file=tmp_path / "x")
    assert "rahasia" not in repr(cfg)


def test_invalid_port(tmp_path):
    with pytest.raises(ConfigurationError):
        load_database_config(environ={"DESKON_DB_PORT": "abc"}, env_file=tmp_path / "x")


def test_invalid_env_line(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("BUKAN_KEY_VALUE\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        read_env_file(env_file)
