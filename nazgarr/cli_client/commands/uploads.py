"""nazgarr upload: il flusso di upload nel terminale (docs/SPEC.md §9).

Come nella web UI: identificazione, primo punto di approvazione (il match
TMDB), analisi, secondo punto (la decisione per ogni tracker), poi la coda.
`upload new` crea il job e lo accompagna; `upload continue` riprende un job
da dove si trova. Ogni approvazione chiede conferma; con --yes si accettano
i valori proposti, ma un problema (ID mancanti, reseed non verificato, pack
misto non confermato) ferma comunque."""

import time
from datetime import datetime

import typer
from rich.status import Status

from nazgarr.cli_client.context import api, state
from nazgarr.cli_client.helpers import find
from nazgarr.cli_client.output import (
    EXIT_DECLINED,
    EXIT_ERROR,
    EXIT_USAGE,
    confirm,
    console,
    emit,
    err_console,
    fail,
    size,
    table,
)

app = typer.Typer(help="Uploads: start one, follow it, list, cancel, resume.", no_args_is_help=True)

POLL_SECONDS = 1.0
FINAL = ("done", "partial", "failed", "cancelled")
WORKING = ("identifying", "analyzing", "queued", "running")


def _title(job: dict) -> str:
    return f"{job.get('title') or job.get('pack_name') or job['relative_path']}" + (
        f" ({job['year']})" if job.get("year") else "")


def _wait(client, job_id: int, until: tuple[str, ...]) -> dict:
    """Aspetta che il job esca dagli stati di lavoro (o arrivi a uno di until)."""
    job = client.get(f"/api/uploads/{job_id}")
    with Status("", console=console) as status:
        while job["status"] in WORKING and job["status"] not in until:
            stage = job.get("stage") or job["status"]
            done, total = job.get("progress_done"), job.get("progress_total")
            status.update(f"{job['status']}: {stage}" + (f" {done}/{total}" if total else ""))
            time.sleep(POLL_SECONDS)
            job = client.get(f"/api/uploads/{job_id}")
    return job


# --- primo punto: il match ------------------------------------------------------


def _match(client, job: dict, yes: bool, tmdb: str | None) -> dict:
    candidates = job.get("candidates") or []
    layout = job.get("layout") or {}
    console.print(f"[bold]{_title(job)}[/bold] · detected {job.get('kind') or '?'}"
                  + (f", seasons {job['seasons']}" if job.get("seasons") else ""))
    chosen = None
    if tmdb:
        kind, _, number = tmdb.partition("/")
        chosen = next((c for c in candidates if c["content_type"] == kind and str(c["tmdb_id"]) == number), None) or {
            "content_type": kind, "tmdb_id": int(number), "title": tmdb}
    elif candidates:
        table(["#", "Title", "Year", "Type", "TMDB", "Confidence"], [
            (i, c.get("title"), c.get("year") or "", c["content_type"], c["tmdb_id"],
             f"{(c.get('confidence') or 0) * 100:.0f}%" + (" (ambiguous)" if c.get("ambiguous") else ""))
            for i, c in enumerate(candidates, start=1)])
        if yes:
            if candidates[0].get("ambiguous"):
                raise fail("The best match is ambiguous: choose it with --tmdb movie/ID or tv/ID.", EXIT_DECLINED)
            chosen = candidates[0]
        else:
            pick = typer.prompt("Which one? (number, or movie/ID tv/ID)", default="1")
            if "/" in pick:
                kind, _, number = pick.partition("/")
                chosen = {"content_type": kind, "tmdb_id": int(number)}
            else:
                chosen = candidates[int(pick) - 1]
    else:
        if yes:
            raise fail("No TMDB candidate found: pass --tmdb movie/ID or tv/ID.", EXIT_DECLINED)
        pick = typer.prompt("No candidate found. TMDB id (movie/ID or tv/ID)")
        kind, _, number = pick.partition("/")
        chosen = {"content_type": kind, "tmdb_id": int(number)}
    content_type = chosen["content_type"]
    kind = "movie" if content_type == "movie" else (job.get("kind") if job.get("kind") != "movie" else "season_pack")
    body = {"content_type": content_type, "tmdb_id": chosen["tmdb_id"], "kind": kind,
            "seasons": job.get("seasons") or sorted(int(s) for s in (layout.get("episodes_by_season") or {})),
            "episode": job.get("episode")}
    if content_type == "tv":
        # L'ordinamento degli episodi che combacia meglio con i file, come nella
        # web UI: stagioni ed episodio nella sua numerazione.
        orders = client.get(f"/api/uploads/{job['id']}/episode-orders", tmdb_id=chosen["tmdb_id"])
        order = orders.get("recommended")
        if order:
            labels = {o["key"]: o["label"] for o in orders.get("orders") or []}
            found = (orders.get("found") or {}).get(order) or {}
            if found:
                body["seasons"] = sorted(int(s) for s in found)[: None if kind == "complete_pack" else 1]
                if kind == "episode":
                    body["episode"] = found[str(body["seasons"][0])][0]
            body["episode_order"] = order
            console.print(f"Episode ordering: {labels.get(order, order)}")
            warning = orders.get("warning")
            if warning:
                console.print(f"[yellow]The files do not follow {labels.get(warning['tvdb'])} (Sonarr's order): "
                              f"{labels.get(warning['order'])} fits them better and is used. "
                              "Change it in the web UI if needed.[/yellow]")
    console.print(f"Match: {chosen.get('title') or ''} ({content_type}/{chosen['tmdb_id']}), {kind}")
    return client.post(f"/api/uploads/{job['id']}/match", body)


