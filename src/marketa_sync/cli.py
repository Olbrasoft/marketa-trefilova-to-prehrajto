"""Durable parallel uploads with independent language variants."""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import threading
import time

from sdilej_to_prehrajto.git_state import GitStatePersister
from sdilej_to_prehrajto.language import WhisperLanguageDetector
from sdilej_to_prehrajto.prehrajto import login as target_login
from sdilej_to_prehrajto.prehrajto import rename_video, uploaded_video_confirmed, PrehrajtoError
from sdilej_to_prehrajto.sdilej import login as source_login, SdilejProvider, PremiumRequiredError
from sdilej_to_prehrajto.state import StateStore
from sdilej_to_prehrajto.subtitle_backfill import Backfill, atomic_write
from sdilej_to_prehrajto.subtitle_target import SubtitleTarget, trusted_detail
from sdilej_to_prehrajto.subtitle_media import SubtitleUnavailable
from sdilej_to_prehrajto.subtitles import SubtitleQueue

from .catalog import EnglishTitles, VariantSourceStore, import_sources, named_films, read_rows
from .discovery import EnglishProvider, display_name, has_subtitles, bitmap_subtitles
from .subtitle_visual import VISUAL_POLICY
from .naming import MarketaPipeline, with_resolution, reconcile_resolution_names


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def source_session():
    return source_login(required_env("SDILEJ_EMAIL"), required_env("SDILEJ_PASSWORD"))


def target_session():
    return target_login(required_env("PREHRAJTO_EMAIL"), required_env("PREHRAJTO_PASSWORD"))


def make_pipeline(root, variant, names, source=None, target=None, provider=None, persist=None,
                  checkpoint_lock=None):
    lane = root / "variants" / variant
    return MarketaPipeline(
        source_provider=provider or SdilejProvider(source, None),
        source_session=source, target_session=target,
        state=StateStore(lane / "state/sync.json", on_persist=persist, checkpoint_lock=checkpoint_lock),
        selected_sources=VariantSourceStore(lane / "manifests/selected-sources.jsonl", variant),
        subtitle_queue=SubtitleQueue(lane / "plans/subtitle-followup.jsonl"),
        name_builder=lambda film, candidate: display_name(names[film.cr_film_id], variant, candidate),
    )


def ordered_plans(pipelines, films, limit, *, excluded=()):
    plans = []
    for variant, pipeline in pipelines.items():
        available = [film for film in films if (variant, film.cr_film_id) not in excluded]
        pending = [film for film in available if pipeline.state.pending_prepared(film.cr_film_id)]
        pending_ids = {film.cr_film_id for film in pending}
        fresh = [film for film in available if film.cr_film_id not in pending_ids]
        # Reserve one due reconciliation per lane so subtitle work does not
        # wait behind thousands of films that have never been transferred.
        rows = pipeline.build_plan(pending, 1, verified_only=True)
        rows += pipeline.build_plan(fresh, limit, verified_only=True)
        for row in rows:
            plans.append((variant, row))
    plans.sort(key=lambda item: (
        not pipelines[item[0]].state.pending_prepared(item[1]["film"]["cr_film_id"]),
        item[1]["film"].get("priority_rank") or 10**9, item[0] != "cz"))
    return plans[:limit]


