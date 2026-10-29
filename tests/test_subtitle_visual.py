import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from marketa_sync.subtitle_visual import classify_samples, inspect_burned_in, readable_lines, run_private
from sdilej_to_prehrajto.subtitle_media import SubtitleUnavailable

CZECH_LINES = [
    "Proč jsi mi to neřekl, když jsem se tě na to ptal?",
    "Tady jsme přece byli včera a ještě tu nikdo nebyl.",
    "Opravdu potřebuju vědět, co se tady vlastně stalo.",
]


def test_czech_requires_multiple_different_readable_observations():
    pytest.importorskip("lingua")
    assert classify_samples([[line] for line in CZECH_LINES])["visual_status"] == "czech_burned_in_verified"
    assert classify_samples([[CZECH_LINES[0]]] * 6)["visual_status"] == "uncertain"
    assert classify_samples([[], [], []])["visual_status"] == "no_text_detected"


@pytest.mark.parametrize("lines", [
    ["Why did you not tell me when I asked you about it?",
     "We were here yesterday and nobody else was around.",
     "I really need to know what happened here last night."],
    ["Prečo si mi to nepovedal, keď som sa ťa na to pýtal?",
     "Tu sme predsa boli včera a ešte tu nikto nebol.",
     "Naozaj potrebujem vedieť, čo sa tu vlastne stalo."],
])
def test_english_and_slovak_cannot_be_named_czech_subtitles(lines):
    pytest.importorskip("lingua")
    assert classify_samples([[line] for line in lines])["visual_status"] != "czech_burned_in_verified"


def test_uncertain_language_is_not_positive_subtitle_evidence():
    detector = SimpleNamespace(compute_language_confidence_values=lambda _: [
        SimpleNamespace(value=0.55), SimpleNamespace(value=0.45)])
    assert classify_samples([[line] for line in CZECH_LINES], detector=detector)["visual_status"] == "uncertain"


def test_ocr_discards_low_confidence_noise():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tconf\ttext\n"
    rows = "".join(f"5\t1\t1\t1\t1\t{confidence}\t{word}\n"
                   for confidence,word in [(95,"Tady"),(96,"jsme"),(94,"včera"),(93,"byli."),(12,"garbage")])
    assert readable_lines(header + rows) == ["Tady jsme včera byli."]


@pytest.mark.parametrize("payload", ["", "unexpected OCR output", "level\ttext\n5\tTady jsme byli\n"])
def test_invalid_ocr_output_is_not_treated_as_a_video_without_text(payload):
    with pytest.raises(SubtitleUnavailable, match="visual_subtitle_ocr_invalid"):
        readable_lines(payload)


def test_inspection_errors_never_expose_signed_source_urls(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(
        ["ffmpeg", "https://private.example/file?token=secret"], 1)))
    with pytest.raises(SubtitleUnavailable, match="^visual_subtitle_runtime_unavailable$"):
        run_private(["ffmpeg"], 1)


@pytest.mark.parametrize("readings,expected", [
    (["noise", *CZECH_LINES, "empty", "empty"], "czech_burned_in_verified"),
    (["noise", "empty", "empty", "empty", "empty", "empty"], "uncertain"),
    (["noise"] * 6, "uncertain"),
])
def test_one_noisy_scene_does_not_abort_the_other_frames_or_prove_absence(monkeypatch, readings, expected):
    pytest.importorskip("lingua")
    samples = iter(readings)
    calls = []
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tconf\ttext\n"
    def run(arguments, timeout):
        calls.append(arguments[0])
        if arguments[0] == "ffmpeg":
            Path(arguments[-1]).write_bytes(b"private test frame")
            return b""
        text = next(samples)
        if text == "empty":
            return header.encode()
        if text == "noise":
            return (header + "5\t1\t1\t1\t1\t12\ttexture\n").encode()
        return (header + "".join(f"5\t1\t1\t1\t1\t95\t{word}\n" for word in text.split())).encode()
    monkeypatch.setattr("marketa_sync.subtitle_visual.run_private", run)
    result = inspect_burned_in("https://private.example/file?token=secret", 7200)
    assert result["visual_status"] == expected
    assert result["visual_samples"] == 6
    assert result["visual_uncertain_samples"] == readings.count("noise")
    assert calls.count("ffmpeg") == calls.count("tesseract") == 6
    assert "secret" not in str(result) and "texture" not in str(result)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("tesseract"), reason="media/OCR tools unavailable")
@pytest.mark.parametrize("background", ["color=black:s=1280x720:r=1:d=12", "testsrc2=s=1280x720:r=1:d=12"])
def test_real_video_pixels_distinguish_burned_in_from_a_separate_text_track(tmp_path, background):
    pytest.importorskip("lingua")
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=True).stdout
    if "ces" not in languages.splitlines():
        pytest.skip("Czech OCR model unavailable")
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font.exists():
        pytest.skip("Test font unavailable")
    filters = []
    for index,line in enumerate(CZECH_LINES):
        text = tmp_path / f"line-{index}.txt"
        text.write_text(line)
        filters.append(f"drawtext=fontfile={font}:textfile={text}:fontcolor=white:fontsize=30:borderw=2:bordercolor=black:"
                       f"x=(w-text_w)/2:y=h-100:enable='between(t,{index*4},{index*4+3.9})'")
    burned = tmp_path / "burned.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", background,
                    "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "ultrafast", str(burned)],
                   capture_output=True, check=True, timeout=30)
    evidence = inspect_burned_in(str(burned), 12)
    assert evidence["visual_status"] == "czech_burned_in_verified"
    assert evidence["visual_samples"] == 6
    assert "Proč" not in str(evidence)

    subtitle = tmp_path / "cs.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:12,000\n" + CZECH_LINES[0] + "\n")
    separate = tmp_path / "separate.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=1280x720:r=1:d=12",
                    "-i", str(subtitle), "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "srt", str(separate)],
                   capture_output=True, check=True, timeout=30)
    assert inspect_burned_in(str(separate), 12)["visual_status"] == "no_text_detected"