# --- secondo punto: la decisione -------------------------------------------------


def _draft(target: dict) -> dict:
    """Come initialDraft della web UI (frontend/src/lib/upload.ts)."""
    identical = [d for d in target.get("dupes") or [] if d.get("verdict") == "identical"]
    action = target.get("suggested_action") if target.get("suggested_action") in ("reseed", "skip") else "upload"
    defaults = target.get("client_defaults") or {}
    flags = target.get("flags") or {}
    return {
        "target_id": target["id"], "action": action, "name": target.get("proposed_name") or "",
        "category_id": target.get("category_id"), "type_id": target.get("type_id"),
        "resolution_id": target.get("resolution_id"),
        "flags": {k: v for k, v in flags.items()},
        "reseed_torrent_id": target.get("reseed_torrent_id") or (identical[0]["torrent_id_remote"] if identical
                                                                 else None),
        "client_category": target.get("client_category") or defaults.get("category"),
        "client_tags": target.get("client_tags") or (defaults.get("tags_upload") if action == "upload"
                                                     else defaults.get("tags_reseed") if action == "reseed" else ""),
    }


def _verified(target: dict, torrent_id: str | None) -> bool:
    dupe = next((d for d in target.get("dupes") or [] if d.get("torrent_id_remote") == torrent_id), None)
    return bool(dupe and (dupe.get("verification") or {}).get("status") == "passed")


def _problem(target: dict, draft: dict) -> str | None:
    if draft["action"] == "upload":
        if not draft["name"].strip():
            return "no name"
        missing = [k for k in ("category_id", "type_id", "resolution_id") if draft.get(k) is None]
        if missing:
            return f"missing {', '.join(missing)} (set them in the tracker's upload profile, or here)"
    if draft["action"] == "reseed":
        if not draft["reseed_torrent_id"]:
            return "no torrent to reseed"
        if not _verified(target, draft["reseed_torrent_id"]):
            return "the reseed needs a passed full hash check"
    return None


def _render_target(target: dict, draft: dict) -> None:
    dupes = target.get("dupes") or []
    identical = sum(1 for d in dupes if d.get("verdict") == "identical")
    console.print(f"\n[bold]{target['tracker_label']}[/bold] → [bold]{draft['action']}[/bold]"
                  + (f" (suggested: {target['suggested_action']})" if target.get("suggested_action") else ""))
    if draft["action"] == "upload":
        console.print(f"  Name: {draft['name']}")
        console.print(f"  IDs: category={draft['category_id']} type={draft['type_id']} "
                      f"resolution={draft['resolution_id']} · flags: "
                      + ", ".join(f"{k}={v}" for k, v in draft["flags"].items() if v))
    if draft["action"] == "reseed":
        console.print(f"  Reseed torrent {draft['reseed_torrent_id']}"
                      + (" (verified)" if _verified(target, draft["reseed_torrent_id"]) else " (not verified)"))
    if dupes:
        console.print(f"  On the tracker already: {len(dupes)} release(s), {identical} identical")
        for d in dupes[:5]:
            console.print(f"    · {d.get('verdict')}: {d.get('name')} ({d.get('torrent_id_remote')})")
    if target.get("error_message"):
        err_console.print(f"  [yellow]{target['error_message']}[/yellow]")


