"""Il comando `nazgarr`, per l'installazione senza Docker (pacchetto Python
con pipx, decisione dell'utente 2026-10-02). Fa quello che nel container
fanno entrypoint e supervisord:

    nazgarr init --scan-root /mnt     config.yaml, chiave segreta, controlli
    nazgarr serve                     il server (un solo processo, sempre)
    nazgarr install-service           il servizio systemd (Linux) o launchd (macOS)
    nazgarr reset-password            una password nuova, se l'hai dimenticata
    nazgarr version

e tutti i comandi client, che parlano con un'istanza in esecuzione
(nazgarr/cli_client, docs/CLI.md).

Senza Docker non c'è nessuna mappatura dei volumi: Nazgarr vede il
filesystem vero, e disk_scan_root è la cartella sotto cui stanno i dischi
(il confine oltre il quale non registra e non sfoglia niente)."""

import os
import platform
import shutil
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

SECRET_FILE = "secret.key"
SERVICE_NAME = "nazgarr"
LAUNCHD_LABEL = "io.github.lktorrentz.nazgarr"


def default_dirs() -> tuple[Path, Path]:
    """(config, dati) nelle posizioni standard della piattaforma."""
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        base = home / "Library" / "Application Support" / "Nazgarr"
        return base, base / "data"
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Nazgarr"
        return base, base / "data"
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "nazgarr"
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "nazgarr"
    return config, data


def default_config_path() -> Path:
    return Path(os.environ.get("NAZGARR_CONFIG") or default_dirs()[0] / "config.yaml")


def _write_private(path: Path, content: str) -> None:
    """Un file leggibile solo dal proprietario (la chiave segreta)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)


def secret_key(config_path: Path) -> str | None:
    """APP_SECRET_KEY dall'ambiente o dal file accanto alla config: cifra le
    credenziali nel DB, deve restare la stessa per sempre."""
    if os.environ.get("APP_SECRET_KEY"):
        return os.environ["APP_SECRET_KEY"]
    path = config_path.parent / SECRET_FILE
    return path.read_text().strip() if path.exists() else None


def missing_tools() -> list[str]:
    """Programmi esterni che servono: mediainfo (identità dei file, upload) e
    ffmpeg (screenshot degli upload)."""
    missing = [tool for tool in ("ffmpeg",) if shutil.which(tool) is None]
    try:
        from pymediainfo import MediaInfo

        if not MediaInfo.can_parse():
            missing.append("mediainfo")
    except Exception:
        missing.append("mediainfo")
    return missing


def cmd_init(args) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    data_dir = Path(args.data_dir) if args.data_dir else default_dirs()[1]
    scan_root = Path(args.scan_root).expanduser().resolve()
    if not scan_root.is_dir():
        print(f"{scan_root} does not exist: point --scan-root at the folder your disks are under.", file=sys.stderr)
        return 2
    if config_path.exists() and not args.force:
        print(f"{config_path} already exists (--force rewrites it). The secret key is never touched.")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump({"disk_scan_root": str(scan_root), "data_dir": str(data_dir)}))
        print(f"Configuration written to {config_path}")
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = config_path.parent / SECRET_FILE
    if not key_path.exists() and not os.environ.get("APP_SECRET_KEY"):
        # Una chiave Fernet (32 byte in base64 url-safe): cifra le credenziali (nazgarr/core/crypto.py).
        _write_private(key_path, Fernet.generate_key().decode() + "\n")
        print(f"Secret key created in {key_path}. Back it up with the database: without it the stored "
              "credentials can't be read.")
    missing = missing_tools()
    if missing:
        print(f"Missing: {', '.join(missing)}. Install them with your package manager "
              "(e.g. apt install mediainfo ffmpeg, brew install media-info ffmpeg).", file=sys.stderr)
    print("Done. Start it with `nazgarr serve`, or as a service with `nazgarr install-service`.")
    return 0


def cmd_serve(args) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    if not config_path.exists():
        print(f"No configuration at {config_path}: run `nazgarr init --scan-root <folder of your disks>` first.",
              file=sys.stderr)
        return 2
    key = secret_key(config_path)
    if not key:
        print(f"No secret key: set APP_SECRET_KEY or run `nazgarr init` again ({config_path.parent}).",
              file=sys.stderr)
        return 2
    os.environ["CONFIG_PATH"] = str(config_path)
    os.environ["APP_SECRET_KEY"] = key
    import uvicorn

    # Un solo processo: il worker degli upload, il pianificatore e la cartella
    # osservata vivono dentro, con più worker si duplicherebbero.
    uvicorn.run("nazgarr.main:app", host=args.host, port=args.port, workers=1)
    return 0


def systemd_unit(config_path: Path, host: str, port: int) -> str:
    return f"""[Unit]
