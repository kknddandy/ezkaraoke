import os

# Must be set before any test module imports PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Keep the suite silent: PlayerController builds its libvlc instance with a
# dummy audio output (see ezkaraoke/player.py), so test media — e.g. the
# ffmpeg-generated 440 Hz sine used by test_player_queue — is never played
# through the machine's real speakers.
os.environ.setdefault("EZKARAOKE_DUMMY_AUDIO", "1")
