"""Run transcription on audio files."""
from __future__ import annotations

import json
import os
import traceback

import tqdm
import whisper

from zeno_build.cache_utils import CacheLock, fail_cache, get_cache_path


def make_predictions(
    audio_paths: list[str],
    model_name: str,
    output_dir: str = "results",
) -> list[str] | None:
    """Make predictions over a particular dataset."""
    # Load from cache if existing
    file_root = get_cache_path(output_dir, {"model_preset": model_name})
    if os.path.exists(f"{file_root}.json"):
        with open(f"{file_root}.json", "r") as f:
            return json.load(f)

    with CacheLock(file_root) as cache_lock:
        # If the cache is locked, then another process is already generating
        # so just skip this one
        if not cache_lock:
            return None
        # Make predictions
        try:
            predictions: list[str] = transcribe_audio(model_name, audio_paths)
        except Exception:
            tb = traceback.format_exc()
            fail_cache(file_root, tb)
            raise

        # Dump the predictions
        with open(f"{file_root}.json", "w") as f:
            json.dump(predictions, f)

    return predictions


def transcribe_audio(model_name: str, audio_paths: list[str]) -> list[str]:
    """Transcribe audio files using a given Whisper model.

    Args:
        model_name (str): Whisper model name
        audio_paths (list[str]): List of audio file paths

    Returns:
        list[str]: Output transcriptions.
    """
    model = whisper.load_model(model_name)

    outs: list[str] = []
    for i in tqdm.trange(0, len(audio_paths)):
        outs.append(model.transcribe(audio_paths[i])["text"])

    return outs
