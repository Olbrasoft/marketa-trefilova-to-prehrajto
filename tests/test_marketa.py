import json
from dataclasses import replace
from pathlib import Path

import pytest

from marketa_sync.catalog import EnglishTitles, VariantSourceStore, import_sources, named_films
from marketa_sync.cli import make_pipeline, ordered_plans, followup_report
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
    assert "999" not in store.path.read_text()
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
