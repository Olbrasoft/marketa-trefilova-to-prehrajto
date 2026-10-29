import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from marketa_sync.catalog import EnglishTitles, VariantSourceStore, combined_title, import_sources, named_films
from marketa_sync.cli import make_pipeline, ordered_plans, followup_report, drain_queue, checkpoint_paths, prepare, run_subtitles
from marketa_sync.discovery import choose_english, display_name, POLICY, LEGACY_POLICY, EnglishProvider
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.prehrajto import _uploaded_video_id_from_html, login, PrehrajtoError
from sdilej_to_prehrajto.state import StateStore
from sdilej_to_prehrajto.subtitle_backfill import Backfill, source_map
from sdilej_to_prehrajto.subtitle_media import SubtitleUnavailable


def candidate(source_id="123", language="en", width=1920, subtitles=False):
    return Candidate(source_id, f"https://sdilej.cz/{source_id}/film.mkv", "The Film 2000",
                     size_bytes=5_000_000_000, duration_sec=7200, width=width, height=1080,
                     audio_language=language, language_probability=0.99, language_evidence="verified",
                     language_tier=LanguageTier.CZECH_AUDIO if language == "cs" else LanguageTier.FOREIGN_AUDIO,
                     match_tier=MatchTier.STRONG, match_evidence={"marketa": {
                         "policy": POLICY, "czech_subtitles": subtitles}})


@pytest.mark.parametrize("subtitles,width,expected", [(True,1280,"sub"),(False,1280,"4k"),(True,3840,"sub")])
def test_subtitles_outrank_resolution(subtitles, width, expected):
    high = candidate("4k",width=3840)
    sub = candidate("sub",width=width,subtitles=subtitles)
    assert choose_english([high,sub]).source_id == expected


def test_only_english_can_be_an_english_variant():
    assert choose_english([candidate(language="de"),candidate(language="cs")]) is None
    with pytest.raises(ValueError):
        display_name("The Film", "en", candidate(language="cs"))


@pytest.mark.parametrize("variant,language,subtitles,title", [
    ("cz","cs",False,"The Film CZ Dabing"),
    ("en","en",True,"The Film CZ Titulky"),
    ("en","en",False,"The Film"),
])
def test_exact_names(variant,language,subtitles,title):
    assert display_name("The Film", variant, candidate(language=language,subtitles=subtitles)) == title


@pytest.mark.parametrize("wanted,visible", [
    ("The Film", "The Film CZ Dabing"), ("The Film CZ Dabing", "The Film CZ Titulky"),
    ("The Film (2000) CZ Dabing", "The Film (2001) CZ Dabing"),
])
def test_target_lookup_never_merges_variants_or_remakes(wanted, visible):
    html=f'<h3 id="snippet-uploadedVideoListing-videoName-777">{visible}.mkv</h3>'
    assert _uploaded_video_id_from_html(html,wanted) is None


def test_old_target_account_is_rejected_before_network():
    with pytest.raises(PrehrajtoError):
        login("sdilej.prehrajto@seznam.cz", "irrelevant")


def seed_source(root, variant, film_id, source):
    pipeline=make_pipeline(root,variant,{film_id:"The Film"})
    film=Film(film_id,"film","Film","The Film",2000,120,"en",priority_rank=film_id)
    pipeline._record_verified_source(film,source,display_name("The Film",variant,source))
    return pipeline,film


def test_same_film_uploads_independently_by_variant(tmp_path):
    cz, film=seed_source(tmp_path,"cz",1,candidate(language="cs"))
    en,_=seed_source(tmp_path,"en",1,candidate(subtitles=True))
    assert [v for v,_ in ordered_plans({"cz":cz,"en":en},[film],10)] == ["cz","en"]
    cz.state.record_success(1,{"target_video_id":"777"})
    assert [v for v,_ in ordered_plans({"cz":cz,"en":en},[film],10)] == ["en"]


def test_completed_upload_keeps_exact_source_and_subtitle_followup(tmp_path):
    pipeline,film=seed_source(tmp_path,"en",1,candidate(subtitles=True))
    plan=pipeline.build_plan([film],1,verified_only=True)
    pipeline._finish_success(film,candidate(subtitles=True),plan[0],{
        "target_video_id":"777","source_id":"123","source_url":"https://sdilej.cz/123/film.mkv",
        "display_name":"The Film CZ Titulky","needs_czech_subtitles":True,
        "completion_evidence":"reconciled_owned_prepared",
    })
    assert source_map(tmp_path/"variants/en")["777"]["source_id"]=="123"
    followup_report(tmp_path)
    assert "The Film CZ Titulky" in (tmp_path/"reports/subtitle-followup.md").read_text()


