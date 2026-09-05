#!/usr/bin/env python3

""" Text-to-speech using the Piper TTS system """

import argparse
import subprocess
import tempfile
from pathlib import Path

from trackbot_audio import DEFAULT_PLAYER_CMD as PLAYER_CMD, PLAYBACK_VOLUME

VOICES_DIR = Path("~/piper-voices").expanduser()
DEFAULT_VOICE = "hfc_male"
DEFAULT_LANG = "en_US"
DEFAULT_QUALITY = "medium"


def _voice_model_path(voice, lang=DEFAULT_LANG, quality=DEFAULT_QUALITY):
    """Path to a Piper voice model, matching the piper-voices repo's layout:
    <lang-prefix>/<lang>/<voice>/<quality>/<lang>-<voice>-<quality>.onnx
    (e.g. voice="ryan" -> en/en_US/ryan/medium/en_US-ryan-medium.onnx)."""
    lang_prefix = lang.split("_")[0]
    name = f"{lang}-{voice}-{quality}"
    return VOICES_DIR / lang_prefix / lang / voice / quality / f"{name}.onnx"


def speak(text, voice=DEFAULT_VOICE, output_file=None):
    """Synthesize text with Piper and play it, blocking until playback finishes.
    If output_file is given, the synthesized audio is saved there permanently;
    otherwise it's written to a temp file that's deleted after playback."""
    model_path = _voice_model_path(voice)
    if not model_path.is_file():
        raise FileNotFoundError(f"Piper voice model not found: {model_path}")

    if output_file is not None:
        wav_path = Path(output_file)
        keep_file = True
    else:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_file:
            wav_path = Path(wav_file.name)
        keep_file = False

    print(f"Saving audio to {wav_path}")

    try:
        # input=text pipes the text to Piper's stdin directly -- no shell, so no
        # quoting/escaping to get wrong (and no shell-injection risk from text).
        subprocess.run(
            ["piper", "--model", str(model_path), "--output-file", str(wav_path)],
            input=text, text=True, check=True,
        )
        subprocess.run([PLAYER_CMD, "--volume", str(PLAYBACK_VOLUME), str(wav_path)], check=True)
    finally:
        if not keep_file:
            wav_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Text-to-speech using Piper TTS")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                         help=f"Piper voice/speaker name, e.g. 'ryan' (default: {DEFAULT_VOICE})")
    parser.add_argument("--output",
                         help="Save the synthesized audio to this file (default: a temp file, deleted after playback)")
    parser.add_argument("--input",
                         help="Read the text to speak from this file instead of the built-in test phrase")
    parser.add_argument("--say",
                         help="Text to speak, given directly on the command line "
                              "(takes precedence over --input)")
    args = parser.parse_args()

    if args.say:
        spoken_text = args.say
    elif args.input:
        spoken_text = Path(args.input).read_text()
    else:
        spoken_text = f"Testing the Python implementation of Piper T T S using {args.voice}."

    speak(spoken_text, voice=args.voice, output_file=args.output)
