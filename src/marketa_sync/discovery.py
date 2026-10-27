"""Select English audio, preferring verified Czech subtitles over resolution."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import time

from bs4 import BeautifulSoup

from sdilej_to_prehrajto.language import LanguageDetectionError
from sdilej_to_prehrajto.matching import classify_candidate
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.ranking import quality_acceptable, resolution_rank
from sdilej_to_prehrajto.sdilej import SdilejProvider, SdilejError, parse_detail_html
from sdilej_to_prehrajto.subtitle_media import choose_stream, run_media, SubtitleUnavailable, is_czech
from sdilej_to_prehrajto.subtitle_backfill import atomic_write

POLICY = "english-subtitles-first-v1"


def has_subtitles(candidate: Candidate) -> bool:
    return candidate.match_evidence.get("marketa", {}).get("czech_subtitles") is True


def choose_english(candidates: list[Candidate]) -> Candidate | None:
    acceptable = [c for c in candidates if c.audio_language == "en"
                  and c.match_tier in {MatchTier.STRONG, MatchTier.SOLID}
                  and quality_acceptable(c)]
    return min(acceptable, key=lambda c: (
        not has_subtitles(c), -resolution_rank(c.width, c.height),
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
    def __init__(self, *args, english_titles: dict[int, str], probe=probe_original, cache_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.english_titles = english_titles
        self.probe_original = probe
        self.cache_path = cache_path
        self.cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}

    def verify_english(self, film: Film, candidate: Candidate) -> Candidate | None:
        html = self._get(candidate.url).text
        detail = parse_detail_html(html, candidate)
        payload = self.probe_original(detail.download_url)
        audio = [s for s in payload["streams"] if s.get("codec_type") == "audio"]
        if not audio:
            return None
        # A Czech first/default track must never be relabeled as English merely
        # because the same container also includes an English secondary track.
        defaults = [s for s in audio if s.get("disposition", {}).get("default")]
        for stream in [audio[0], *defaults]:
            language = stream.get("tags", {}).get("language", "").lower()
            if language and language not in {"en", "eng", "english", "und"}:
                return None
        video = next(s for s in payload["streams"] if s.get("codec_type") == "video")
        detail.width, detail.height = int(video["width"]), int(video["height"])
        detail.video_codec = {"hevc": "h265", "avc": "h264"}.get(video.get("codec_name"), video.get("codec_name"))
        detail.duration_sec = int(float(payload.get("format", {}).get("duration") or detail.duration_sec or 0))
        match = classify_candidate(film, detail.title, duration_sec=detail.duration_sec)
        detail.match_tier, detail.match_evidence = match.tier, match.evidence
        if match.tier not in {MatchTier.STRONG, MatchTier.SOLID} or not quality_acceptable(detail):
            return None
        # Sample the original that will be relayed, not a possibly different preview.
        language, probability = self.language_detector.detect_consensus(
            detail.download_url, detail.duration_sec, preferred_language="en")
        if probability < self.minimum_language_probability:
            raise LanguageDetectionError("Original audio language is uncertain")
        if language != "en":
            return None
        detail.audio_language, detail.language_probability = language, probability
        detail.language_tier = LanguageTier.FOREIGN_AUDIO
        detail.language_evidence = "whisper_original_multisample"
        subtitles = [s for s in payload["streams"] if s.get("codec_type") == "subtitle"]
        try:
            stream = choose_stream(subtitles)
            evidence = {"czech_subtitles": True, "subtitle_stream_index": stream["index"],
                        "subtitle_codec": stream["codec_name"]}
        except SubtitleUnavailable:
            evidence = {"czech_subtitles": False}
        page_tracks = BeautifulSoup(html, "html.parser").select("video track[src]")
        if any(is_czech(t.get("srclang", "")) and t.get("kind", "subtitles") in {"subtitles", "captions"}
               and not re.search(r"forced|vynucen|commentary", t.get("label", ""), re.I)
               for t in page_tracks):
            evidence = {"czech_subtitles": True, "subtitle_method": "original_page_track"}
        if not evidence["czech_subtitles"] and re.search(r"\b(?:cz|ceske|české)[ ._-]*(?:tit|titulky|subs?)\b", detail.title, re.I):
            raise SubtitleUnavailable("burned_in_or_external_subtitles_require_review")
        detail.match_evidence["marketa"] = {"policy": POLICY, **evidence}
        return detail

    def discover(self, film: Film) -> list[Candidate]:
        deadline = time.monotonic() + self.discovery_timeout_seconds
        film = replace(film, original_title=self.english_titles[film.cr_film_id])
        candidates = {}
        queries = []
        for title in dict.fromkeys([film.original_title, film.title]):
            queries.extend([f"{title} {film.year}" if film.year else title, title])
        queries.extend(f"{film.original_title} {suffix}" for suffix in ("en", "eng", "cz titulky", "cz tit"))
        for query in dict.fromkeys(queries):
            if time.monotonic() >= deadline:
                raise SdilejError("English search incomplete; fallback deferred")
            for candidate in self.search_by_quality(query):
                match = classify_candidate(film, candidate.title, duration_sec=candidate.duration_sec)
                if match.tier in {MatchTier.STRONG, MatchTier.SOLID}:
                    candidates[candidate.source_id] = candidate
        resolved = []
        cached = self.cache.setdefault(str(film.cr_film_id), {})
        for candidate in sorted(candidates.values(), key=lambda c: (
            -resolution_rank(c.width, c.height), c.size_bytes or 0, c.source_id)):
            if time.monotonic() >= deadline:
                raise SdilejError("English discovery incomplete; subtitle-free fallback deferred")
            # Any unresolved candidate can contain Czech subtitles. Never treat a
            # timeout as evidence that the subtitle-free fallback is the best option.
            record = cached.get(candidate.source_id)
            if record is not None and record.get("policy") == POLICY:
                verified = Candidate.from_dict(record["candidate"]) if record.get("candidate") else None
            else:
                verified = self.verify_english(film, candidate)
                cached[candidate.source_id] = {"policy": POLICY, "candidate": verified.to_dict() if verified else None}
                if self.cache_path:
                    atomic_write(self.cache_path, json.dumps(self.cache, ensure_ascii=False, sort_keys=True) + "\n")
            if verified:
                resolved.append(verified)
        selected = choose_english(resolved)
        return [selected] if selected else []


def display_name(english_title: str, variant: str, candidate: Candidate) -> str:
    if variant == "cz":
        if candidate.audio_language != "cs":
            raise ValueError("Czech dubbing requires verified Czech audio")
        return f"{english_title} CZ Dabing"
    if candidate.audio_language != "en":
        raise ValueError("English variant requires verified English audio")
    return f"{english_title} CZ Titulky" if has_subtitles(candidate) else english_title