def test_import_drops_target_state_and_prefers_actual_uploaded_source(tmp_path):
    upstream=tmp_path/"old"
    (upstream/"manifests").mkdir(parents=True)
    (upstream/"state").mkdir()
    (tmp_path/"variants/cz/state").mkdir(parents=True)
    (tmp_path/"variants/cz/state/sync.json").write_text('{"schema_version":1,"films":{}}')
    uploaded=candidate("111",language="cs")
    current={"cr_film_id":1,"source_id":"222","source_url":"https://sdilej.cz/222/film.mkv",
             "source_title":"The Film","audio_language":"cs","language_tier":"czech_audio",
             "match_tier":"strong","upload_status":"success","target_video_id":"999"}
    (upstream/"manifests/selected-sources.jsonl").write_text(json.dumps(current)+'\n')
    (upstream/"state/sync.json").write_text(json.dumps({"films":{"1":{
        "last_plan":{"selected":uploaded.to_dict()},"upload":{
            "source_id":"111","source_url":uploaded.url,"target_video_id":"999"}}}}))
    import_sources(tmp_path,upstream)
    store=VariantSourceStore(tmp_path/"variants/cz/manifests/selected-sources.jsonl","cz")
    assert store.candidate(1).source_id=="111"
    assert not store.uploaded(1)
    assert "target_video_id" not in store.get(1)
    assert StateStore(tmp_path/"variants/cz/state/sync.json").data["films"]=={}
    assert import_sources(tmp_path,upstream)["new_czech_sources"]==0


def test_original_title_is_not_assumed_to_be_english(tmp_path):
    (tmp_path/"config").mkdir()
    (tmp_path/"config/english-titles.json").write_text('{}')
    titles=EnglishTitles(tmp_path)
    row={"cr_film_id":1,"title":"Amélie","original_title":"Le Fabuleux Destin d'Amélie Poulain"}
    assert titles.get(row) is None
    assert named_films([row],titles)==([], {})


def test_processing_keeps_relay_source_binding(tmp_path):
    state=StateStore(tmp_path/"state.json")
    state.record_plan(1,{"selected":{"source_id":"123"},"display_name":"The Film"})
    state.record_prepared(1,"777",100)
    state.record_target_processing(1,video_id="777",size=100,source_id="123")
    assert StateStore(state.path).snapshot(1)["prepared"]["relay_source_id"]=="123"


def test_unverified_english_manifest_is_not_uploadable(tmp_path):
    store=VariantSourceStore(tmp_path/"manifest.jsonl","en")
    store.record({"cr_film_id":1,"source_id":"123","source_url":"https://sdilej.cz/123/film.mkv",
                  "audio_language":"en","match_tier":"strong"})
    assert store.candidate(1) is None


def test_identical_english_titles_and_years_get_distinct_target_names(tmp_path):
    (tmp_path/"config").mkdir()
    (tmp_path/"config/english-titles.json").write_text(json.dumps({
        "1":{"title":"The Film"},"2":{"title":"The Film"}}))
    films=[{"cr_film_id":i,"title":"Film","year":2000} for i in [1,2]]
    _,names=named_films(films,EnglishTitles(tmp_path))
    assert names[1]!=names[2]


def test_source_timeout_never_selects_an_unsubtitled_fallback(monkeypatch):
    from sdilej_to_prehrajto.sdilej import SdilejError
    film=Film(1,"film","The Film","The Film",2000,120,"en")
    provider=EnglishProvider(None,None,english_titles={1:"The Film"},discovery_timeout_seconds=900)
    plain=candidate("123",width=3840)
    unknown=candidate("456",width=1920)
    monkeypatch.setattr(provider,"search_by_quality",lambda _: [plain,unknown])
    def verify(_film, item):
        if item.source_id=="456": raise SdilejError("unavailable")
        return item
    monkeypatch.setattr(provider,"verify_english",verify)
    with pytest.raises(SdilejError): provider.discover(film)


def test_resume_processing_upload_preserves_subtitle_provenance(tmp_path, monkeypatch):
    pipeline,film=seed_source(tmp_path,"en",1,candidate(subtitles=True))
    plan=pipeline.build_plan([film],1,verified_only=True)
    pipeline.state.record_prepared(1,"777",5_000_000_000)
    pipeline.state.record_target_processing(1,video_id="777",size=5_000_000_000,source_id="123")
    monkeypatch.setattr(pipeline,"_target_id_by_name",lambda *_: "777")
    monkeypatch.setattr(pipeline,"_existing_target_complete",lambda *_: True)
    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.relay_upload",lambda *_args,**_kwargs: pytest.fail("No second relay"))
    pipeline.execute(plan)
    upload=pipeline.state.snapshot(1)["upload"]
    assert upload["completion_evidence"]=="reconciled_owned_prepared"
    queue=json.loads((tmp_path/"variants/en/plans/subtitle-followup.jsonl").read_text())
    assert queue["source_binding_verified"] is True
    assert source_map(tmp_path/"variants/en")["777"]["source_id"]=="123"


