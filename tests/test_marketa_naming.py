from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from marketa_sync.cli import make_pipeline, run_subtitles
from marketa_sync.naming import with_resolution, without_resolution, reconcile_resolution_names
from sdilej_to_prehrajto.models import Candidate, Film, LanguageTier
from sdilej_to_prehrajto.prehrajto import PrehrajtoError


@pytest.mark.parametrize("title,width,height,expected", [
    ("The Film CZ Dabing", 1920, 800, "The Film 1080p CZ Dabing"),
    ("The Film CZ Titulky", 3840, 1600, "The Film 4K CZ Titulky"),
    ("The Film", 1280, 536, "The Film 720p"),
    ("The Film (2000) CZ Titulky", 2560, 1440, "The Film (2000) 1440p CZ Titulky"),
    ("The Film 1080p CZ Dabing", 1920, 1080, "The Film 1080p CZ Dabing"),
    ("The Film 720p CZ Titulky", 1920, 1080, "The Film 1080p CZ Titulky"),
    ("The Film CZ Titulky", 7680, 4320, "The Film 8K CZ Titulky"),
    ("The Film CZ Dabing", 0, 0, "The Film CZ Dabing"),
    ("The Film CZ Titulky.mkv", 1920, 800, "The Film 1080p CZ Titulky"),
])
def test_resolution_precedes_the_preserved_language_suffix(title, width, height, expected):
    assert with_resolution(title, width, height) == expected
    assert with_resolution(expected, width, height) == expected


def test_legacy_name_keeps_the_remake_year_and_language_variant():
    assert without_resolution("Film (2000) 1080p CZ Dabing") == "Film (2000) CZ Dabing"
    assert without_resolution("Film (2000) 4K CZ Titulky") == "Film (2000) CZ Titulky"


@pytest.mark.parametrize("processing", [False, True])
def test_owned_legacy_prepared_target_is_not_uploaded_again(tmp_path, monkeypatch, processing):
    source = Candidate("123", "https://sdilej.cz/123/film.mkv", "Film", width=1920, height=800,
                       audio_language="cs", language_tier=LanguageTier.CZECH_AUDIO, size_bytes=100)
    pipeline = make_pipeline(tmp_path, "cz", {1: "The Film"})
    pipeline._selected[1] = source
    pipeline.state.record_plan(1, {"selected": source.to_dict(), "display_name": "The Film CZ Dabing"})
    pipeline.state.record_prepared(1, "777", 100)
    def lookup(_session, name, *, include_processing=True):
        if name != "The Film CZ Dabing" or (processing and not include_processing):
            return None
        return "777"
    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.uploaded_video_id_by_name", lookup)
    renamed = []
    monkeypatch.setattr(pipeline, "_target_completed_and_named", lambda _session, video, name: renamed.append((video, name)) or True)
    relay = Mock(side_effect=AssertionError("The prepared target must never be uploaded again"))
    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.relay_upload", relay)
    film = Film(1, "film", "The Film", None, 2000, 120, "en")
    plan = [{"film": film.to_dict(), "selected": source.to_dict(), "display_name": "The Film 1080p CZ Dabing", "needs_czech_subtitles": False}]
    pipeline.execute(plan)
    saved = pipeline.state.snapshot(1)
    assert (saved.get("prepared") or saved["upload"])["target_video_id"] == "777"
    assert bool(saved.get("upload")) is not processing
    assert renamed == ([] if processing else [("777", "The Film 1080p CZ Dabing")])
    relay.assert_not_called()


def test_untracked_legacy_target_blocks_duplicates_without_inventing_resolution(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, "cz", {1: "The Film"})
    monkeypatch.setattr("sdilej_to_prehrajto.pipeline.uploaded_video_id_by_name", lambda _session, name, **_: "777" if name == "The Film CZ Dabing" else None)
    with pytest.raises(PrehrajtoError, match="verified resolution binding"):
        pipeline._target_id_by_name(None, "The Film 1080p CZ Dabing")


@pytest.mark.parametrize("confirmed", [False, True])
def test_completed_title_migration_updates_only_a_confirmed_owned_target(tmp_path, monkeypatch, confirmed):
    pipeline = make_pipeline(tmp_path, "cz", {1: "The Film"})
    pipeline.state.record_success(1, {"target_video_id": "777", "display_name": "The Film CZ Dabing", "width": 1920, "height": 800, "source_id": "123", "completion_evidence": "relay_completed"})
    def rename(_session, video, name, *, expected):
        assert video == "777" and name == "The Film 1080p CZ Dabing"
        assert expected == "The Film CZ Dabing"
        if not confirmed:
            raise PrehrajtoError("Unconfirmed")
        return "https://prehraj.to/the-film/1234567890abcdef"
    monkeypatch.setattr("marketa_sync.naming.confirm_rename", rename)
    assert reconcile_resolution_names({"cz": pipeline}, None) == int(confirmed)
    saved = pipeline.state.snapshot(1)["upload"]
    assert saved["display_name"] == ("The Film 1080p CZ Dabing" if confirmed else "The Film CZ Dabing")
    assert saved["target_video_id"] == "777" and saved["source_id"] == "123"


def test_adding_subtitle_suffix_keeps_resolution_before_it(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, "en", {1: "The Film"})
    pipeline.state.record_success(1, {"target_video_id": "777", "display_name": "The Film 1080p", "width": 1920, "height": 800})
    row = {"target_video_id": "777", "source": {"cr_film_id": 1}}
    calls = []
    confirmations = iter([False, True])
    monkeypatch.setattr("marketa_sync.cli.rename_video", lambda _session, video, name: calls.append((video, name)))
    monkeypatch.setattr("marketa_sync.cli.uploaded_video_confirmed", lambda *_: next(confirmations))
    monkeypatch.setattr("marketa_sync.cli.Backfill", lambda *_args, on_verified, **_kwargs: SimpleNamespace(run=lambda **_: on_verified(row)))
    monkeypatch.setattr("marketa_sync.cli.followup_report", lambda _: None)
    run_subtitles(tmp_path, None, None, None, 20, 10, state_store=pipeline.state)
    assert calls == [("777", "The Film 1080p CZ Titulky")]
    assert pipeline.state.snapshot(1)["upload"]["display_name"] == "The Film 1080p CZ Titulky"