def _choose_id(target: dict, draft: dict, field: str, map_name: str) -> None:
    options = target.get(map_name) or {}
    if not options:
        draft[field] = typer.prompt(f"  {field}", type=int)
        return
    labels = ", ".join(f"{k}={v}" for k, v in options.items())
    value = typer.prompt(f"  {field} ({labels})", default=str(draft.get(field) or ""))
    draft[field] = options.get(value, int(value) if value.isdigit() else None)


def _verify(client, job_id: int, target: dict, torrent_id: str) -> dict:
    console.print(f"  Full hash check against torrent {torrent_id}…")
    client.post(f"/api/uploads/{job_id}/targets/{target['id']}/verify", {"torrent_id_remote": torrent_id})
    with Status("checking every piece", console=console):
        while True:
            time.sleep(POLL_SECONDS)
            job = client.get(f"/api/uploads/{job_id}")
            fresh = next(t for t in job["targets"] if t["id"] == target["id"])
            dupe = next((d for d in fresh.get("dupes") or [] if d.get("torrent_id_remote") == torrent_id), {})
            status = (dupe.get("verification") or {}).get("status")
            if status in ("passed", "failed", "error"):
                console.print(f"  Check {status}.")
                return fresh


def _parse_at(value: str | None) -> str | None:
    """--at "2026-10-10 21:00": l'ora locale di questo computer, in ISO."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise fail("--at wants a date and time, e.g. \"2026-10-10 21:00\".", EXIT_USAGE) from exc
    return (when if when.tzinfo else when.astimezone()).isoformat()


def _decide(client, job: dict, yes: bool, confirm_mixed: bool, at: str | None = None) -> dict:
    analysis = job.get("analysis") or {}
    mixed = analysis.get("pack_mixed")
    if mixed and not (job.get("overrides") or {}).get("pack_mixed_confirmed"):
        console.print("[yellow]The episodes come from different releases:[/yellow] "
                      + "; ".join(f"{k}: {', '.join(v)}" for k, v in mixed.items()))
        if not (confirm_mixed or (not yes and typer.confirm("Upload it as a mixed pack anyway?", default=False))):
            raise fail("A mixed pack is uploaded only after a confirmation (--confirm-mixed).", EXIT_DECLINED)
        client.put(f"/api/uploads/{job['id']}/overrides",
                   {"overrides": {**(job.get("overrides") or {}), "pack_mixed_confirmed": True}})
        job = _wait(client, job["id"], ("awaiting_decision",))
    for target_id, seeding in (analysis.get("seeding_here") or {}).items():
        err_console.print(f"[yellow]Tracker target {target_id} already seeds these files:[/yellow] "
                          f"{seeding.get('name')} on {seeding.get('client')}")
    decisions = []
    for target in job["targets"]:
        draft = _draft(target)
        _render_target(target, draft)
        if not yes:
            action = typer.prompt("  Action", default=draft["action"],
                                  type=typer.Choice(["upload", "reseed", "skip"]))
            if action != draft["action"]:
                draft = {**draft, "action": action}
            if action == "upload":
                draft["name"] = typer.prompt("  Name", default=draft["name"])
                for field, map_name in (("category_id", "category_id_map"), ("type_id", "type_id_map"),
                                        ("resolution_id", "resolution_id_map")):
                    if draft.get(field) is None:
                        _choose_id(target, draft, field, map_name)
            if action == "reseed":
                identical = [d for d in target.get("dupes") or [] if d.get("verdict") in ("identical", "same_slot")]
                if not draft["reseed_torrent_id"] and identical:
                    draft["reseed_torrent_id"] = typer.prompt(
                        "  Torrent to reseed", default=identical[0]["torrent_id_remote"])
                if draft["reseed_torrent_id"] and not _verified(target, draft["reseed_torrent_id"]):
                    target = _verify(client, job["id"], target, draft["reseed_torrent_id"])
        problem = _problem(target, draft)
        if problem:
            raise fail(f"{target['tracker_label']}: {problem}.", EXIT_USAGE)
        decisions.append(draft)
    console.print()
    table(["Tracker", "Action", "What"], [
        (t["tracker_label"], d["action"],
         d["name"] if d["action"] == "upload" else d.get("reseed_torrent_id") or "") for t, d in zip(
            job["targets"], decisions, strict=True)])
    confirm("Approve? Uploads, hardlinks and torrents added to the clients follow on their own"
            + (f", starting {at}." if at else "."), yes)
    return client.post(f"/api/uploads/{job['id']}/approve", {"targets": decisions, "scheduled_at": at})


# --- il percorso intero -----------------------------------------------------------


def drive(ctx: typer.Context, job_id: int, yes: bool, tmdb: str | None, confirm_mixed: bool, wait: bool,
          at: str | None = None) -> dict:
    client = api(ctx)
    job = _wait(client, job_id, ())
    if job["status"] == "awaiting_match":
        job = _match(client, job, yes, tmdb)
        job = _wait(client, job_id, ())
    if job["status"] == "awaiting_decision":
        job = _decide(client, job, yes, confirm_mixed, _parse_at(at))
        if job.get("scheduled_at"):
            console.print(f"Upload #{job_id} scheduled for {job['scheduled_at']}.")
            return job  # niente attesa: parte da solo a quell'ora
        console.print(f"Upload #{job_id} queued.")
    if wait and job["status"] not in FINAL:
        job = _wait(client, job_id, ())
    if job["status"] == "failed":
        raise fail(f"Upload #{job_id} failed: {job.get('error_message') or 'see nazgarr upload show'}", EXIT_ERROR)
    if job["status"] in FINAL:
        for t in job["targets"]:
            console.print(f"  {t['tracker_label']}: {t.get('action') or '-'} {t['status']}"
                          + (f" {t['remote_url']}" if t.get("remote_url") else "")
                          + (f" ({t['error_message']})" if t.get("error_message") else ""))
    return job


@app.command("new")
def new_upload(
    ctx: typer.Context,
    disk: str = typer.Argument(..., help="Disk name or ID."),
    paths: list[str] = typer.Argument(..., help="File or folder, relative to the disk (more files with --pack)."),
    pack: bool = typer.Option(False, "--pack", help="The files make one season or complete pack."),
    tracker: list[str] = typer.Option(None, "--tracker", "-t", help="Only these trackers (repeatable)."),
    tmdb: str = typer.Option(None, "--tmdb", help="Force the content: movie/ID or tv/ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the proposed values without asking."),
    confirm_mixed: bool = typer.Option(False, "--confirm-mixed", help="Allow a pack of different releases."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Follow it until it is done."),
    at: str = typer.Option(None, "--at", help="Start at this local time instead of right away (\"2026-10-10 21:00\")."),
):
    """Start an upload and walk it through match, decision and approval."""
    client = api(ctx)
    found = find(client.get("/api/disks"), disk, "disk")
    body: dict = {"disk_id": found["id"]}
    if pack:
        body["files"] = paths
    elif len(paths) == 1:
        body["relative_path"] = paths[0]
    else:
        raise fail("One file or folder, or several files with --pack.", EXIT_USAGE)
    if tracker:
        upload_trackers = client.get("/api/uploads/trackers")
        body["tracker_ids"] = [find(upload_trackers, t, "upload tracker")["id"] for t in tracker]
    if tmdb:
        body["forced_ids"] = {"tmdb": tmdb}
    job = client.post("/api/uploads", body)
    console.print(f"Upload #{job['id']} started: {_title(job)}")
    job = drive(ctx, job["id"], yes, tmdb, confirm_mixed, wait, at)
    if state(ctx).json:
        emit(state(ctx), job, lambda _j: None)


@app.command("continue")
def continue_upload(
    ctx: typer.Context,
    job_id: int = typer.Argument(..., help="The upload ID."),
    tmdb: str = typer.Option(None, "--tmdb", help="Choose the content: movie/ID or tv/ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the proposed values without asking."),
    confirm_mixed: bool = typer.Option(False, "--confirm-mixed", help="Allow a pack of different releases."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Follow it until it is done."),
    at: str = typer.Option(None, "--at", help="Start at this local time instead of right away (\"2026-10-10 21:00\")."),
):
    """Pick an upload up where it is (match or decision waiting for you)."""
    drive(ctx, job_id, yes, tmdb, confirm_mixed, wait, at)


@app.command("schedule")
def schedule_upload(
    ctx: typer.Context,
    job_id: int = typer.Argument(..., help="The upload ID, queued."),
    at: str = typer.Option(None, "--at", help="The new local start time (\"2026-10-10 21:00\")."),
    now: bool = typer.Option(False, "--now", help="Start it as soon as it is its turn."),
):
    """Change when a queued upload starts, or start it now."""
    if bool(at) == now:
        raise fail("Either --at or --now.", EXIT_USAGE)
    job = api(ctx).post(f"/api/uploads/{job_id}/schedule", {"scheduled_at": None if now else _parse_at(at)})
    when = job.get("scheduled_at")
    console.print(f"Upload #{job_id}: " + (f"starts {when}." if when else "starts now."))


@app.command("ls")
def list_uploads(ctx: typer.Context, history: bool = typer.Option(False, "--history", help="Finished ones.")):
    """Uploads in progress (or the history)."""
    jobs = [j for j in api(ctx).get("/api/uploads") if (j["status"] in FINAL) == history]
    emit(state(ctx), jobs, lambda data: table(
        ["ID", "Title", "Kind", "Status", "Trackers", "Finished" if history else "Stage"],
        [(j["id"], _title(j), j.get("kind") or "", j["status"],
          ", ".join(f"{t['tracker_label']}:{t.get('action') or t['status']}" for t in j["targets"]),
          (j.get("finished_at") or "") if history else (j.get("stage") or "")) for j in data]))


@app.command("show")
def show_upload(ctx: typer.Context, job_id: int = typer.Argument(..., help="The upload ID.")):
    """One upload: source, match, targets and its events."""
    job = api(ctx).get(f"/api/uploads/{job_id}")

    def render(j: dict) -> None:
        console.print(f"[bold]#{j['id']} {_title(j)}[/bold] · {j['status']} · {j.get('kind') or ''}")
        console.print(f"Source: {j.get('pack_name') or j['relative_path']}"
                      + (f" ({size((j.get('analysis') or {}).get('total_size_bytes'))})" if j.get("analysis") else ""))
        for t in j["targets"]:
            _render_target(t, _draft(t))
        console.print("\nEvents:")
        for e in (j.get("events") or [])[-15:]:
            console.print(f"  {e.get('created_at', '')} {e.get('level', '')} {e.get('code')} {e.get('params') or ''}")

    emit(state(ctx), job, render)


@app.command("cancel")
def cancel_upload(ctx: typer.Context, job_id: int = typer.Argument(..., help="The upload ID."),
                  yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")):
    """Cancel an upload (one already sending to a tracker finishes that tracker first)."""
    confirm(f"Cancel upload #{job_id}?", yes)
    job = api(ctx).post(f"/api/uploads/{job_id}/cancel")
    emit(state(ctx), job, lambda j: console.print(f"Upload #{j['id']}: {j['status']}."))


@app.command("resume")
def resume_upload(ctx: typer.Context, job_id: int = typer.Argument(..., help="The upload ID.")):
    """Resume a cancelled upload where it stopped (a tracker already done is never uploaded again)."""
    job = api(ctx).post(f"/api/uploads/{job_id}/resume")
    emit(state(ctx), job, lambda j: console.print(f"Upload #{j['id']}: {j['status']}."))


@app.command("rm")
def remove_upload(ctx: typer.Context, job_id: int = typer.Argument(..., help="The upload ID."),
                  yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")):
    """Remove an upload from the history (nothing changes on trackers, clients or disk)."""
    confirm(f"Remove upload #{job_id} from the history?", yes)
    api(ctx).delete(f"/api/uploads/{job_id}")
    console.print(f"Upload #{job_id} removed.")