@pytest.mark.parametrize("codec,visual_status,subtitled", [
    ("subrip", "no_text_detected", True),
    ("hdmv_pgs_subtitle", "no_text_detected", False),
    ("hdmv_pgs_subtitle", "czech_burned_in_verified", True),
])
def test_only_text_tracks_or_verified_pixels_justify_the_subtitle_label(monkeypatch, codec, visual_status, subtitled):
    film=Film(1,"film","The Film","The Film",2000,120,"en")
    detector=SimpleNamespace(detect_consensus=lambda *_args,**_kwargs: ("en",0.99))
    payload={"format":{"duration":"7200"},"streams":[
        {"codec_type":"video","width":1920,"height":1080,"codec_name":"h264"},
        {"codec_type":"audio","tags":{"language":"eng"}},
        {"codec_type":"subtitle","index":2,"codec_name":codec,"tags":{"language":"ces"}},
    ]}
    provider=EnglishProvider(None,detector,english_titles={1:"The Film"},probe=lambda _: payload,
        visual_inspector=lambda *_args, **_kwargs: {"visual_policy": "burned-in-czech-v1", "visual_status": visual_status})
    monkeypatch.setattr(provider,"_get",lambda _: SimpleNamespace(text="<html></html>"))
    monkeypatch.setattr("marketa_sync.discovery.parse_detail_html",lambda *_: candidate())
    result=provider.verify_english(film,candidate())
    assert result.match_evidence["marketa"]["czech_subtitles"] is subtitled
    assert result.match_evidence["marketa"]["subtitle_codec"]==codec
    assert display_name("The Film", "en", result) == ("The Film CZ Titulky" if subtitled else "The Film")


@pytest.mark.parametrize("visual_status,expected", [
    ("czech_burned_in_verified", "The Film CZ Titulky"),
    ("no_text_detected", "The Film"),
    ("foreign_text_detected", "The Film"),
    ("uncertain", None),
])
def test_visual_evidence_controls_names_and_unknown_results_are_deferred(monkeypatch, visual_status, expected):
    film = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    detector = SimpleNamespace(detect_consensus=lambda *_args, **_kwargs: ("en", 0.99))
    payload = {"format": {"duration": "7200"}, "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264"},
        {"codec_type": "audio", "tags": {"language": "eng"}}]}
    provider = EnglishProvider(None, detector, english_titles={1: "The Film"}, probe=lambda _: payload,
                               visual_inspector=lambda *_args, **_kwargs: {"visual_status": visual_status})
    monkeypatch.setattr(provider, "_get", lambda _: SimpleNamespace(text="<html></html>"))
    monkeypatch.setattr("marketa_sync.discovery.parse_detail_html", lambda *_: candidate())
    if expected is None:
        with pytest.raises(SubtitleUnavailable, match="burned_in_or_external_subtitles_require_review"):
            provider.verify_english(film, candidate())
    else:
        result = provider.verify_english(film, candidate())
        assert display_name("The Film", "en", result) == expected


def test_attachable_text_is_preferred_over_a_higher_resolution_bitmap_track():
    bitmap = candidate("bitmap", width=3840, subtitles=True)
    bitmap.match_evidence["marketa"]["subtitle_method"] = "original_embedded_bitmap"
    text = candidate("text", width=1920, subtitles=True)
    text.match_evidence["marketa"]["subtitle_method"] = "original_embedded_text"
    assert choose_english([bitmap, text]).source_id == "text"


def test_processing_reconciliation_is_not_starved_by_fresh_films(tmp_path):
    pipeline,first=seed_source(tmp_path,"cz",1,candidate(language="cs"))
    later=replace(first,cr_film_id=10,priority_rank=10)
    pipeline._record_verified_source(later,candidate("456",language="cs"),"Later CZ Dabing")
    pipeline.name_builder=lambda film,_: f"Film {film.cr_film_id} CZ Dabing"
    pipeline.state.record_plan(10,{"selected":{"source_id":"456"},"display_name":"Later CZ Dabing"})
    pipeline.state.record_prepared(10,"777",100)
    plans=ordered_plans({"cz":pipeline},[first,later],2)
    assert [row["film"]["cr_film_id"] for _,row in plans]==[10,1]


@pytest.mark.parametrize("english,original,expected", [
    ("The Shawshank Redemption", "The Shawshank Redemption", "The Shawshank Redemption"),
    ("Goodfellas", "GoodFellas", "Goodfellas"),
    ("The Film", "  the   film  ", "The Film"),
    ("Seven", "Se7en", "Seven (Se7en)"),
    ("Spirited Away", "千と千尋の神隠し", "Spirited Away (千と千尋の神隠し)"),
    ("The Film", None, "The Film"),
])
def test_combined_names_preserve_both_database_values_without_duplicates(english, original, expected):
    assert combined_title(english, original) == expected


def test_all_variants_use_both_titles_before_language_suffix():
    title = combined_title("The Good, the Bad and the Ugly", "Il buono, il brutto, il cattivo")
    assert display_name(title, "cz", candidate(language="cs")) == title + " CZ Dabing"
    assert display_name(title, "en", candidate(subtitles=True)) == title + " CZ Titulky"
    assert display_name(title, "en", candidate()) == title


