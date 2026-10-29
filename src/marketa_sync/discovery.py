"""Select English or original audio, preferring verified Czech subtitles."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time

from bs4 import BeautifulSoup
import requests

from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.matching import classify_candidate, normalize_title
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import quality_acceptable, resolution_rank
from sdilej_to_prehrajto.sdilej import SdilejProvider, SdilejError, PremiumRequiredError, parse_detail_html, slugify
from sdilej_to_prehrajto.subtitle_media import choose_stream, run_media, SubtitleUnavailable, is_czech, TEXT_CODECS
from sdilej_to_prehrajto.subtitle_backfill import atomic_write
from .subtitle_visual import inspect_burned_in, VISUAL_POLICY

LEGACY_POLICY = "english-subtitles-first-v1"
POLICY = "original-subtitles-first-v2"

LANGUAGE_CODES = {
    "eng": "en", "english": "en", "ces": "cs", "cze": "cs", "cz": "cs",
    "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh", "cmn": "zh",
    "deu": "de", "ger": "de", "fra": "fr", "fre": "fr", "ita": "it",
    "spa": "es", "rus": "ru", "por": "pt", "hin": "hi", "ara": "ar",
    "nld": "nl", "dut": "nl", "pol": "pl", "swe": "sv", "dan": "da",
    "nor": "no", "fin": "fi", "tur": "tr", "slk": "sk", "slo": "sk",
}


def audio_language(value: str | None) -> str:
    value = (value or "").lower().split("-")[0]
    return LANGUAGE_CODES.get(value, value)


def allowed_audio(candidate: Candidate) -> bool:
    original = candidate.match_evidence.get("marketa", {}).get("original_language")
    return bool(candidate.audio_language and candidate.audio_language in {"en", original})


def has_subtitles(candidate: Candidate) -> bool:
    return candidate.match_evidence.get("marketa", {}).get("czech_subtitles") is True


def bitmap_subtitles(candidate: Candidate) -> bool:
    evidence = candidate.match_evidence.get("marketa", {})
    if evidence.get("subtitle_method") == "burned_in_ocr_verified":
        return False
    codec = evidence.get("subtitle_codec")
    return (evidence.get("subtitle_method") == "original_embedded_bitmap"
            or bool(codec and codec not in TEXT_CODECS and evidence.get("subtitle_attachable") is False))


def choose_english(candidates: list[Candidate]) -> Candidate | None:
    acceptable = [c for c in candidates if allowed_audio(c)
                  and c.match_tier in {MatchTier.STRONG, MatchTier.SOLID}
                  and quality_acceptable(c)]
    return min(acceptable, key=lambda c: (
        not has_subtitles(c),
        bitmap_subtitles(c),
        -resolution_rank(c.width, c.height),
        c.size_bytes or 0, c.source_id,
    ), default=None)


def probe_original(url: str) -> dict:
    payload = json.loads(run_media([
        "ffprobe", "-v", "error", "-rw_timeout", "20000000",
        "-show_streams", "-show_format", "-of", "json", url,
    ], 90))
    if not any(s.get("codec_type") == "video" for s in payload.get("streams", [])):
        raise SdilejError("Original video stream could not be verified")
    return payload


class EnglishProvider(SdilejProvider):
    def __init__(self, *args, english_titles: dict[int, str], probe=probe_original, cache_path=None,
                 visual_inspector=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.english_titles = english_titles
        self.probe_original = probe
        self.visual_inspector = visual_inspector or inspect_burned_in
        self.cache_path = cache_path
        self.cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}

    def _match(self, film: Film, title: str, duration_sec: int | None):
        aliases = dict.fromkeys(alias for alias in (
            self.english_titles[film.cr_film_id], film.original_title, film.title,
        ) if alias and normalize_title(alias))
        if not aliases:
            raise SdilejError("No searchable database title")
        matches = [classify_candidate(replace(film, title=alias, original_title=None),
                                      title, duration_sec=duration_sec) for alias in aliases]
        tiers = {MatchTier.REJECT: 0, MatchTier.AMBIGUOUS: 1, MatchTier.SOLID: 2, MatchTier.STRONG: 3}
        return max(matches, key=lambda match: (tiers[match.tier], match.score))

    def verify_english(self, film: Film, candidate: Candidate) -> Candidate | None:
        html = self._get(candidate.url).text
        detail = parse_detail_html(html, candidate)
        payload = self.probe_original(detail.download_url)
        audio = [s for s in payload["streams"] if s.get("codec_type") == "audio"]
        if not audio:
            return None
        # Verify the first/default audio that the relayed file actually plays;
        # a secondary original track cannot prove the default audio language.
        defaults = [s for s in audio if s.get("disposition", {}).get("default")]
        languages = {"en", film.original_language} - {None, ""}
        for stream in [audio[0], *defaults]:
            language = audio_language(stream.get("tags", {}).get("language"))
            if language and language not in languages | {"und"}:
                return None
        video = next(s for s in payload["streams"] if s.get("codec_type") == "video")
        detail.width, detail.height = int(video["width"]), int(video["height"])
        detail.video_codec = {"hevc": "h265", "avc": "h264"}.get(video.get("codec_name"), video.get("codec_name"))
        detail.duration_sec = int(float(payload.get("format", {}).get("duration") or detail.duration_sec or 0))
        match = self._match(film, detail.title, detail.duration_sec)
        detail.match_tier, detail.match_evidence = match.tier, match.evidence
        if match.tier not in {MatchTier.STRONG, MatchTier.SOLID} or not quality_acceptable(detail):
            return None
        # Sample the original that will be relayed, not a possibly different preview.
        language, probability = self.language_detector.detect_consensus(
            detail.download_url, detail.duration_sec, preferred_language=film.original_language or "en")
        if probability < self.minimum_language_probability:
            raise LanguageDetectionError("Original audio language is uncertain")
        if language not in languages:
            return None
        detail.audio_language, detail.language_probability = language, probability
        detail.language_tier = LanguageTier.FOREIGN_AUDIO
        detail.language_evidence = "whisper_original_multisample"
        subtitles = [s for s in payload["streams"] if s.get("codec_type") == "subtitle"]
        full_czech = [s for s in subtitles if is_czech(s.get("tags", {}).get("language", ""))
                      and not s.get("disposition", {}).get("forced")
                      and not re.search(r"forced|vynucen|commentary|komentář", s.get("tags", {}).get("title", ""), re.I)]
        try:
            stream = choose_stream(subtitles)
            evidence = {"czech_subtitles": True, "subtitle_stream_index": stream["index"],
                        "subtitle_codec": stream["codec_name"], "subtitle_attachable": True,
                        "subtitle_method": "original_embedded_text"}
        except SubtitleUnavailable:
            evidence = {"czech_subtitles": False, "subtitle_attachable": False}
            if full_czech:
                evidence.update(subtitle_stream_index=full_czech[0]["index"],
                                subtitle_codec=full_czech[0].get("codec_name"),
                                source_czech_bitmap=True,
                                subtitle_method="original_embedded_bitmap")
        page_tracks = BeautifulSoup(html, "html.parser").select("video track[src]")
        if any(is_czech(t.get("srclang", "")) and t.get("kind", "subtitles") in {"subtitles", "captions"}
               and not re.search(r"forced|vynucen|commentary", t.get("label", ""), re.I)
               for t in page_tracks):
            evidence = {"czech_subtitles": True, "subtitle_method": "original_page_track", "subtitle_attachable": True}
        if not evidence["czech_subtitles"]:
            visual = self.visual_inspector(detail.download_url, detail.duration_sec,
                                           deadline=getattr(self, "discovery_deadline", None))
            evidence.update(visual)
            if visual["visual_status"] == "czech_burned_in_verified":
                evidence.update(czech_subtitles=True, subtitle_method="burned_in_ocr_verified",
                                subtitle_attachable=False)
            elif (visual["visual_status"] == "uncertain" or (not evidence.get("source_czech_bitmap") and re.search(
                    r"\b(?:cz|ceske|české)[ ._-]*(?:tit|titulky|subs?)\b", detail.title, re.I))):
                # A filename hint or a failed visual check is not language proof.
                raise SubtitleUnavailable("burned_in_or_external_subtitles_require_review")
        # The active uploader refreshes manifests without reloading its code.
        # Keep new English sources readable by that runner; only non-English
        # originals require the expanded policy used by its successor.
        detail.match_evidence["marketa"] = {
            "policy": LEGACY_POLICY if language == "en" else POLICY,
            "original_language": film.original_language, **evidence,
        }
        return detail

    def discover(self, film: Film) -> list[Candidate]:
        deadline = time.monotonic() + self.discovery_timeout_seconds
        self.discovery_deadline = deadline
        english_title = self.english_titles[film.cr_film_id]
        candidates = {}
        queries = []
        for title in dict.fromkeys([english_title, film.original_title, film.title]):
            if not title or not slugify(title):
                continue
            queries.extend([f"{title} {film.year}" if film.year else title, title])
        if slugify(english_title):
            queries.extend(f"{english_title} {suffix}" for suffix in ("en", "eng", "cz titulky", "cz tit"))
        for query in dict.fromkeys(queries):
            if time.monotonic() >= deadline:
                raise SdilejError("English search incomplete; fallback deferred")
            for candidate in self.search_by_quality(query):
                match = self._match(film, candidate.title, candidate.duration_sec)
                if match.tier in {MatchTier.STRONG, MatchTier.SOLID}:
                    candidates[candidate.source_id] = candidate
        resolved = []
        incomplete = False
        cached = self.cache.setdefault(str(film.cr_film_id), {})
        # Visit unchecked candidates before retrying failures from a previous
        # run. A broken high-resolution file must not hide all later sources.
        for candidate in sorted(candidates.values(), key=lambda c: (
            not bool(cached.get(c.source_id, {}).get("candidate")),
            bool(cached.get(c.source_id, {}).get("error")),
            -resolution_rank(c.width, c.height), c.size_bytes or 0, c.source_id)):
            if time.monotonic() >= deadline:
                incomplete = True
                break
            # Any unresolved candidate can contain Czech subtitles. Never treat a
            # timeout as evidence that the subtitle-free fallback is the best option.
            record = cached.get(candidate.source_id)
            current = record is not None and (
                record.get("policy") == POLICY and record.get("original_language") == film.original_language
                or record.get("policy") == LEGACY_POLICY and film.original_language in {None, "en"})
            if current and record.get("candidate"):
                evidence = record["candidate"].get("match_evidence", {}).get("marketa", {})
                needs_visual = (not evidence.get("czech_subtitles")
                                or bitmap_subtitles(Candidate.from_dict(record["candidate"])))
                if needs_visual and evidence.get("visual_policy") != VISUAL_POLICY:
                    current = False
            if current and record.get("error") and record.get("retry_after", "") > datetime.now(timezone.utc).isoformat():
                incomplete = True
                continue
            if current and "candidate" in record:
                verified = Candidate.from_dict(record["candidate"]) if record.get("candidate") else None
            else:
                try:
                    verified = self.verify_english(film, candidate)
                except PremiumRequiredError:
                    raise
                except (SdilejError, LanguageDetectionError, SubtitleUnavailable, requests.RequestException, TimeoutError) as error:
                    incomplete = True
                    cached[candidate.source_id] = {
                        "policy": POLICY, "original_language": film.original_language,
                        "error": type(error).__name__,
                        "retry_after": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    }
                    if self.cache_path:
                        atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, sort_keys=True) + "\n")
                    continue
                cached[candidate.source_id] = {"policy": POLICY, "original_language": film.original_language,
                                               "candidate": verified.to_dict() if verified else None}
                if self.cache_path:
                    atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, sort_keys=True) + "\n")
            if verified:
                resolved.append(verified)
        selected = choose_english(resolved)
        # A verified subtitled source satisfies the preference even when some
        # other files are unavailable. Only a subtitle-free fallback requires
        # a complete scan proving that no subtitled candidate was overlooked.
        if incomplete and (selected is None or not has_subtitles(selected)):
            raise SdilejError("Original source verification incomplete; selection deferred")
        return [selected] if selected else []


def display_name(english_title: str, variant: str, candidate: Candidate) -> str:
    if variant == "cz":
        if candidate.audio_language != "cs":
            raise ValueError("Czech dubbing requires verified Czech audio")
        return f"{english_title} CZ Dabing"
    if not allowed_audio(candidate):
        raise ValueError("Original variant requires verified English or original audio")
    return f"{english_title} CZ Titulky" if has_subtitles(candidate) else english_title
