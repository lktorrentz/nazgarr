"""L'albero dei comandi di `nazgarr` (Typer): quelli del server (init,
serve, install-service, reset-password, version, nazgarr/cli.py) e quelli
client, che parlano con un'istanza in esecuzione attraverso le API
(docs/CLI.md)."""

import sys
from types import SimpleNamespace

import click
import typer

from nazgarr.cli_client.commands import (
    auth,
    browse,
    clients,
    config,
    disks,
    instances,
    raw,
    reviews,
    runs,
    settings,
    status,
    trackers,
    uploads,
)
from nazgarr.cli_client.http import ApiError
from nazgarr.cli_client.output import EXIT_DECLINED, EXIT_ERROR, EXIT_UNAUTHORIZED, EXIT_USAGE, State, err_console

HELP = """Nazgarr from the command line.

Server commands (init, serve, install-service, reset-password) run Nazgarr on
this machine.
Every other command talks to a running instance through its API: log in once
with `nazgarr login --url http://HOST:3019`, then use it like the web UI.

Full guide: https://github.com/lktorrentz/nazgarr/blob/main/docs/CLI.md
"""


def build() -> typer.Typer:
    from nazgarr import cli as server

    app = typer.Typer(help=HELP, no_args_is_help=True, add_completion=True, pretty_exceptions_enable=False,
                      rich_markup_mode="rich", context_settings={"help_option_names": ["-h", "--help"]})

    @app.callback()
    def root(
        ctx: typer.Context,
        profile: str = typer.Option(None, "--profile", "-P", envvar="NAZGARR_PROFILE",
                                    help="Saved instance to use (see `nazgarr profile ls`)."),
        url: str = typer.Option(None, "--url", envvar="NAZGARR_URL",
                                help="Address of the instance, overriding the profile."),
        json_output: bool = typer.Option(False, "--json", help="Print the raw JSON answer (for scripts and jq)."),
    ):
        ctx.obj = State(profile=profile, url=url, json=json_output)

    # --- server ---------------------------------------------------------------

    @app.command(rich_help_panel="Server")
    def init(
        scan_root: str = typer.Option(..., "--scan-root", help="The folder your disks are under (e.g. /mnt)."),
        data_dir: str = typer.Option(None, "--data-dir", help="Where to keep the database and caches."),
        config: str = typer.Option(None, "--config", help="Path of config.yaml."),
        force: bool = typer.Option(False, "--force", help="Rewrite config.yaml if it exists."),
    ):
        """Write the configuration and the secret key, check the tools."""
        return server.cmd_init(SimpleNamespace(scan_root=scan_root, data_dir=data_dir, config=config, force=force))

    @app.command(rich_help_panel="Server")
    def serve(
        config: str = typer.Option(None, "--config", help="Path of config.yaml."),
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(3019, "--port"),
    ):
        """Start the server (always one process)."""
        return server.cmd_serve(SimpleNamespace(config=config, host=host, port=port))

    @app.command("install-service", rich_help_panel="Server")
    def install_service(
        config: str = typer.Option(None, "--config", help="Path of config.yaml."),
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(3019, "--port"),
        print_only: bool = typer.Option(False, "--print", help="Print the file instead of writing it."),
    ):
        """Write the systemd (Linux) or launchd (macOS) service file."""
        return server.cmd_install_service(SimpleNamespace(config=config, host=host, port=port, print=print_only))

    @app.command("reset-password", rich_help_panel="Server")
    def reset_password(
        config: str = typer.Option(None, "--config", help="Path of config.yaml (default: the one in use)."),
        username: str = typer.Option(None, "--username", help="Change the username too (default: keep it)."),
        password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the new password from stdin."),
    ):
        """Set a new password for the account (forgotten password). Run it where Nazgarr runs."""
        def ask() -> str:
            if password_stdin:
                return sys.stdin.readline().rstrip("\n")
            return typer.prompt("New password", hide_input=True, confirmation_prompt=True)

        return server.cmd_reset_password(SimpleNamespace(config=config, username=username, ask=ask))

    @app.command(rich_help_panel="Server")
    def version():
        """The installed version."""
        return server.cmd_version(SimpleNamespace())

    # --- client ---------------------------------------------------------------

    app.command(rich_help_panel="Connection")(auth.setup)
    app.command(rich_help_panel="Connection")(auth.login)
    app.command(rich_help_panel="Connection")(auth.logout)
    app.add_typer(auth.app, name="profile", rich_help_panel="Connection")
    app.command(rich_help_panel="Overview")(status.status)
    app.add_typer(disks.app, name="disk", rich_help_panel="Configuration")
    app.add_typer(clients.app, name="client", rich_help_panel="Configuration")
    app.add_typer(trackers.app, name="tracker", rich_help_panel="Configuration")
    app.add_typer(settings.arr_app, name="arr", rich_help_panel="Configuration")
    app.add_typer(settings.app, name="settings", rich_help_panel="Configuration")
    app.add_typer(settings.schedule_app, name="schedule", rich_help_panel="Configuration")
    app.add_typer(config.app, name="config", rich_help_panel="Configuration")
    app.add_typer(instances.app, name="instance", rich_help_panel="Configuration")
    app.command(rich_help_panel="Reseeding")(runs.scan)
    app.add_typer(runs.app, name="runs", rich_help_panel="Reseeding")
    app.add_typer(reviews.app, name="review", rich_help_panel="Reseeding")
    app.add_typer(uploads.app, name="upload", rich_help_panel="Uploads")
    app.add_typer(browse.library_app, name="library", rich_help_panel="Library")
    app.add_typer(browse.triage_app, name="triage", rich_help_panel="Library")
    app.command("logs", rich_help_panel="Advanced")(browse.logs)
    app.command("api", rich_help_panel="Advanced")(raw.raw)
    return app


def run(argv: list[str] | None) -> int:
    """Il comando, con il codice di uscita (main() di nazgarr/cli.py)."""
    command = typer.main.get_command(build())
    try:
        result = command.main(args=argv, prog_name="nazgarr", standalone_mode=False)
    except ApiError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        return EXIT_UNAUTHORIZED if exc.status in (401, 403) else EXIT_ERROR
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (click.exceptions.Abort, typer.Abort):
        err_console.print("Aborted.")
        return EXIT_DECLINED
    except click.ClickException as exc:
        exc.show()
        return EXIT_USAGE if isinstance(exc, click.UsageError) else exc.exit_code
    return result if isinstance(result, int) else 0
