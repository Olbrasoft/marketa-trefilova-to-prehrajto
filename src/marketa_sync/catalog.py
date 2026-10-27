"""Import proven source identities without importing another account's uploads."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests

from sdilej_to_prehrajto.models import Candidate, Film
from sdilej_to_prehrajto.sources import SelectedSourceStore
from sdilej_to_prehrajto.subtitle_backfill import atomic_write

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
        expected = "cs" if self.variant == "cz" else "en"
        if row.get("audio_language") != expected:
            return None
        if self.variant == "en" and row.get("match_evidence", {}).get("marketa", {}).get("policy") != "english-subtitles-first-v1":
            return None
        return source_candidate(row)


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
        if existing or target_state.get(str(film_id)):
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
        return sum(bool(self.get(film)) for film in pending)

    def save(self):
        atomic_write(self.path, json.dumps(self.rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def named_films(rows: list[dict], titles: EnglishTitles) -> tuple[list[Film], dict[int, str]]:
    known = [(row, titles.get(row)) for row in rows]
    counts = Counter((title or row.get("original_title") or "").casefold() for row, title in known)
    films, names = [], {}
    for row, title in known:
        if not title:
            continue
        film = Film.from_dict(row)
        # Preserve the requested short name; distinguish known remakes by year.
        name = f"{title} ({film.year})" if counts[title.casefold()] > 1 and film.year else title
        names[film.cr_film_id] = name
        films.append(film)
    return films, names