def test_display_names_do_not_replace_individual_search_names(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/english-titles.json").write_text(json.dumps({"1": {"title": "English Film"}}))
    titles = EnglishTitles(tmp_path)
    rows = [{"cr_film_id": 1, "title": "Cesky Film", "original_title": "Original Film", "year": 2000, "runtime_min": 120}]
    films, names = named_films(rows, titles)
    assert names[1] == "English Film (Original Film)"
    assert films[0].original_title == "Original Film"
    queries = []
    provider = EnglishProvider(None, None, english_titles={1: titles.get(rows[0])})
    monkeypatch.setattr(provider, "search_by_quality", lambda query: queries.append(query) or [])
    assert provider.discover(films[0]) == []
    assert {"English Film", "Original Film", "Cesky Film"}.issubset(queries)
    assert not any("English Film (Original Film)" in query for query in queries)
    for title in ("English Film 2000", "Original Film 2000", "Cesky Film 2000"):
        assert provider._match(films[0], title, 7200).tier in {MatchTier.STRONG, MatchTier.SOLID}
    assert provider._match(films[0], "Original Film 2001", 7200).tier == MatchTier.REJECT


@pytest.mark.parametrize("response_id,response_imdb,accepted", [(12,"tt0000012",True),(13,"tt0000012",False),(12,"tt0000013",False)])
def test_missing_original_is_filled_only_from_matching_database_identity(tmp_path, monkeypatch, response_id, response_imdb, accepted):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/english-titles.json").write_text(json.dumps({"1": {"title": "English Film"}}))
    monkeypatch.setenv("TMDB_API_KEY", "test-secret")
    data = {"id": response_id, "imdb_id": response_imdb, "original_title": "Original Film"}
    session = SimpleNamespace(headers={}, get=lambda *_args, **_kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: data))
    titles = EnglishTitles(tmp_path, session=session)
    film = {"cr_film_id": 1, "tmdb_id": 12, "imdb_id": "tt0000012"}
    assert titles.fill_originals([film], 10) == int(accepted)
    assert titles.original(film) == ("Original Film" if accepted else None)
    assert "test-secret" not in titles.path.read_text()
    if accepted:
        assert titles.rows["1"]["original_source"] == "https://www.themoviedb.org/movie/12"
        assert titles.original({**film, "original_title": "Exported Original"}) == "Exported Original"


class FakeClock:
    def __init__(self):
        self.seconds = 0
        self.waits = []

    def __call__(self):
        return self.seconds

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds


@pytest.mark.parametrize("continuous,expected", [(False, [1]), (True, [1, 2, 3])])
def test_continuous_upload_drains_more_than_one_batch(tmp_path, monkeypatch, continuous, expected):
    pipeline, first = seed_source(tmp_path, "cz", 1, candidate(language="cs"))
    films = [replace(first, cr_film_id=i, priority_rank=i) for i in [1, 2, 3]]
    pipeline.name_builder = lambda film, _: f"Film {film.cr_film_id} CZ Dabing"
    for film in films[1:]:
        pipeline._record_verified_source(film, candidate(str(100 + film.cr_film_id), language="cs"), "Film")
    executed = []
    clock = FakeClock()
    def execute(plan):
        film_id = plan[0]["film"]["cr_film_id"]
        executed.append(film_id)
        pipeline.state.record_success(film_id, {"target_video_id": str(700 + film_id)})
        clock.seconds += 1
    monkeypatch.setattr(pipeline, "execute", execute)
    counts = drain_queue(tmp_path, {"cz": pipeline}, films, limit=1, runtime_minutes=1,
                         continuous=continuous, clock=clock, sleep=clock.sleep)
    assert executed == expected
    assert counts["uploaded"] == len(expected)
    assert counts["failed"] == 0


def test_continuous_upload_waits_for_newly_prepared_sources(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, "cz", {1: "The Film"})
    film = Film(1, "film", "Film", "The Film", 2000, 120, "en", priority_rank=1)
    clock = FakeClock()
    refreshed = []
    def refresh():
        refreshed.append(clock.seconds)
        if clock.seconds >= 30 and not pipeline.selected_sources.candidate(1):
            pipeline._record_verified_source(film, candidate(language="cs"), "The Film CZ Dabing")
        return [film]
    monkeypatch.setattr(pipeline, "execute", lambda _: pipeline.state.record_success(1, {"target_video_id": "777"}))
    counts = drain_queue(tmp_path, {"cz": pipeline}, [film], limit=1, runtime_minutes=1,
                         continuous=True, refresh=refresh, clock=clock, sleep=clock.sleep)
    assert counts["uploaded"] == 1
    assert refreshed[:2] == [0, 30]
    assert clock.seconds == 60


def test_producer_and_uploader_checkpoint_files_do_not_overlap(tmp_path):
    producer = set(checkpoint_paths(tmp_path, "prepare"))
    uploader = set(checkpoint_paths(tmp_path, "continuous"))
    assert producer.isdisjoint(uploader)
    assert tmp_path / "state/english-titles.json" in producer
    assert tmp_path / "variants/cz/state/sync.json" in uploader
    assert tmp_path / "variants/en/plans/subtitle-followup.jsonl" in uploader


