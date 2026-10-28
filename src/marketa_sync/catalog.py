"""Import proven source identities without importing another account's uploads."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import requests

from sdilej_to_prehrajto.models import Candidate, Film
from sdilej_to_prehrajto.sources import SelectedSourceStore
from sdilej_to_prehrajto.subtitle_backfill import atomic_write
from .discovery import POLICY, LEGACY_POLICY, allowed_audio

USER_AGENT = "MarketaFilmCatalog/0.1 (https://github.com/Olbrasoft/marketa-trefilova-to-prehrajto)"


def read_rows(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def stable_source(row: dict) -> bool:
    parsed = urlsplit(row.get("source_url", ""))
    source_id = str(row.get("source_id", ""))
    return bool(source_id.isdigit() and parsed.scheme == "https"
                and parsed.hostname == "sdilej.cz" and not parsed.query
                and not parsed.fragment and parsed.path.startswith(f"/{source_id}/"))


def source_candidate(row: dict) -> Candidate:
    names = {
        "source_id": "source_id", "url": "source_url", "title": "source_title",
        "filename": "source_filename", "size_bytes": "size_bytes",
        "duration_sec": "duration_sec", "width": "width", "height": "height",
        "audio_language": "audio_language", "language_tier": "language_tier",
        "language_probability": "language_probability", "language_evidence": "language_evidence",
        "match_tier": "match_tier", "match_evidence": "match_evidence",
        "mime_type": "mime_type", "video_codec": "video_codec", "query": "query",
    }
    data = {key: row[value] for key, value in names.items() if value in row}
    data["title"] = data.get("title") or row.get("source_filename") or row["source_url"]
    return Candidate.from_dict(data)


class VariantSourceStore(SelectedSourceStore):
    def __init__(self, path: Path, variant: str):
        super().__init__(path)
        self.variant = variant

    def candidate(self, film_id: int) -> Candidate | None:
        row = self.get(film_id)
        if not row or not stable_source(row) or row.get("match_tier") not in {"solid", "strong"}:
            return None
        candidate = source_candidate(row)
        if self.variant == "cz" and candidate.audio_language != "cs":
            return None
        if self.variant == "en" and (not allowed_audio(candidate) or
                row.get("match_evidence", {}).get("marketa", {}).get("policy") not in {POLICY, LEGACY_POLICY}):
            return None
        return candidate


def import_sources(root: Path, upstream: Path) -> dict:
    manifest = {int(row["cr_film_id"]): row for row in read_rows(upstream / "manifests/selected-sources.jsonl")}
    state = json.loads((upstream / "state/sync.json").read_text())["films"]
    imported_uploads = 0
    for film_id, transfer in state.items():
        upload = transfer.get("upload") or {}
        selected = transfer.get("last_plan", {}).get("selected") or {}
        # Only use a plan that is bound to the actual transferred source ID.
        if not upload.get("source_url") or selected.get("source_id") != upload.get("source_id"):
            continue
        candidate = Candidate.from_dict(selected)
        row = candidate.to_dict()
        manifest[int(film_id)] = {
            **row, "cr_film_id": int(film_id), "source_url": row.pop("url"),
            "source_title": candidate.title, "source_filename": candidate.filename,
            "size_bytes": upload.get("size_bytes") or candidate.size_bytes,
            "provenance": "upstream_completed_upload", "source_status": "verified",
        }
        imported_uploads += 1
    store = VariantSourceStore(root / "variants/cz/manifests/selected-sources.jsonl", "cz")
    target_state = json.loads((root / "variants/cz/state/sync.json").read_text()).get("films", {})
    count = 0
    # Whitelist fields: do not copy target IDs, claims, failures, or signed URLs.
    for film_id, row in sorted(manifest.items()):
        if row.get("audio_language") != "cs" or not stable_source(row):
            continue
        if row.get("match_tier") not in {"solid", "strong"}:
            continue
        existing = store.get(film_id)
        transfer = target_state.get(str(film_id), {})
        if existing or any(transfer.get(key) for key in ("upload", "prepared", "previous_target_id", "claim")):
            continue
        candidate = source_candidate(row)
        clean = candidate.to_dict()
        clean.update(cr_film_id=film_id, source_url=clean.pop("url"),
                     source_title=clean.pop("title"), source_filename=clean.pop("filename"),
                     source_status="verified", upload_status="pending",
                     provenance=row.get("provenance", "upstream_verified_manifest"))
        store.record(clean)
        count += 1
    store.compact()
    return {"new_czech_sources": count, "total_czech_sources": len(store),
            "upstream_upload_sources": imported_uploads}


class EnglishTitles:
    def __init__(self, root: Path, session=None):
        self.path = root / "state/english-titles.json"
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.overrides = json.loads((root / "config/english-titles.json").read_text())
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get(self, film: dict) -> str | None:
        override = self.overrides.get(str(film["cr_film_id"]))
        if override:
            return override["title"]
        cached = self.rows.get(str(film["cr_film_id"]), {})
        return cached.get("title")

    def original(self, film: dict) -> str | None:
        # Prefer the existing database export; only fill gaps from another
        # database record with the same movie identity.
        for title in (film.get("original_title"), self.rows.get(str(film["cr_film_id"]), {}).get("original_title")):
            if isinstance(title, str) and title.strip():
                return title.strip()
        return None

    def original_language(self, film: dict) -> str | None:
        for language in (film.get("original_language"), self.rows.get(str(film["cr_film_id"]), {}).get("original_language")):
            if isinstance(language, str) and re.fullmatch(r"[a-z]{2}", language):
                return language
        return None

    def fill_originals(self, films: list[dict], limit: int) -> int:
        api_key = os.environ.get("TMDB_API_KEY")
        if not api_key:
            return 0
        now = datetime.now(timezone.utc)
        pending = [film for film in films if self.get(film)
                   and (not self.original(film) or not self.original_language(film))
                   and str(film.get("tmdb_id") or "").isdigit()
                   and self.rows.get(str(film["cr_film_id"]), {}).get("original_retry_after", "") <= now.isoformat()][:limit]
        resolved = 0
        for film in pending:
            tmdb_id = int(film["tmdb_id"])
            cached = self.rows.setdefault(str(film["cr_film_id"]), {})
            try:
                response = self.session.get(f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                                            params={"api_key": api_key, "language": "en-US"}, timeout=30)
                response.raise_for_status()
                data = response.json()
                if data.get("id") != tmdb_id or (film.get("imdb_id") and data.get("imdb_id")
                                                and film["imdb_id"] != data["imdb_id"]):
                    raise ValueError("Movie identity mismatch")
                title = data.get("original_title")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("Original title missing")
                cached.update(original_title=title.strip(),
                              original_source=f"https://www.themoviedb.org/movie/{tmdb_id}")
                language = data.get("original_language")
                if isinstance(language, str) and re.fullmatch(r"[a-z]{2}", language):
                    cached["original_language"] = language
                cached.pop("original_retry_after", None)
                resolved += 1
            except (requests.RequestException, ValueError):
                # Request exception strings may include the API key.
                cached["original_retry_after"] = (now + timedelta(hours=6)).isoformat()
        if pending:
            self.save()
        return resolved

    def resolve(self, films: list[dict], limit=100) -> int:
        now = datetime.now(timezone.utc)
        pending = [film for film in films if not self.get(film)
                   and self.rows.get(str(film["cr_film_id"]), {}).get("retry_after", "") <= now.isoformat()][:limit]
        for start in range(0, len(pending), 25):
            batch = pending[start:start + 25]
            values = []
            for film in batch:
                imdb = film.get("imdb_id") or ""
                tmdb = str(film.get("tmdb_id") or "")
                if re.fullmatch(r"tt\d+", imdb):
                    values.append(f'({film["cr_film_id"]} wdt:P345 "{imdb}")')
                elif tmdb.isdigit():
                    values.append(f'({film["cr_film_id"]} wdt:P4947 "{tmdb}")')
            if not values:
                for film in batch:
                    self.rows[str(film["cr_film_id"])] = {"status": "missing_external_id",
                        "retry_after": (now + timedelta(days=7)).isoformat()}
                self.save()
                continue
            query = ('SELECT ?id ?item ?title WHERE { VALUES (?id ?property ?external) { '
                     + " ".join(values) + ' } ?item ?property ?external; rdfs:label ?title . '
                     + 'FILTER(LANG(?title) = "en") }')
            response = self.session.get("https://query.wikidata.org/sparql",
                                        params={"query": query, "format": "json"}, timeout=60)
            response.raise_for_status()
            grouped = {}
            for result in response.json()["results"]["bindings"]:
                grouped.setdefault(result["id"]["value"], []).append(result)
            for film in batch:
                key = str(film["cr_film_id"])
                results = grouped.get(key, [])
                if len(results) == 1:
                    self.rows[key] = {"title": results[0]["title"]["value"],
                                      "source": results[0]["item"]["value"], "language": "en"}
                else:
                    self.rows[key] = {"status": "missing_or_ambiguous",
                                      "retry_after": (now + timedelta(days=7)).isoformat()}
            self.save()
        self.fill_originals(films, limit)
        return sum(bool(self.get(film)) for film in pending)

    def save(self):
        atomic_write(self.path, json.dumps(self.rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def combined_title(english_title: str, original_title: str | None) -> str:
    """Combine database names verbatim; never translate or synthesize a title."""
    english_title = english_title.strip()
    original_title = (original_title or "").strip()
    def identity(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not original_title or identity(english_title) == identity(original_title):
        return english_title
    return f"{english_title} ({original_title})"


def named_films(rows: list[dict], titles: EnglishTitles) -> tuple[list[Film], dict[int, str]]:
    known = [(row, titles.get(row)) for row in rows]
    counts = Counter((title or row.get("original_title") or "").casefold() for row, title in known)
    year_counts = Counter(((title or row.get("original_title") or "").casefold(), row.get("year")) for row, title in known)
    films, names = [], {}
    for row, title in known:
        if not title:
            continue
        film = Film.from_dict({**row, "original_title": titles.original(row),
                              "original_language": titles.original_language(row)})
        # Keep both recorded names searchable; disambiguate remakes separately.
        name = combined_title(title, film.original_title)
        if counts[title.casefold()] > 1 and film.year:
            name = f"{name} ({film.year})"
        if counts[title.casefold()] > 1 and (not film.year or year_counts[title.casefold(), film.year] > 1):
            name = f"{name} [ID {film.cr_film_id}]"
        names[film.cr_film_id] = name
        films.append(film)
    return films, names
