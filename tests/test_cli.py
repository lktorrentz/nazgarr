import os
import stat
import tomllib
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

from nazgarr import cli

ROOT = Path(__file__).resolve().parent.parent


def test_init_writes_the_config_and_a_private_valid_secret_key(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    (tmp_path / "disks").mkdir()
    config = tmp_path / "cfg" / "config.yaml"

    assert cli.main(["init", "--scan-root", str(tmp_path / "disks"), "--config", str(config),
                     "--data-dir", str(tmp_path / "data")]) == 0

    assert yaml.safe_load(config.read_text()) == {
        "disk_scan_root": str((tmp_path / "disks").resolve()), "data_dir": str(tmp_path / "data"),
    }
    key_file = config.parent / cli.SECRET_FILE
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    Fernet(key_file.read_text().strip().encode())  # una chiave che nazgarr/core/crypto.py accetta
    first_key = key_file.read_text()

    # Rilanciato: la chiave non cambia mai (le credenziali nel DB dipendono da lei).
    cli.main(["init", "--scan-root", str(tmp_path / "disks"), "--config", str(config), "--force"])
    assert key_file.read_text() == first_key


def test_init_wants_an_existing_scan_root(tmp_path):
    assert cli.main(["init", "--scan-root", str(tmp_path / "missing"), "--config", str(tmp_path / "c.yaml")]) == 2


def test_serve_needs_a_config_and_a_key_and_runs_one_process(tmp_path, monkeypatch):
    # serve scrive CONFIG_PATH e APP_SECRET_KEY nell'ambiente del processo:
    # una copia, se no la chiave finta arriverebbe ai test dopo questo.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    config = tmp_path / "config.yaml"
    assert cli.main(["serve", "--config", str(config)]) == 2
    config.write_text("disk_scan_root: /mnt\ndata_dir: /tmp/x\n")
    assert cli.main(["serve", "--config", str(config)]) == 2  # nessuna chiave

    (tmp_path / cli.SECRET_FILE).write_text("k\n")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert cli.main(["serve", "--config", str(config), "--port", "9000"]) == 0
    assert calls == [(("nazgarr.main:app",), {"host": "0.0.0.0", "port": 9000, "workers": 1})]


def test_the_service_files_start_the_installed_command():
    unit = cli.systemd_unit(Path("/home/u/.config/nazgarr/config.yaml"), "0.0.0.0", 8080)
    assert '-m nazgarr.cli serve --config "/home/u/.config/nazgarr/config.yaml" --host 0.0.0.0 --port 8080' in unit
    assert "Restart=on-failure" in unit and "UMask=0077" in unit
    plist = cli.launchd_plist(Path("/Users/u/c.yaml"), "127.0.0.1", 8080, Path("/Users/u/Logs"))
    assert "<string>serve</string>" in plist and "<key>KeepAlive</key>" in plist


def test_the_package_needs_the_same_libraries_as_the_lock():
    # Il pacchetto pipx e il container non devono divergere: stesse
    # dipendenze dirette di requirements.in (supervisor serve solo nel container).
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wanted = [
        line.strip() for line in (ROOT / "requirements.in").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert project["dependencies"] == [dep for dep in wanted if dep != "supervisor"]


def _account_db(tmp_path):
    """Un config.yaml e un DB con l'account admin, come dopo il setup."""
    from nazgarr.core import db, migrations, settings_repo
    from nazgarr.web import auth

    config = tmp_path / "config.yaml"
    config.write_text(f"data_dir: {tmp_path / 'data'}\n")
    engine = db.make_engine(str(tmp_path / "data" / "nazgarr.db"))
    migrations.upgrade(engine)
    session = db.make_session_factory(engine)()
    return config, session, settings_repo, auth


def test_reset_password_sets_a_new_one_and_logs_every_session_out(tmp_path, monkeypatch):
    import io

    config, session, settings_repo, auth = _account_db(tmp_path)
    settings_repo.set_setting(session, "auth_username", "admin")
    settings_repo.set_setting(session, "auth_password_hash", auth.hash_password("forgotten-one"))
    version_before = auth.token_version(session)

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["reset-password", "--config", str(config), "--password-stdin"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("a-new-password\n"))
    assert cli.main(["reset-password", "--config", str(config), "--password-stdin"]) == 0

    session.expire_all()
    assert settings_repo.get_setting(session, "auth_username") == "admin"
    assert auth.verify_password("a-new-password", settings_repo.get_setting(session, "auth_password_hash"))
    assert auth.token_version(session) != version_before  # i browser già entrati devono rifare il login

    monkeypatch.setattr("sys.stdin", io.StringIO("another-password\n"))
    assert cli.main(["reset-password", "--config", str(config), "--password-stdin", "--username", "luca"]) == 0
    session.expire_all()
    assert settings_repo.get_setting(session, "auth_username") == "luca"


def test_reset_password_without_an_account_points_to_the_setup_code(tmp_path, monkeypatch, capsys):
    config, _session, _repo, _auth = _account_db(tmp_path)
    asked = []
    monkeypatch.setattr("typer.prompt", lambda *a, **k: asked.append(a) or "never-used")

    assert cli.main(["reset-password", "--config", str(config)]) == 2

    assert asked == []  # niente password chiesta per niente
    assert "setup code" in capsys.readouterr().err