def prepare(root, films, names, limit, persist, runtime_minutes):
    session = source_session()
    detector = WhisperLanguageDetector()
    detector.audio_stream = "0:a:0"
    title_catalog = EnglishTitles(root)
    search_titles = {film.cr_film_id: title_catalog.get(film.to_dict()) for film in films}
    provider = EnglishProvider(session, detector, english_titles=search_titles,
                               cache_path=root / "variants/en/state/candidate-scans.json",
                               visual_report_path=root / "variants/en/state/visual-subtitles.json",
                               discovery_timeout_seconds=900, max_candidates=None)
    pipeline = make_pipeline(root, "en", names, source=session, provider=provider)
    scan_path = root / "variants/en/state/discovery.json"
    scanned = json.loads(scan_path.read_text()) if scan_path.exists() else {}
    attempted = 0
    deadline = time.monotonic() + runtime_minutes * 60
    # Keep fresh films moving without starving resumable older scans when
    # title resolution continuously introduces more films into the catalog.
    fresh = deque(film for film in films if str(film.cr_film_id) not in scanned)
    retries = deque(sorted((film for film in films if str(film.cr_film_id) in scanned),
                           key=lambda film: scanned[str(film.cr_film_id)].get("checked_at", "")))
    ordered = []
    while fresh or retries:
        ordered.extend(fresh.popleft() for _ in range(min(4, len(fresh))))
        if retries:
            ordered.append(retries.popleft())
    visual_reviews = []
    for film in films:
        candidate = pipeline.selected_sources.candidate(film.cr_film_id)
        if (candidate and (not has_subtitles(candidate) or bitmap_subtitles(candidate))
                and candidate.match_evidence["marketa"].get("visual_policy") != VISUAL_POLICY):
            visual_reviews.append(film)
    review_ids = {film.cr_film_id for film in visual_reviews}
    ordered = visual_reviews + [film for film in ordered if film.cr_film_id not in review_ids]
    for film in ordered:
        if attempted >= limit or time.monotonic() >= deadline:
            break
        key = str(film.cr_film_id)
        existing = pipeline.selected_sources.candidate(film.cr_film_id)
        visual_review = film.cr_film_id in review_ids
        if not visual_review and (existing or pipeline.state.uploaded(film.cr_film_id)):
            continue
        now = datetime.now(timezone.utc)
        if scanned.get(key, {}).get("retry_after", "") > now.isoformat():
            continue
        attempted += 1
        provider.discovery_timeout_seconds = min(300, max(1, deadline - time.monotonic()))
        try:
            if visual_review:
                # Review the exact file already uploaded/prepared, never
                # replace its source while learning whether pixels have text.
                provider.discovery_deadline = min(deadline, time.monotonic() + provider.discovery_timeout_seconds)
                checked = provider.verify_english(film, existing)
                selected = [checked] if checked else []
            else:
                selected = provider.discover(film)
            if selected:
                candidate = selected[0]
                pipeline._record_verified_source(film, candidate, display_name(names[film.cr_film_id], "en", candidate))
                status, days = "verified", 0
            else:
                status, days = "no_verified_english_source", 7
        except PremiumRequiredError:
            raise
        except SubtitleUnavailable as error:
            status = str(error)
            days = 1 if status == "burned_in_or_external_subtitles_require_review" else 1 / 24
        except Exception as error:
            # Do not serialize request URLs, media commands, or response bodies.
            failures = scanned.get(key, {}).get("failures", 0) + 1
            status, days = type(error).__name__, min(24, 2 ** (failures - 1)) / 24
        now = datetime.now(timezone.utc)
        scanned[key] = {"status": status, "checked_at": now.isoformat(),
                        "failures": 0 if status == "verified" else scanned.get(key, {}).get("failures", 0) + 1,
                        "retry_after": (now + timedelta(days=days)).isoformat()}
        pipeline.selected_sources.compact()
        atomic_write(scan_path, json.dumps(scanned, indent=2, sort_keys=True) + "\n")
        if persist:
            persist(scan_path, "flush")
        print(f"english_film={key} status={status}", flush=True)


def followup_report(root: Path):
    lane = root / "variants/en"
    sources = VariantSourceStore(lane / "manifests/selected-sources.jsonl", "en")
    state = StateStore(lane / "state/sync.json")
    backfill_path = lane / "state/subtitle-backfill.json"
    backfill = json.loads(backfill_path.read_text()).get("videos", {}) if backfill_path.exists() else {}
    lines = ["# Czech subtitle follow-up", "", "Attach subtitles only after target processing has finished.", "",
             "| Film | Target ID | Source | Action |", "| --- | --- | --- | --- |"]
    for film_id, entry in state.data.get("films", {}).items():
        upload = entry.get("upload") or {}
        pending = entry.get("prepared") or {}
        if not upload and not pending:
            continue
        plan = entry.get("last_plan") or {}
        source = sources.get(int(film_id)) or {}
        title = upload.get("display_name") or plan.get("display_name") or film_id
        subtitle_evidence = source.get("match_evidence", {}).get("marketa", {})
        action = "Extract Czech track from exact source after processing" if subtitle_evidence.get("czech_subtitles") else "Find and add matching Czech subtitles after processing"
        video_id = upload.get("target_video_id") or pending.get("target_video_id")
        if backfill.get(str(video_id), {}).get("status") in {"attached_verified", "already_has_czech"}:
            action = "Complete: Czech subtitles verified"
        lines.append(f"| {title.replace('|', '/')} | {video_id} | {source.get('source_url', '')} | {action} |")
    atomic_write(root / "reports/subtitle-followup.md", "\n".join(lines) + "\n")


