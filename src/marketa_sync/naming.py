"""Resolution-aware film titles without changing audio/subtitle variants."""
from __future__ import annotations

import re
import time

from sdilej_to_prehrajto.pipeline import SyncPipeline
from sdilej_to_prehrajto.prehrajto import rename_video, uploaded_video_id_by_name, PrehrajtoError
from sdilej_to_prehrajto.ranking import resolution_label
from sdilej_to_prehrajto.subtitle_target import trusted_detail

LANGUAGE_SUFFIX = re.compile(r"\s+CZ\s+(?:Dabing|Titulky)$", re.I)
RESOLUTION_SUFFIX = re.compile(r"\s+(?:SD|[248]K|\d{3,4}p)$", re.I)
OWNED_SOURCE_EVIDENCE = {"relay_completed", "reconciled_after_relay_error", "reconciled_owned_prepared",
                         "reconciled_statistics_and_uploaded_listing"}


def without_resolution(title: str) -> str:
    language = LANGUAGE_SUFFIX.search(title)
    suffix = language.group() if language else ""
    base = title[:language.start()] if language else title
    return RESOLUTION_SUFFIX.sub("", base) + suffix


def with_resolution(title: str, width: int, height: int = 0) -> str:
    if not width and not height:
        return title
    title = re.sub(r"\.(?:mkv|mp4|avi|mov|m4v|webm|mpeg|mpg|wmv|ts)$", "", title, flags=re.I)
    language = LANGUAGE_SUFFIX.search(title)
    suffix = language.group() if language else ""
    base = title[:language.start()] if language else title
    base = RESOLUTION_SUFFIX.sub("", base)
    quality = "8K" if width >= 7680 or height >= 4320 else resolution_label(width, height)
    return f"{base} {quality}{suffix}"


class MarketaPipeline(SyncPipeline):
    """Recognize pre-resolution names without relaxing language/year checks."""

    def owns_prepared_source(self, video_id):
        for film_id, source in list(self._selected.items()):
            prepared = self.state.snapshot(film_id).get("prepared") or {}
            if (str(prepared.get("target_video_id")) == str(video_id)
                    and str(prepared.get("relay_source_id")) == str(source.source_id)):
                return True
        return False

    def _target_id_by_name(self, session, name):
        found = SyncPipeline._target_id_by_name(session, name)
        legacy = without_resolution(name)
        if found or legacy == name:
            return found
        found = SyncPipeline._target_id_by_name(session, legacy)
        if found and not self.owns_prepared_source(found):
            # A legacy title alone cannot prove the resolution of the selected
            # source. Block duplicates without relabelling somebody else's file.
            for film_id, source in list(self._selected.items()):
                plan = self.state.snapshot(film_id).get("last_plan") or {}
                if plan.get("display_name") == name:
                    self.state.record_attempt(film_id, {
                        "status": "target_requires_review", "source_id": source.source_id,
                        "target_video_id": found, "permanent": False,
                        "reason": "Legacy target resolution binding requires verification"})
                    self.state.persist_external("failure")
            raise PrehrajtoError("Existing legacy target needs verified resolution binding")
        return found

    def _existing_target_complete(self, session, video_id, name):
        if SyncPipeline._existing_target_complete(session, video_id, name):
            return True
        legacy = without_resolution(name)
        if (legacy != name and self.owns_prepared_source(video_id)
                and SyncPipeline._existing_target_complete(session, video_id, legacy)):
            return self._target_completed_and_named(session, video_id, name)
        return False


def confirm_rename(session, video_id: str, wanted: str, *, expected=None) -> str:
    if uploaded_video_id_by_name(session, wanted, include_processing=False) != video_id:
        if expected and uploaded_video_id_by_name(session, expected, include_processing=False) != video_id:
            raise PrehrajtoError("Target title changed or is still processing")
        rename_video(session, video_id, wanted)
        if uploaded_video_id_by_name(session, wanted, include_processing=False) != video_id:
            raise PrehrajtoError("Resolution name change unconfirmed")
    return getattr(session, "_prehrajto_detail_urls", {}).get(video_id, "")


def reconcile_resolution_names(pipelines, session, *, limit=20, deadline=None):
    """Rename completed, ledger-owned targets and update their live stores."""
    renamed = 0
    for variant, pipeline in pipelines.items():
        store = pipeline.state
        with store._lock:
            film_ids = list(store.data["films"])
        for film_id in film_ids:
            if renamed >= limit or (deadline is not None and time.monotonic() >= deadline):
                return renamed
            entry = store.snapshot(film_id)
            upload = entry.get("upload") or {}
            if not upload.get("display_name") or not upload.get("target_video_id"):
                continue
            if upload.get("completion_evidence") not in OWNED_SOURCE_EVIDENCE:
                continue
            old = upload["display_name"]
            wanted = with_resolution(old, upload.get("width", 0), upload.get("height", 0))
            if old == wanted:
                continue
            video_id = str(upload["target_video_id"])
            try:
                # Metadata HTTP requests must not hold up transfer checkpoints.
                detail = confirm_rename(session, video_id, wanted, expected=old)
            except Exception as error:
                print(f"resolution_name_deferred variant={variant} target={video_id} error={type(error).__name__}", flush=True)
                continue
            with store._lock:
                current = store.snapshot(film_id).get("upload") or {}
                if (current.get("target_video_id") != upload["target_video_id"]
                        or current.get("source_id") != upload.get("source_id")
                        or current.get("display_name") != old):
                    continue
                store.film(film_id)["upload"]["display_name"] = wanted
                if trusted_detail(detail):
                    store.film(film_id)["upload"]["detail_url"] = detail
                store.save("success")
                renamed += 1
                print(f"resolution_name_updated variant={variant} target={video_id}", flush=True)
    return renamed