@pytest.mark.parametrize("original,tag,detected,accepted", [
    ("ja", "jpn", "ja", True), ("de", "ger", "de", True),
    ("ja", "eng", "en", True), ("ja", "ger", "de", False),
    (None, "ger", "de", False), ("ja", "ces", "cs", False),
])
def test_original_audio_is_verified_against_the_movie_language(monkeypatch, original, tag, detected, accepted):
    film = Film(1, "film", "The Film", "The Film", 2000, 120, original)
    detector = SimpleNamespace(detect_consensus=lambda *args, **kwargs: (detected, 0.99))
    payload = {"format": {"duration": "7200"}, "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264"},
        {"codec_type": "audio", "tags": {"language": tag}},
        {"codec_type": "subtitle", "index": 2, "codec_name": "subrip", "tags": {"language": "ces"}},
    ]}
    provider = EnglishProvider(None, detector, english_titles={1: "The Film"}, probe=lambda _: payload)
    monkeypatch.setattr(provider, "_get", lambda _: SimpleNamespace(text="<html></html>"))
    monkeypatch.setattr("marketa_sync.discovery.parse_detail_html", lambda *_: candidate())
    result = provider.verify_english(film, candidate())
    assert (result is not None) == accepted
    if accepted:
        assert result.audio_language == detected
        assert result.match_evidence["marketa"]["policy"] == (LEGACY_POLICY if detected == "en" else POLICY)
        assert display_name("The Film", "en", result) == "The Film CZ Titulky"
        assert choose_english([result]) is result


def test_broken_candidate_does_not_prevent_verifying_and_resuming_later_candidates(tmp_path, monkeypatch):
    from sdilej_to_prehrajto.sdilej import SdilejError
    film = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    cache_path = tmp_path / "cache.json"
    provider = EnglishProvider(None, None, english_titles={1: "The Film"}, cache_path=cache_path)
    broken, good = candidate("broken", width=3840), candidate("good")
    monkeypatch.setattr(provider, "search_by_quality", lambda _: [broken, good])
    calls = []
    def verify(_film, item):
        calls.append(item.source_id)
        if item.source_id == "broken":
            raise SdilejError("unavailable https://example.test/private-token")
        item.match_evidence["marketa"]["visual_policy"] = "burned-in-czech-v1"
        return item
    monkeypatch.setattr(provider, "verify_english", verify)
    with pytest.raises(SdilejError):
        provider.discover(film)
    assert calls == ["broken", "good"]
    assert "private-token" not in cache_path.read_text()
    assert provider.cache["1"]["good"]["candidate"]["source_id"] == "good"
    calls.clear()
    with pytest.raises(SdilejError):
        provider.discover(film)
    assert calls == []
    provider.cache["1"]["broken"]["retry_after"] = "2000-01-01T00:00:00+00:00"
    monkeypatch.setattr(provider, "verify_english", lambda *_: None)
    assert provider.discover(film)[0].source_id == "good"


def test_verified_subtitled_source_remains_usable_when_another_candidate_fails(monkeypatch):
    from sdilej_to_prehrajto.sdilej import SdilejError
    film = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    provider = EnglishProvider(None, None, english_titles={1: "The Film"})
    broken, good = candidate("broken", width=3840), candidate("good", subtitles=True)
    monkeypatch.setattr(provider, "search_by_quality", lambda _: [broken, good])
    def verify(_film, item):
        if item.source_id == "broken":
            raise SdilejError("unavailable")
        return item
    monkeypatch.setattr(provider, "verify_english", verify)
    assert provider.discover(film)[0].source_id == "good"


def test_cached_subtitles_remain_available_when_new_verification_exhausts_the_deadline(monkeypatch):
    from sdilej_to_prehrajto.sdilej import SdilejError
    clock = FakeClock()
    monkeypatch.setattr("marketa_sync.discovery.time.monotonic", clock)
    film = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    provider = EnglishProvider(None, None, english_titles={1: "The Film"}, discovery_timeout_seconds=1)
    broken, good = candidate("broken", width=3840), candidate("good", subtitles=True)
    provider.cache = {"1": {"good": {"policy": POLICY, "original_language": "en", "candidate": good.to_dict()}}}
    monkeypatch.setattr(provider, "search_by_quality", lambda _: [broken, good])
    def verify(*_):
        clock.seconds = 2
        raise SdilejError("unavailable")
    monkeypatch.setattr(provider, "verify_english", verify)
    assert provider.discover(film)[0].source_id == "good"


def test_original_language_metadata_is_filled_even_when_original_title_exists(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/english-titles.json").write_text(json.dumps({"1": {"title": "The Film"}}))
    monkeypatch.setenv("TMDB_API_KEY", "test-secret")
    data = {"id": 12, "imdb_id": "tt0000012", "original_title": "Original Film", "original_language": "ja"}
    session = SimpleNamespace(headers={}, get=lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: data))
    titles = EnglishTitles(tmp_path, session=session)
    film = {"cr_film_id": 1, "title": "The Film", "original_title": "Original Film", "tmdb_id": 12, "imdb_id": "tt0000012"}
    assert titles.fill_originals([film], 10) == 1
    assert named_films([film], titles)[0][0].original_language == "ja"
    assert titles.fill_originals([film], 10) == 0