def checkpoint_paths(root: Path, mode: str) -> tuple[Path, ...]:
    # Preparation and uploads run concurrently and must never publish stale
    # copies of each other's files during a checkpoint rebase.
    if mode in {"import", "titles", "prepare"}:
        relative = ["variants/cz/manifests/selected-sources.jsonl",
                    "variants/en/manifests/selected-sources.jsonl",
                    "state/english-titles.json", "variants/en/state/discovery.json",
                    "variants/en/state/candidate-scans.json", "variants/en/state/visual-subtitles.json"]
    else:
        relative = ["variants/cz/state/sync.json", "variants/en/state/sync.json",
                    "variants/en/plans/subtitle-followup.jsonl", "reports/subtitle-followup.md",
                    "reports/subtitle-backfill.md", "variants/en/state/subtitle-backfill.json"]
    return tuple(root / path for path in relative)


def run_subtitles(root, source, target, persist, limit, runtime_minutes, checkpoint_lock=None,
                  state_store=None):
    lock = checkpoint_lock if checkpoint_lock is not None else threading.RLock()
    lane = root / "variants/en"
    store = state_store or StateStore(lane / "state/sync.json", on_persist=persist, checkpoint_lock=lock)

    def confirm_name(row, has_czech=True):
        with lock:
            film_id = row.get("source", {}).get("cr_film_id")
            if film_id is None:
                return
            entry = store.snapshot(film_id)
            upload = entry.get("upload") or {}
            prepared = entry.get("prepared") or {}
            source_evidence = row.get("source", {})
            record = upload or (prepared if source_evidence.get("visual_subtitle_status") else {})
            title = upload.get("display_name") or (entry.get("last_plan") or {}).get("display_name", "")
            if (str(record.get("target_video_id")) != row["target_video_id"] or not title
                    or re.search(r"CZ\s+Dabing$", title, re.I)):
                return
            base = re.sub(r"\s+CZ\s+Titulky$", "", title, flags=re.I)
            wanted = base + " CZ Titulky" if has_czech else base
            wanted = with_resolution(wanted, record.get("width") or source_evidence.get("width", 0),
                                     record.get("height") or source_evidence.get("height", 0))
            if wanted == title:
                row["title"] = wanted
                return
            if not uploaded_video_confirmed(target, row["target_video_id"], wanted):
                rename_video(target, row["target_video_id"], wanted)
                if not uploaded_video_confirmed(target, row["target_video_id"], wanted):
                    raise PrehrajtoError("Subtitle name change unconfirmed")
            if upload:
                store.film(film_id)["upload"]["display_name"] = wanted
            if entry.get("last_plan"):
                store.film(film_id)["last_plan"]["display_name"] = wanted
            detail = getattr(target, "_prehrajto_detail_urls", {}).get(row["target_video_id"], "")
            if trusted_detail(detail):
                row["detail_url"] = detail
                store.film(film_id)["upload" if upload else "prepared"]["detail_url"] = detail
            store.save("success")
            row["title"] = wanted

    with lock:
        backfill = Backfill(SubtitleTarget(target), source, lane,
                            lane / "state/subtitle-backfill.json", root / "reports/subtitle-backfill.md",
                            persist=persist, checkpoint_lock=lock, on_verified=confirm_name,
                            on_no_subtitles=lambda row: confirm_name(row, False))
    result = backfill.run(max_pages=2, max_sources=limit, limit=limit, runtime_minutes=runtime_minutes)
    with lock:
        followup_report(root)
        if persist:
            persist(lane / "state/subtitle-backfill.json", "flush")
    return result


