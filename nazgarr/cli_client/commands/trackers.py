"""nazgarr tracker: i tracker e il loro profilo di upload (Configurazione ›
Tracker). Token API, announce URL e RSS key si chiedono a schermo."""

import json
from pathlib import Path

import typer

from nazgarr.cli_client.context import api, state
from nazgarr.cli_client.helpers import (
    counted,
    delete_preview,
    duration,
    find,
    human_duration,
    refuse_if_busy,
    secret,
    warn_insecure_url,
)
from nazgarr.cli_client.output import EXIT_USAGE, confirm, console, emit, fail, table

app = typer.Typer(help="Trackers and their upload profiles.", no_args_is_help=True)
profile_app = typer.Typer(help="The upload profile of a tracker: flags, IDs, naming, description.",
                          no_args_is_help=True)
app.add_typer(profile_app, name="profile")


def _tracker(client, ref: str) -> dict:
    """Per ID, per nome, o per la chiave del preset del suo profilo (itt)."""
    trackers = client.get("/api/trackers")
    by_preset = [t for t in trackers
                 if ((t.get("upload_profile") or {}).get("source_profile_key") or "").lower() == ref.lower()]
    if len(by_preset) == 1 and not any(t["label"].lower() == ref.lower() for t in trackers):
        return by_preset[0]
    return find(trackers, ref, "tracker")


def _render(trackers: list[dict], clients: dict[int, str]) -> None:
    table(["ID", "Tracker", "URL", "Enabled", "Client", "Language", "Seed rule", "Upload profile"], [
        (t["id"], t["label"], t["base_url"], "yes" if t["enabled"] else "no",
         clients.get(t.get("torrent_client_id"), "default") if t.get("torrent_client_id") else "default",
         t.get("language") or "",
         " / ".join(x for x in (human_duration(t.get("min_seed_time_seconds")),
                                f"ratio {t['min_ratio']}" if t.get("min_ratio") is not None else "") if x)
         + (f" ({t['seed_rule']})" if t.get("min_seed_time_seconds") and t.get("min_ratio") is not None else ""),
         (t.get("upload_profile") or {}).get("source_profile_key") or ("custom" if t.get("upload_profile") else "-"))
        for t in trackers])


@app.command("ls")
def list_trackers(ctx: typer.Context):
    """The trackers."""
    client = api(ctx)
    clients = {c["id"]: c["label"] for c in client.get("/api/torrent-clients")}
    emit(state(ctx), client.get("/api/trackers"), lambda data: _render(data, clients))


@app.command("presets")
def presets(ctx: typer.Context):
    """Known trackers: name, address and upload settings filled in for you."""
    data = api(ctx).get("/api/trackers/upload-profiles/bundled")
    emit(state(ctx), data, lambda d: table(["Preset", "Tracker", "URL"],
                                           [(p["key"], p["label"], p.get("base_url") or "") for p in d]))


def _seed_body(min_seed_time: str | None, min_ratio: float | None, seed_rule: str | None) -> dict:
    body = {}
    if min_seed_time is not None:
        body["min_seed_time_seconds"] = duration(min_seed_time) if min_seed_time else None
    if min_ratio is not None:
        body["min_ratio"] = min_ratio
    if seed_rule is not None:
        if seed_rule not in ("any", "all"):
            raise fail("--seed-rule is any or all.", EXIT_USAGE)
        body["seed_rule"] = seed_rule
    return body


