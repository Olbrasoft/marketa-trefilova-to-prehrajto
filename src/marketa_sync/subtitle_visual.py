"""Conservative detection of Czech text burned into original video pixels.

OCR is evidence for naming only. It never creates a subtitle file. Failure
to see text in sparse samples is not proof that the whole film has none.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from sdilej_to_prehrajto.subtitle_media import SubtitleUnavailable, extraction_timeout

VISUAL_POLICY = "burned-in-czech-v1"
SAMPLE_FRACTIONS = (0.12, 0.26, 0.40, 0.54, 0.68, 0.82)
CZECH_WORDS = frozenset("jsem jsi jsme jste tady teď ještě proč něco někdo nikdo řekl říká můžu můžeš můžeme chci nechci přece všechno vůbec opravdu dobře protože kdyby který která třeba vědět přijde měl měla musíš potřebuju".split())


def run_private(arguments, timeout):
    try:
        result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise SubtitleUnavailable("visual_subtitle_runtime_unavailable") from None
    if result.returncode:
        raise SubtitleUnavailable("visual_subtitle_inspection_failed")
    return result.stdout


def readable_lines(tsv: str) -> list[str]:
    lines = {}
    saw_text = False
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    required = {"level", "page_num", "block_num", "par_num", "line_num", "conf", "text"}
    if not required.issubset(reader.fieldnames or []):
        raise SubtitleUnavailable("visual_subtitle_ocr_invalid")
    for word in reader:
        try:
            if int(word["level"]) != 5:
                continue
            text = word["text"].strip()
            if not re.search(r"[^\W\d_]", text):
                continue
            saw_text = True
            if float(word["conf"]) < 70:
                continue
            key = tuple(word[field] for field in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(text)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SubtitleUnavailable("visual_subtitle_ocr_invalid") from None
    result = [" ".join(words) for words in lines.values() if len(words) >= 3]
    # Credit lists and large blocks of scene text are not subtitle evidence.
    readable = [line for line in result if len(line) >= 16] if len(result) <= 3 else []
    if saw_text and not readable:
        raise SubtitleUnavailable("visual_subtitle_ocr_uncertain")
    return readable


@lru_cache(maxsize=1)
def language_detector():
    try:
        from lingua import LanguageDetectorBuilder
    except ImportError:
        raise SubtitleUnavailable("visual_subtitle_runtime_unavailable") from None
    return LanguageDetectorBuilder.from_all_languages_with_latin_script().build()


def classify_samples(samples: list[list[str]], *, detector=None) -> dict:
    unique = list(dict.fromkeys(line.casefold() for lines in samples for line in lines))
    evidence = {"visual_policy": VISUAL_POLICY, "visual_samples": len(samples),
                "visual_text_samples": sum(bool(lines) for lines in samples),
                "visual_distinct_lines": len(unique)}
    if not unique:
        return {**evidence, "visual_status": "no_text_detected"}
    text = " ".join(unique)
    if (len(unique) < 3 or evidence["visual_text_samples"] < 3
            or len(re.sub(r"\W", "", text)) < 90):
        return {**evidence, "visual_status": "uncertain"}
    values = (detector or language_detector()).compute_language_confidence_values(text)
    if not values or values[0].value < 0.9 or len(values) < 2 or values[0].value - values[1].value < 0.2:
        return {**evidence, "visual_status": "uncertain"}
    language = values[0].language.iso_code_639_1.name.lower()
    evidence.update(visual_language=language, visual_language_confidence=round(values[0].value, 4))
    if language != "cs":
        return {**evidence, "visual_status": "foreign_text_detected"}
    if len(set(re.findall(r"[^\W\d_]+", text)) & CZECH_WORDS) < 3:
        return {**evidence, "visual_status": "uncertain"}
    return {**evidence, "visual_status": "czech_burned_in_verified"}


def inspect_burned_in(url: str, duration_sec: int, *, deadline=None) -> dict:
    if duration_sec <= 0:
        raise SubtitleUnavailable("visual_subtitle_duration_unknown")
    if deadline is None:
        deadline = time.monotonic() + 180
    samples, hashes, positions, uncertain_samples = [], [], [], 0
    with tempfile.TemporaryDirectory(prefix="subtitle-visual-") as directory:
        for fraction in SAMPLE_FRACTIONS:
            position = round(duration_sec * fraction, 3)
            image = Path(directory) / "frame.png"
            # Decode video pixels only. Never overlay embedded text/PGS tracks:
            # a positive observation must prove that subtitles are burned in.
            run_private([
                "ffmpeg", "-nostdin", "-v", "error", "-rw_timeout", "20000000",
                "-ss", str(position), "-i", url, "-an", "-sn", "-dn",
                "-frames:v", "1", "-vf", "crop=iw:ih*0.45:0:ih*0.55,scale=1920:-2",
                "-threads", "1", "-update", "1", "-y", str(image),
            ], extraction_timeout(30, deadline))
            if not image.is_file():
                raise SubtitleUnavailable("visual_subtitle_inspection_failed")
            hashes.append(hashlib.sha256(image.read_bytes()).hexdigest())
            tsv = run_private(["tesseract", str(image), "stdout", "-l", "ces+eng",
                               "--oem", "1", "--psm", "6", "tsv"], extraction_timeout(15, deadline))
            try:
                samples.append(readable_lines(tsv.decode("utf-8")))
            except SubtitleUnavailable as error:
                if str(error) != "visual_subtitle_ocr_uncertain":
                    raise
                # Scene texture can resemble illegible text. Keep collecting
                # independent observations rather than abandoning the film.
                samples.append([])
                uncertain_samples += 1
            positions.append(position)
    # Persist counts and hashes, never signed URLs, images or dialogue text.
    result = classify_samples(samples)
    if uncertain_samples and result["visual_status"] != "czech_burned_in_verified":
        # A noisy observation must never become evidence of subtitle absence.
        result["visual_status"] = "uncertain"
    return {**result, "visual_uncertain_samples": uncertain_samples,
            "visual_sample_seconds": positions, "visual_frame_sha256": hashes}