def drain_queue(root, pipelines, films, *, limit, runtime_minutes, persist=None,
                continuous=False, refresh=None, subtitles=None,
                session_pairs=None, poll_interval_seconds=15,
                checkpoint_lock=None, subtitle_drain_minutes=0,
                clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + runtime_minutes * 60
    subtitle_deadline = deadline + subtitle_drain_minutes * 60
    counts = {"planned": 0, "uploaded": 0, "processing": 0, "failed": 0}
    pairs = session_pairs or [None]
    lock = checkpoint_lock if checkpoint_lock is not None else threading.RLock()
    free_slots = deque(range(len(pairs)))
    pending = {}
    reserved = set()
    next_refresh = clock()
    next_subtitles = clock()
    subtitle_future = None
    print(f"upload_workers={len(pairs)}", flush=True)

    def execute(variant, row, slot):
        pipeline = pipelines[variant]
        print(f"upload_task_started variant={variant} cr_film_id={row['film']['cr_film_id']} worker={slot + 1}", flush=True)
        if pairs[slot] is None:
            pipeline.execute([row])
        else:
            pipeline.execute([row], session_pairs=[pairs[slot]])

    # One shared pool caps transfers across both variants. Keep subtitle work
    # separate so it cannot hold an otherwise available transfer slot.
    with ThreadPoolExecutor(max_workers=len(pairs)) as transfers, ThreadPoolExecutor(max_workers=1) as maintenance:
        while True:
            for future in list(pending):
                if not future.done():
                    continue
                variant, row, slot = pending.pop(future)
                future.result()
                film_id = row["film"]["cr_film_id"]
                reserved.remove((variant, film_id))
                free_slots.append(slot)
                counts["planned"] += 1
                pipeline = pipelines[variant]
                checkpoint = pipeline.state.snapshot(film_id)
                counts["uploaded" if checkpoint.get("upload") else "processing" if checkpoint.get("prepared") else "failed"] += 1
                with lock:
                    followup_report(root)
                    if persist:
                        persist(pipeline.state.path, "flush")
                print(json.dumps(counts), flush=True)
            if subtitle_future is not None and subtitle_future.done():
                try:
                    subtitle_future.result()
                except Exception as error:
                    print(f"subtitle_followup_deferred={type(error).__name__}", flush=True)
                subtitle_future = None

            plans = []
            if clock() < deadline:
                if refresh and clock() >= next_refresh:
                    films = refresh()
                    next_refresh = clock() + poll_interval_seconds
                remaining = limit if continuous else max(0, limit - counts["planned"] - len(pending))
                capacity = min(len(free_slots), remaining)
                if capacity:
                    plans = ordered_plans(pipelines, films, capacity, excluded=reserved)
                for variant, row in plans:
                    if clock() >= deadline:
                        break
                    slot = free_slots.popleft()
                    reserved.add((variant, row["film"]["cr_film_id"]))
                    future = transfers.submit(execute, variant, row, slot)
                    pending[future] = (variant, row, slot)
            remaining_minutes = int((subtitle_deadline - clock()) / 60)
            if (subtitles and subtitle_future is None and clock() >= next_subtitles
                    and remaining_minutes > 0 and (clock() < deadline or pending)):
                # Processing can finish while every transfer slot is busy,
                # including the graceful drain after the upload deadline.
                next_subtitles = clock() + 600
                print("subtitle_followup_started interval_seconds=600", flush=True)
                subtitle_future = maintenance.submit(subtitles, min(10, remaining_minutes))

            active = [*pending, *([subtitle_future] if subtitle_future is not None else [])]
            if active:
                wait(active, timeout=poll_interval_seconds, return_when=FIRST_COMPLETED)
            elif continuous and clock() < deadline:
                print("upload_queue=empty waiting_for_preparation=true", flush=True)
                sleep(min(30, max(0, deadline - clock())))
            else:
                break
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["import", "titles", "plan", "prepare", "upload", "continuous", "subtitles"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--runtime-minutes", type=int, default=45)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("UPLOAD_WORKERS", "3")))
    parser.add_argument("--persist-git-state", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 200 or not 1 <= args.runtime_minutes <= 330 or not 1 <= args.workers <= 6:
        parser.error("limit must be 1..200; runtime-minutes must be 1..330; workers must be 1..6")
    root = args.root.resolve()
    extra = checkpoint_paths(root, args.mode)
    persist = GitStatePersister(root, extra) if args.persist_git_state else None
    if args.mode == "import":
        if args.upstream is None:
            parser.error("import requires --upstream")
        print(json.dumps(import_sources(root, args.upstream)))
        if persist:
            persist(extra[0], "flush")
        return 0
    rows = read_rows(root / "backlog/films.jsonl.gz")
    titles = EnglishTitles(root)
    if args.mode == "titles":
        print(f"resolved_titles={titles.resolve(rows, args.limit)}")
        if persist:
            persist(titles.path, "flush")
        return 0
    films, names = named_films(rows, titles)
    if args.mode == "prepare":
        prepare(root, films, names, args.limit, persist, args.runtime_minutes)
        return 0
    if args.mode == "subtitles":
        run_subtitles(root, source_session(), target_session(), persist, args.limit, args.runtime_minutes)
        return 0
    source, target = (source_session(), target_session()) if args.mode in {"upload", "continuous"} else (None, None)
    checkpoint_lock = threading.RLock()
    pipelines = {variant: make_pipeline(root, variant, names, source, target, persist=persist,
                                       checkpoint_lock=checkpoint_lock)
                 for variant in ("cz", "en")}
    for pipeline in pipelines.values():
        pipeline.state.release_claims_from_other_run(os.environ.get("GITHUB_RUN_ID"))
    if args.mode == "plan":
        plans = ordered_plans(pipelines, films, args.limit)
        for variant, row in plans:
            print(json.dumps({"variant": variant, "title": row["display_name"],
                              "source": row["selected"]["url"],
                              "width": row["selected"]["width"], "height": row["selected"]["height"]}, ensure_ascii=False))
        return 0
    # Requests sessions are private to a transfer slot; sharing cookie jars
    # across concurrent logins and uploads is not safe.
    pairs = [(source, target)]
    for _ in range(1, args.workers):
        pairs.append((source_session(), target_session()))
    def refresh():
        if persist:
            with checkpoint_lock:
                for variant, pipeline in pipelines.items():
                    payload = persist.read_remote_file(f"variants/{variant}/manifests/selected-sources.jsonl")
                    pipeline.selected_sources.merge_jsonl(payload)
                titles.rows = json.loads(persist.read_remote_file("state/english-titles.json"))
        refreshed_films, refreshed_names = named_films(rows, titles)
        names.update(refreshed_names)
        return refreshed_films

    subtitle_sessions = None
    def subtitles(remaining_minutes):
        nonlocal subtitle_sessions
        with checkpoint_lock:
            has_targets = any(entry.get("upload") or entry.get("prepared")
                              for entry in pipelines["en"].state.data["films"].values())
        if subtitle_sessions is None:
            subtitle_sessions = (source_session(), target_session())
        deadline = time.monotonic() + remaining_minutes * 60
        reconcile_resolution_names(pipelines, subtitle_sessions[1], deadline=deadline)
        remaining_minutes = int((deadline - time.monotonic()) / 60)
        if has_targets and remaining_minutes > 0:
            run_subtitles(root, *subtitle_sessions, persist, 20, remaining_minutes,
                          checkpoint_lock=checkpoint_lock, state_store=pipelines["en"].state)

    continuous = args.mode == "continuous"
    counts = drain_queue(root, pipelines, films, limit=args.limit,
                         runtime_minutes=args.runtime_minutes, persist=persist,
                         continuous=continuous, refresh=refresh if continuous else None,
                         subtitles=subtitles if continuous else None, session_pairs=pairs,
                         checkpoint_lock=checkpoint_lock,
                         subtitle_drain_minutes=max(0, 330 - args.runtime_minutes) if continuous else 0)
    print(json.dumps(counts), flush=True)
    return int(counts["failed"] > 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"failed={type(error).__name__}", flush=True)
        raise SystemExit(1) from None