@pytest.mark.parametrize("limit,expected", [(2, [3, 4]), (7, [3, 4, 5, 6, 1, 7, 2])])
def test_preparation_balances_fresh_films_and_resumable_early_failures(tmp_path, monkeypatch, limit, expected):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/english-titles.json").write_text('{}')
    state_path = tmp_path / "variants/en/state/discovery.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({str(i): {"checked_at": "2000-01-01T00:00:00+00:00", "failures": 9} for i in (1, 2)}))
    first = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    films = [replace(first, cr_film_id=i) for i in range(1, 8)]
    calls = []
    monkeypatch.setattr("marketa_sync.cli.source_session", lambda: None)
    monkeypatch.setattr(EnglishProvider, "discover", lambda self, film: calls.append(film.cr_film_id) or [])
    prepare(tmp_path, films, {i: "The Film" for i in range(1, 8)}, limit, None, 40)
    assert calls == expected


def test_existing_verified_english_source_remains_uploadable_after_policy_upgrade(tmp_path):
    pipeline, film = seed_source(tmp_path, "en", 1, candidate(subtitles=True))
    row = pipeline.selected_sources.get(1)
    row["match_evidence"]["marketa"]["policy"] = LEGACY_POLICY
    pipeline.selected_sources.record(row)
    assert pipeline.selected_sources.candidate(1).source_id == "123"


def test_both_variants_can_transfer_concurrently_with_private_sessions(tmp_path, monkeypatch):
    cz, film = seed_source(tmp_path, "cz", 1, candidate(language="cs"))
    en, _ = seed_source(tmp_path, "en", 1, candidate(subtitles=True))
    barrier = threading.Barrier(2)
    sessions = []
    lock = threading.Lock()
    for variant, pipeline in (("cz", cz), ("en", en)):
        def execute(plan, *, session_pairs, pipeline=pipeline, variant=variant):
            with lock:
                sessions.append((variant, session_pairs[0]))
            barrier.wait(timeout=3)
            pipeline.state.record_success(1, {"target_video_id": variant + "777"})
        monkeypatch.setattr(pipeline, "execute", execute)
    counts = drain_queue(tmp_path, {"cz": cz, "en": en}, [film], limit=2, runtime_minutes=1,
                         session_pairs=[("source-1", "target-1"), ("source-2", "target-2")])
    assert counts["uploaded"] == 2
    assert {pair for _, pair in sessions} == {("source-1", "target-1"), ("source-2", "target-2")}


def test_fast_slot_refills_before_a_slow_transfer_finishes(tmp_path, monkeypatch):
    pipeline, first = seed_source(tmp_path, "cz", 1, candidate(language="cs"))
    films = [replace(first, cr_film_id=i, priority_rank=i) for i in (1, 2, 3)]
    pipeline.name_builder = lambda film, _: f"Film {film.cr_film_id} CZ Dabing"
    for film in films[1:]:
        pipeline._record_verified_source(film, candidate(str(film.cr_film_id), language="cs"), "Film")
    third_started = threading.Event()
    lock = threading.Lock()
    calls, completed, sessions = [], [], {}
    active = maximum_active = 0
    def execute(plan, *, session_pairs):
        nonlocal active, maximum_active
        film_id = plan[0]["film"]["cr_film_id"]
        with lock:
            calls.append(film_id)
            sessions[film_id] = session_pairs[0]
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if film_id == 1:
                assert third_started.wait(3), "A slow transfer blocked the free slot"
            elif film_id == 3:
                third_started.set()
            pipeline.state.record_success(film_id, {"target_video_id": str(700 + film_id)})
            with lock:
                completed.append(film_id)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(pipeline, "execute", execute)
    counts = drain_queue(tmp_path, {"cz": pipeline}, films, limit=3, runtime_minutes=1,
                         session_pairs=[("source-1", "target-1"), ("source-2", "target-2")])
    assert sorted(calls) == [1, 2, 3]
    assert maximum_active == 2
    assert completed.index(2) < completed.index(1)
    assert sessions[2] == sessions[3] != sessions[1]
    assert counts["uploaded"] == 3


