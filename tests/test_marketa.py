import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from marketa_sync.catalog import EnglishTitles, VariantSourceStore, combined_title, import_sources, named_films
from marketa_sync.cli import make_pipeline, ordered_plans, followup_report, drain_queue, checkpoint_paths
from marketa_sync.discovery import choose_english, display_name, POLICY, EnglishProvider
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier, MatchTier
from sdilej_to_prehrajto.prehrajto import _uploaded_video_id_from_html, login, PrehrajtoError
from sdilej_to_prehrajto.state import StateStore
from sdilej_to_prehrajto.subtitle_backfill import source_map


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


@pytest.mark.parametrize("codec", ["subrip", "hdmv_pgs_subtitle"])
def test_original_czech_tracks_are_preferred_even_when_manual_conversion_is_needed(monkeypatch, codec):
    film=Film(1,"film","The Film","The Film",2000,120,"en")
    detector=SimpleNamespace(detect_consensus=lambda *_args,**_kwargs: ("en",0.99))
    payload={"format":{"duration":"7200"},"streams":[
        {"codec_type":"video","width":1920,"height":1080,"codec_name":"h264"},
        {"codec_type":"audio","tags":{"language":"eng"}},
        {"codec_type":"subtitle","index":2,"codec_name":codec,"tags":{"language":"ces"}},
    ]}
    provider=EnglishProvider(None,detector,english_titles={1:"The Film"},probe=lambda _: payload)
    monkeypatch.setattr(provider,"_get",lambda _: SimpleNamespace(text="<html></html>"))
    monkeypatch.setattr("marketa_sync.discovery.parse_detail_html",lambda *_: candidate())
    result=provider.verify_english(film,candidate())
    assert result.match_evidence["marketa"]["czech_subtitles"] is True
    assert result.match_evidence["marketa"]["subtitle_codec"]==codec


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