Description=Nazgarr
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={sys.executable} -m nazgarr.cli serve --config "{config_path}" --host {host} --port {port}
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
"""


def launchd_plist(config_path: Path, host: str, port: int, log_dir: Path) -> str:
    args = [sys.executable, "-m", "nazgarr.cli", "serve", "--config", str(config_path),
            "--host", host, "--port", str(port)]
    items = "\n".join(f"    <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_dir / "nazgarr.log"}</string>
  <key>StandardErrorPath</key>
  <string>{log_dir / "nazgarr.log"}</string>
</dict>
</plist>
"""


def cmd_install_service(args) -> int:
    """Scrive il file del servizio per l'utente corrente (che deve poter
    leggere e creare hardlink nei dischi) e dice come attivarlo: niente
    comandi di sistema lanciati da qui."""
    config_path = Path(args.config) if args.config else default_config_path()
    if not config_path.exists():
        print("Run `nazgarr init` first.", file=sys.stderr)
        return 2
    system = platform.system()
    if system == "Linux":
        content = systemd_unit(config_path, args.host, args.port)
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "nazgarr.service"
        steps = [
            "systemctl --user daemon-reload",
            "systemctl --user enable --now nazgarr",
            "sudo loginctl enable-linger $USER   # start at boot, even without logging in",
        ]
    elif system == "Darwin":
        log_dir = Path.home() / "Library" / "Logs" / "Nazgarr"
        log_dir.mkdir(parents=True, exist_ok=True)
        content = launchd_plist(config_path, args.host, args.port, log_dir)
        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        steps = [f"launchctl load -w {path}"]
    else:
        print("Services are written for Linux (systemd) and macOS (launchd). On Windows use WinSW or NSSM "
              f"with: {sys.executable} -m nazgarr.cli serve --config \"{config_path}\"", file=sys.stderr)
        return 2
    if args.print:
        print(content)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"Service written to {path}. To start it:")
    for step in steps:
        print(f"  {step}")
    return 0


def _as_db_owner(db_path: str) -> None:
    """`docker exec` gira da root: il DB e i suoi -wal/-shm devono restare
    dell'utente dell'app (PUID), o il server non potrebbe più scriverci."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        owner = os.stat(db_path)
        if owner.st_uid != 0:
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)


def cmd_reset_password(args) -> int:
    """Password dimenticata: una nuova direttamente nel database, sulla
    macchina dove gira Nazgarr (chi può lanciarlo ha già accesso ai suoi
    file, non è una porta in più). Chiude tutte le sessioni aperte, come
    cambiarla dalle impostazioni. args.ask: chiede la password (a schermo o
    da stdin), solo dopo aver trovato l'account."""
    config_path = Path(args.config or os.environ.get("CONFIG_PATH") or default_config_path())
    if not config_path.is_file():
        print(f"No configuration at {config_path}: pass --config.", file=sys.stderr)
        return 2
    from nazgarr.core.config import load_settings

    db_path = load_settings(str(config_path)).db_path
    if not Path(db_path).is_file():
        print(f"No database at {db_path}.", file=sys.stderr)
        return 2
    _as_db_owner(db_path)
    from nazgarr.core import db, settings_repo
    from nazgarr.web import auth

    engine = db.make_engine(db_path)
    try:
        with db.make_session_factory(engine)() as session:
            username = settings_repo.get_setting(session, "auth_username")
            if not auth.is_auth_configured(session):
                print("No account yet: open Nazgarr and create it with the setup code printed in its log.",
                      file=sys.stderr)
                return 2
            username = (args.username or "").strip() or username
            password = args.ask()
            if len(password) < 8:
                print("The password needs at least 8 characters. Nothing changed.", file=sys.stderr)
                return 2
            settings_repo.set_setting(session, "auth_username", username)
            settings_repo.set_setting(session, "auth_password_hash", auth.hash_password(password))
            auth.revoke_all_tokens(session)
    finally:
        engine.dispose()
    print(f"New password set for {username}. Every open session is logged out: log in again. "
          "API keys keep working.")
    return 0


def cmd_version(_args) -> int:
    from nazgarr.core.version import __commit__, __version__

    print(__version__ + (f" ({__commit__})" if __commit__ else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Tutti i comandi, del server e client (nazgarr/cli_client/app.py, docs/CLI.md)."""
    from nazgarr.cli_client.app import run

    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