@app.command("add")
def add_tracker(
    ctx: typer.Context,
    label: str = typer.Argument(None, help="A name for the tracker (from the preset if missing)."),
    preset: str = typer.Option(None, "--preset", help="A known tracker (see `nazgarr tracker presets`)."),
    url: str = typer.Option(None, "--url", help="Site address, e.g. https://tracker.example."),
    adapter_type: str = typer.Option("unit3d", "--type", help="unit3d, torznab (search and reseed only) or a plugin type."),
    with_announce: bool = typer.Option(False, "--announce", help="Ask the announce URL too (needed only to upload)."),
    client_ref: str = typer.Option(None, "--client", help="Torrent client that seeds for it (name or ID)."),
    language: str = typer.Option(None, "--language", help="Its language (ISO 639-1, e.g. it)."),
    min_seed_time: str = typer.Option(None, "--min-seed-time", help="Hit and run: minimum seed time, e.g. 7d."),
    min_ratio: float = typer.Option(None, "--min-ratio", help="Hit and run: minimum ratio, e.g. 1.0."),
    seed_rule: str = typer.Option(None, "--seed-rule", help="With both: any (default) or all."),
    profile: bool = typer.Option(True, "--profile/--no-profile", help="Create the known upload profile."),
    token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the API token from stdin."),
):
    """Add a tracker. The API token is asked (UNIT3D: your profile › Settings › API key; Torznab: the
    endpoint's API key, with --url the Torznab URL, e.g. http://prowlarr:9696/1/api)."""
    client = api(ctx)
    bundled = None
    if preset:
        bundled = find(client.get("/api/trackers/upload-profiles/bundled"), preset, "preset", key="key")
        label, url = label or bundled["label"], url or bundled.get("base_url")
        adapter_type = bundled.get("adapter_type") or adapter_type
    if not label or not url:
        raise fail("Pass a name and --url, or --preset.", EXIT_USAGE)
    body = {"label": label, "adapter_type": adapter_type, "base_url": url,
            "api_token": secret("API token", token_stdin), "language": language,
            # Senza --preset il server sceglie da sé il profilo dell'indirizzo (es. ITT).
            "upload_profile": (bundled["key"] if profile else "none") if bundled else (None if profile else "none"),
            **_seed_body(min_seed_time, min_ratio, seed_rule)}
    if with_announce:
        body["announce_url"] = secret("Announce URL (with your passkey)")
    if client_ref:
        body["torrent_client_id"] = find(client.get("/api/torrent-clients"), client_ref, "torrent client")["id"]
    created = client.post("/api/trackers", {k: v for k, v in body.items() if v is not None})
    with_profile = (created.get("upload_profile") or {}).get("source_profile_key")
    emit(state(ctx), created, lambda t: console.print(
        f"Tracker [bold]{t['label']}[/bold] (#{t['id']}) added"
        + (f", with the {with_profile} upload profile." if with_profile else ".")))
    warn_insecure_url(created, "its API token and keys")


@app.command("edit")
def edit_tracker(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Tracker name or ID."),
    label: str = typer.Option(None, "--label", help="New name."),
    url: str = typer.Option(None, "--url", help="New address."),
    enabled: bool = typer.Option(None, "--enable/--disable", help="Turn the tracker on or off."),
    new_token: bool = typer.Option(False, "--token", help="Ask a new API token."),
    new_announce: bool = typer.Option(False, "--announce", help="Ask a new announce URL."),
    new_rss_key: bool = typer.Option(False, "--rss-key", help="Ask a new RSS key."),
    client_ref: str = typer.Option(None, "--client", help="Torrent client that seeds for it ('' = default)."),
    language: str = typer.Option(None, "--language", help="Its language (ISO 639-1)."),
    min_seed_time: str = typer.Option(None, "--min-seed-time", help="Minimum seed time, e.g. 7d ('' = none)."),
    min_ratio: float = typer.Option(None, "--min-ratio", help="Minimum ratio."),
    seed_rule: str = typer.Option(None, "--seed-rule", help="any or all."),
    rate_limit: int = typer.Option(None, "--rate-limit", help="Max API calls per minute."),
):
    """Change a tracker."""
    client = api(ctx)
    found = _tracker(client, ref)
    body = {k: v for k, v in {"label": label, "base_url": url, "enabled": enabled, "language": language,
                              "rate_limit_per_min": rate_limit}.items() if v is not None}
    body |= _seed_body(min_seed_time, min_ratio, seed_rule)
    if new_token:
        body["api_token"] = secret("New API token")
    if new_announce:
        body["announce_url"] = secret("New announce URL")
    if new_rss_key:
        body["rss_key"] = secret("New RSS key")
    if client_ref is not None:
        body["torrent_client_id"] = (find(client.get("/api/torrent-clients"), client_ref, "torrent client")["id"]
                                     if client_ref else None)
    if not body:
        raise fail("Nothing to change (see --help).", EXIT_USAGE)
    updated = client.patch(f"/api/trackers/{found['id']}", body)
    emit(state(ctx), updated, lambda t: console.print(f"Tracker {t['label']} updated."))
    warn_insecure_url(updated, "its API token and keys")