def test_idle_slot_polls_new_sources_while_another_transfer_is_running(tmp_path, monkeypatch):
    pipeline, first = seed_source(tmp_path, "cz", 1, candidate(language="cs"))
    second = replace(first, cr_film_id=2, priority_rank=2)
    pipeline.name_builder = lambda film, _: f"Film {film.cr_film_id} CZ Dabing"
    first_started, second_started = threading.Event(), threading.Event()
    calls = []
    def refresh():
        if first_started.is_set() and not pipeline.selected_sources.candidate(2):
            pipeline._record_verified_source(second, candidate("456", language="cs"), "Film 2 CZ Dabing")
        return [first, second]
    def execute(plan, *, session_pairs):
        film_id = plan[0]["film"]["cr_film_id"]
        calls.append(film_id)
        if film_id == 1:
            first_started.set()
            assert second_started.wait(3), "New sources were not polled during the slow transfer"
        else:
            second_started.set()
        pipeline.state.record_success(film_id, {"target_video_id": str(700 + film_id)})
    monkeypatch.setattr(pipeline, "execute", execute)
    counts = drain_queue(tmp_path, {"cz": pipeline}, [first, second], limit=2, runtime_minutes=1,
                         refresh=refresh, poll_interval_seconds=0.01,
                         session_pairs=[("source-1", "target-1"), ("source-2", "target-2")])
    assert sorted(calls) == [1, 2]
    assert counts["uploaded"] == 2


def test_variant_and_subtitle_files_cannot_change_during_another_checkpoint(tmp_path):
    lock = threading.RLock()
    publishing, release = threading.Event(), threading.Event()
    en_attempted, subtitles_attempted = threading.Event(), threading.Event()
    def persist(path, event):
        if path.parts[-3] == "cz" and event == "prepared":
            publishing.set()
            assert release.wait(3)
    cz = make_pipeline(tmp_path, "cz", {1: "The Film"}, persist=persist, checkpoint_lock=lock)
    en = make_pipeline(tmp_path, "en", {1: "The Film"}, persist=persist, checkpoint_lock=lock)
    en.state.save("initial")
    lane = tmp_path / "variants/en"
    subtitles = Backfill(None, None, lane, lane / "state/subtitle-backfill.json",
                         tmp_path / "reports/subtitle-backfill.md", checkpoint_lock=lock)
    def save_en():
        en_attempted.set()
        en.state.record_prepared(1, "222", 100)
    def save_subtitles():
        subtitles_attempted.set()
        subtitles.save()
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(cz.state.record_prepared, 1, "111", 100)
        assert publishing.wait(3)
        second = executor.submit(save_en)
        third = executor.submit(save_subtitles)
        try:
            assert en_attempted.wait(3) and subtitles_attempted.wait(3)
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.05)
            with pytest.raises(FutureTimeout):
                third.result(timeout=0.05)
            assert json.loads(en.state.path.read_text())["films"] == {}
            assert not subtitles.state_path.exists()
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
        third.result(timeout=3)
    assert json.loads(cz.state.path.read_text())["films"]["1"]["prepared"]["target_video_id"] == "111"
    assert json.loads(en.state.path.read_text())["films"]["1"]["prepared"]["target_video_id"] == "222"
    assert subtitles.state_path.is_file()


def test_subtitle_polling_starts_immediately_and_repeats_without_any_uploads(tmp_path):
    pipeline = make_pipeline(tmp_path, "cz", {})
    clock = FakeClock()
    calls = []
    counts = drain_queue(tmp_path, {"cz": pipeline}, [], limit=3, runtime_minutes=22,
                         continuous=True, subtitles=lambda _: calls.append(clock()),
                         clock=clock, sleep=clock.sleep)
    assert calls == [0, 600, 1200]
    assert counts["planned"] == 0


@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("runtime,drain", [(20, 0), (10, 10)])
def test_subtitles_repeat_while_a_transfer_is_busy_and_recover_after_failure(tmp_path, monkeypatch, fail_first, runtime, drain):
    pipeline, film = seed_source(tmp_path, "cz", 1, candidate(language="cs"))
    clock = FakeClock()
    started, release = threading.Event(), threading.Event()
    calls = []
    def execute(_):
        started.set()
        assert release.wait(3), "Subtitle polling depended on upload completion"
        pipeline.state.record_success(1, {"target_video_id": "777"})
    def subtitles(_):
        assert started.wait(3)
        assert not pipeline.state.uploaded(1)
        calls.append(clock())
        clock.seconds += 600
        if len(calls) == 1 and fail_first:
            raise RuntimeError("temporary subtitle failure")
        if len(calls) == 2:
            release.set()
    monkeypatch.setattr(pipeline, "execute", execute)
    counts = drain_queue(tmp_path, {"cz": pipeline}, [film], limit=1, runtime_minutes=runtime,
                         continuous=True, subtitles=subtitles, poll_interval_seconds=0.01,
                         clock=clock, sleep=clock.sleep, subtitle_drain_minutes=drain)
    assert calls == [0, 600]
    assert counts["uploaded"] == 1