@app.command("rm")
def remove_tracker(ctx: typer.Context, ref: str = typer.Argument(..., help="Tracker name or ID."),
                   yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")):
    """Remove a tracker from Nazgarr, with what it found (candidates, reviews, reseed records, searches,
    uploads to it, its upload profile). Nothing changes on the tracker, on the disks or in the clients."""
    client = api(ctx)
    found = _tracker(client, ref)
    preview = delete_preview(client, f"/api/trackers/{found['id']}/delete-preview")
    refuse_if_busy(preview, "tracker_busy", label=found["label"])
    if preview is not None:
        # Quello che se ne va con lui (decisione dell'utente, 2026-10-18).
        console.print(_preview_text(found["label"], preview))
    confirm(f"Remove tracker {found['label']}?" if preview is not None
            else f"Remove tracker {found['label']} and its upload profile?", yes)
    client.delete(f"/api/trackers/{found['id']}")
    console.print(f"Tracker {found['label']} removed.")


def _preview_text(label: str, p: dict) -> str:
    seeding = p.get("executions_seeding") or 0
    parts = [counted(p["candidates"], "candidate"), counted(p["reviews"], "review"),
             counted(p["executions"], "reseed record") + (f" ({seeding} still seeding: the torrents stay in the "
                                                          "client)" if seeding else ""),
             counted(p["match_attempts"], "search record"), counted(p["upload_targets"], "upload") + " to it"]
    if p.get("upload_profile"):
        parts.append("its upload profile")
    gone = [part for part in parts if not part.startswith("0 ")]
    return (f"Removing {label} also removes from Nazgarr: {', '.join(gone)}." if gone
            else f"Nothing else in Nazgarr depends on {label}.") + \
        "\nNothing changes on the tracker, on your disks or in your torrent clients."


# --- profilo di upload --------------------------------------------------------


def _profile_path(client, ref: str) -> tuple[dict, str]:
    tracker = _tracker(client, ref)
    return tracker, f"/api/trackers/{tracker['id']}/upload-profile"


@profile_app.command("show")
def show_profile(ctx: typer.Context, ref: str = typer.Argument(..., help="Tracker name or ID.")):
    """The upload profile: flags, IDs, naming, description template."""
    client = api(ctx)
    tracker, path = _profile_path(client, ref)
    profile = client.get(path)

    def render(p: dict) -> None:
        console.print(f"[bold]{tracker['label']}[/bold] · profile {p.get('source_profile_key') or 'custom'}"
                      + (" · naming customized" if p.get("naming_customized") else ""))
        console.print(f"Defaults: anonymous={p['default_anonymous']} personal={p['default_personal_release']} "
                      f"internal={p.get('default_internal', False)} freeleech={p.get('default_freeleech') or 0}%"
                      f" (options {p.get('freeleech_options') or []})")
        for name in ("category_id_map", "type_id_map", "resolution_id_map"):
            console.print(f"{name}: {p[name]}")
        if p.get("naming_update_available"):
            console.print(f"[yellow]Naming update available: v{p['naming_update_available']}[/yellow]")
        console.print("Description template:" + (f"\n{p['description_template']}" if p.get("description_template")
                                                 else " (default)"))

    emit(state(ctx), profile, render)


@profile_app.command("create")
def create_profile(ctx: typer.Context, ref: str = typer.Argument(..., help="Tracker name or ID."),
                   preset: str = typer.Option(None, "--preset", help="Start from a known tracker's profile.")):
    """Create the upload profile of a tracker (empty, or from a preset)."""
    client = api(ctx)
    _tracker_row, path = _profile_path(client, ref)
    created = client.post(path, {"profile_key": preset})
    emit(state(ctx), created, lambda p: console.print("Upload profile created."))


@profile_app.command("set")
def set_profile(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Tracker name or ID."),
    anonymous: bool = typer.Option(None, "--anonymous/--no-anonymous", help="Anonymous by default."),
    personal: bool = typer.Option(None, "--personal/--no-personal", help="Personal release by default."),
    internal: bool = typer.Option(None, "--internal/--no-internal", help="Internal by default."),
    freeleech: int = typer.Option(None, "--freeleech", help="Default freeleech percentage (0 = none)."),
    freeleech_options: str = typer.Option(None, "--freeleech-options", help="Offered percentages, e.g. 25,50,100."),
    category: list[str] = typer.Option(None, "--category", help="Category ID: movie=1 (repeatable)."),
    type_id: list[str] = typer.Option(None, "--type-id", help="Type ID: REMUX=2 (repeatable)."),
    resolution: list[str] = typer.Option(None, "--resolution", help="Resolution ID: 1080p=3 (repeatable)."),
):
    """Change the defaults and the IDs of the upload profile."""
    client = api(ctx)
    _tracker_row, path = _profile_path(client, ref)
    current = client.get(path)
    body: dict = {k: v for k, v in {"default_anonymous": anonymous, "default_personal_release": personal,
                                    "default_internal": internal, "default_freeleech": freeleech}.items()
                  if v is not None}
    if freeleech_options is not None:
        body["freeleech_options"] = [int(x) for x in freeleech_options.split(",") if x.strip()]
    for field, pairs in (("category_id_map", category), ("type_id_map", type_id),
                         ("resolution_id_map", resolution)):
        if pairs:
            merged = dict(current[field])
            for pair in pairs:
                key, _, value = pair.partition("=")
                if not value.strip().isdigit():
                    raise fail(f"{pair!r}: use KEY=ID, e.g. movie=1.", EXIT_USAGE)
                merged[key.strip()] = int(value)
            body[field] = merged
    if not body:
        raise fail("Nothing to change (see --help).", EXIT_USAGE)
    updated = client.patch(path, body)
    emit(state(ctx), updated, lambda p: console.print("Upload profile updated."))


@profile_app.command("template")
def template(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Tracker name or ID."),
    set_from: Path = typer.Option(None, "--set", help="Read the template from this file ('-' = stdin)."),
    edit: bool = typer.Option(False, "--edit", help="Open it in $EDITOR."),
    reset: bool = typer.Option(False, "--reset", help="Back to the default description."),
):
    """Show, set or edit the description template (BBCode with Jinja blocks, run in a sandbox)."""
    client = api(ctx)
    _tracker_row, path = _profile_path(client, ref)
    current = client.get(path).get("description_template") or ""
    if reset:
        new = ""
    elif edit:
        new = typer.edit(current, extension=".j2")
        if new is None:
            console.print("Not changed.")
            return
    elif set_from is not None:
        import sys

        new = sys.stdin.read() if str(set_from) == "-" else set_from.read_text()
    else:
        typer.echo(current or "(default template)")
        return
    client.patch(path, {"description_template": new})
    console.print("Description template saved." if new else "Back to the default description.")


@profile_app.command("naming")
def naming(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Tracker name or ID."),
    set_from: Path = typer.Option(None, "--set", help="Naming rules from a JSON file ('-' = stdin)."),
    update: bool = typer.Option(False, "--update", help="Take the newer naming rules of the preset."),
    preview: bool = typer.Option(False, "--preview", help="Names these rules give for some example releases."),
):
    """Show the naming rules, set them from JSON, update them, or preview them."""
    client = api(ctx)
    _tracker_row, path = _profile_path(client, ref)
    if update:
        result = client.post(f"{path}/naming/update")
        emit(state(ctx), result, lambda r: console.print("Naming rules updated."))
        return
    if set_from is not None:
        import sys

        rules = _rules_from_json(sys.stdin.read() if str(set_from) == "-" else set_from.read_text())
        # Prima l'anteprima del server, che controlla la forma delle regole
        # (audit 2026-10-18): un file scritto a mano con un tipo sbagliato si
        # ferma qui, con il campo che non va, prima di salvare niente.
        result = client.post(f"{path}/naming/preview", {"naming_rules": rules})
        client.patch(path, {"naming_rules": rules})

        def saved(preview: dict) -> None:
            _examples(preview)
            console.print("Naming rules saved (marked as customized).")
        emit(state(ctx), result, saved)  # con --json: i nomi che danno le regole salvate
        return
    rules = client.get(path).get("naming_rules") or {}
    if preview:
        result = client.post(f"{path}/naming/preview", {"naming_rules": rules})
        emit(state(ctx), result, _examples)
        return
    typer.echo(json.dumps(rules, indent=2, ensure_ascii=False))


def _rules_from_json(text: str) -> dict:
    try:
        rules = json.loads(text)
    except ValueError as exc:
        raise fail(f"The naming rules are not valid JSON: {exc}.", EXIT_USAGE) from exc
    if not isinstance(rules, dict):
        raise fail("The naming rules must be a JSON object.", EXIT_USAGE)
    return rules


def _examples(preview: dict) -> None:
    table(["Example", "Name"], [(e.get("label"), e.get("name")) for e in preview.get("examples") or []])


@profile_app.command("rm")
def remove_profile(ctx: typer.Context, ref: str = typer.Argument(..., help="Tracker name or ID."),
                   yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")):
    """Remove the upload profile (the tracker stays, only uploads to it stop)."""
    client = api(ctx)
    tracker, path = _profile_path(client, ref)
    confirm(f"Remove the upload profile of {tracker['label']}?", yes)
    client.delete(path)
    console.print("Upload profile removed.")