def test_old_unsubtitled_source_is_visually_reviewed_without_reselecting_the_uploaded_file(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/english-titles.json").write_text('{}')
    pipeline, film = seed_source(tmp_path, "en", 1, candidate())
    pipeline.state.record_success(1, {"target_video_id": "777"})
    calls = []
    def verify(self, movie, source):
        calls.append((movie.cr_film_id, source.source_id))
        source.match_evidence["marketa"].update(czech_subtitles=True,
            subtitle_method="burned_in_ocr_verified", visual_policy="burned-in-czech-v1")
        return source
    monkeypatch.setattr("marketa_sync.cli.source_session", lambda: None)
    monkeypatch.setattr(EnglishProvider, "verify_english", verify)
    monkeypatch.setattr(EnglishProvider, "discover", lambda *_: pytest.fail("The uploaded source must not be reselected"))
    prepare(tmp_path, [film], {1: "The Film"}, 1, None, 5)
    stored = VariantSourceStore(tmp_path / "variants/en/manifests/selected-sources.jsonl", "en").candidate(1)
    assert calls == [(1, "123")]
    assert display_name("The Film", "en", stored) == "The Film CZ Titulky"
    assert pipeline.state.snapshot(1)["upload"]["target_video_id"] == "777"


@pytest.mark.parametrize("confirmed_target,old_name,expected", [
    ("777", "The Film", "The Film CZ Titulky"),
    ("wrong", "The Film", "The Film"),
    ("777", "The Film CZ Dabing", "The Film CZ Dabing"),
    ("777", "The Film CZ Titulky", "The Film CZ Titulky"),
])
def test_confirmed_subtitles_rename_only_the_owned_original_and_update_the_live_store(tmp_path, monkeypatch, confirmed_target, old_name, expected):
    pipeline, _ = seed_source(tmp_path, "en", 1, candidate(subtitles=True))
    pipeline.state.record_success(1, {"target_video_id": "777", "display_name": old_name})
    row = {"target_video_id": confirmed_target, "source": {"cr_film_id": 1}}
    rename_calls = []
    confirmations = iter([False, True])
    monkeypatch.setattr("marketa_sync.cli.rename_video", lambda _session, video, name: rename_calls.append((video, name)))
    monkeypatch.setattr("marketa_sync.cli.uploaded_video_confirmed", lambda *_: next(confirmations))
    def backfill(*_args, on_verified, **_kwargs):
        return SimpleNamespace(run=lambda **_: on_verified(row))
    monkeypatch.setattr("marketa_sync.cli.Backfill", backfill)
    run_subtitles(tmp_path, None, None, None, 20, 10, state_store=pipeline.state)
    assert pipeline.state.snapshot(1)["upload"]["display_name"] == expected
    assert StateStore(pipeline.state.path).snapshot(1)["upload"]["display_name"] == expected
    assert rename_calls == ([("777", expected)] if expected != old_name else [])


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("owned", [False, True])
def test_visually_reviewed_video_without_visible_czech_subtitles_loses_only_its_suffix(tmp_path, monkeypatch, prepared, owned):
    pipeline, _ = seed_source(tmp_path, "en", 1, candidate(subtitles=True))
    pipeline.state.record_plan(1, {"selected": {"source_id": "123"}, "display_name": "The Film CZ Titulky"})
    if prepared:
        pipeline.state.record_prepared(1, "777", 100)
    else:
        pipeline.state.record_success(1, {"target_video_id": "777", "display_name": "The Film CZ Titulky"})
    row = {"target_video_id": "777" if owned else "888", "source": {
        "cr_film_id": 1, "visual_subtitle_status": "no_text_detected"}}
    calls = []
    confirmations = iter([False, True])
    monkeypatch.setattr("marketa_sync.cli.rename_video", lambda _session, video, name: calls.append((video, name)))
    monkeypatch.setattr("marketa_sync.cli.uploaded_video_confirmed", lambda *_: next(confirmations))
    monkeypatch.setattr("marketa_sync.cli.Backfill", lambda *args, on_no_subtitles, **kwargs:
                        SimpleNamespace(run=lambda **_: on_no_subtitles(row)))
    run_subtitles(tmp_path, None, None, None, 20, 10, state_store=pipeline.state)
    expected = "The Film" if owned else "The Film CZ Titulky"
    entry = pipeline.state.snapshot(1)
    assert entry["last_plan" if prepared else "upload"]["display_name"] == expected
    assert (entry.get("upload") or entry["prepared"])["target_video_id"] == "777"
    assert bool(entry.get("upload")) is not prepared
    assert calls == ([("777", "The Film")] if owned else [])


def test_cached_bitmap_metadata_cannot_bypass_visual_verification(monkeypatch):
    film = Film(1, "film", "The Film", "The Film", 2000, 120, "en")
    old = candidate("bitmap", subtitles=True)
    old.match_evidence["marketa"].update(subtitle_codec="hdmv_pgs_subtitle", subtitle_attachable=False)
    provider = EnglishProvider(None, None, english_titles={1: "The Film"})
    provider.cache = {"1": {"bitmap": {"policy": POLICY, "original_language": "en", "candidate": old.to_dict()}}}
    monkeypatch.setattr(provider, "search_by_quality", lambda _: [old])
    calls = []
    def verify(_film, source):
        calls.append(source.source_id)
        source.match_evidence["marketa"].update(czech_subtitles=False, visual_policy="burned-in-czech-v1", visual_status="no_text_detected")
        return source
    monkeypatch.setattr(provider, "verify_english", verify)
    selected = provider.discover(film)[0]
    assert calls == ["bitmap"]
    assert display_name("The Film", "en", selected) == "The Film"
