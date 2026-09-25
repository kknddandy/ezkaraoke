"""Tests for the microphone mixer: DSP chain, device introspection and the
fake-stream lifecycle (no real audio device is ever opened)."""

import numpy as np
import pytest
from scipy.signal import lfilter

import ezkaraoke.mic_mixer as mic_mixer
from ezkaraoke.mic_mixer import MicMixer, available, list_input_devices

FS = 48000
BLOCK = 1024
DELAY = int(FS * 0.130)  # 6240 samples @ 48 kHz
TARGET = 10.0 ** (12.0 / 20.0)  # +12 dB as an amplitude ratio


def _sine(freq: float, seconds: float, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(FS * seconds), dtype=np.float64) / FS
    return (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(np.asarray(x, dtype=np.float64)))))


# ---------------------------------------------------------------------- dsp

def test_gain_db_scales_amplitude():
    mixer = MicMixer()
    x = _sine(440.0, 1.0)  # 0 dB shelves are exactly identity
    mixer.set_gain_db(6.0)
    y = mixer._process_samples(x)
    # The chain's 80 Hz HPF passes 440 Hz at -0.096 dB, so allow a few
    # percent around the exact 6 dB gain.
    assert _rms(y) == pytest.approx(_rms(x) * 10.0 ** (6.0 / 20.0), rel=2e-2)
    assert y.dtype == np.float32


def test_bass_boost_raises_rms_at_80_hz():
    mixer = MicMixer()
    x = _sine(80.0, 0.5)
    mixer.set_bass_db(12.0)
    y = mixer._process_samples(x)
    assert _rms(y) > _rms(x)


def test_treble_boost_raises_rms_at_10_khz():
    mixer = MicMixer()
    x = _sine(10000.0, 0.5)
    mixer.set_treble_db(12.0)
    y = mixer._process_samples(x)
    assert _rms(y) > _rms(x)


def test_echo_produces_delayed_copy():
    """An impulse at t=0 re-appears at t=DELAY with the comb feedback."""
    mixer = MicMixer()
    mixer.set_echo(0.5)  # -> feedback 0.3
    assert mixer._echo_fb == pytest.approx(0.3)
    x0 = np.zeros(BLOCK, dtype=np.float32)
    x0[0] = 1.0
    y0 = mixer._process_samples(x0)
    assert y0[0] == pytest.approx(1.0, abs=1e-5)
    peaks = [float(np.max(np.abs(y0)))]
    # Blocks 1..(DELAY//BLOCK - 1) end before the delay has elapsed.
    for _ in range(DELAY // BLOCK - 1):
        y = mixer._process_samples(np.zeros(BLOCK, dtype=np.float32))
        peaks.append(float(np.max(np.abs(y))))
    # Direct sound in block 0, nothing before the delay...
    assert peaks[0] > 0.9
    assert max(peaks[1:]) < 1e-6
    # ...and the delayed copy once the delay has elapsed.
    y = mixer._process_samples(np.zeros(BLOCK, dtype=np.float32))
    assert float(np.max(np.abs(y))) == pytest.approx(0.3, abs=0.01)


def test_output_clipped_to_unit_range():
    mixer = MicMixer()
    y = mixer._process_samples(_sine(440.0, 1.0, amplitude=2.0))
    assert float(np.max(np.abs(y))) <= 1.0
    assert float(np.max(np.abs(y))) == pytest.approx(1.0)
    assert float(np.min(y)) >= -1.0 and float(np.max(y)) <= 1.0


def test_callback_muted_or_no_stream_is_silence():
    mixer = MicMixer()
    indata = np.full((BLOCK, 1), 0.5, dtype=np.float32)
    outdata = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    mixer.set_muted(True)
    mixer._callback(indata, outdata, BLOCK, None, None)
    assert not np.any(outdata)
    # Unmuted but no stream (never started): still silence.
    mixer.set_muted(False)
    outdata.fill(0.5)
    mixer._callback(indata, outdata, BLOCK, None, None)
    assert not np.any(outdata)


def test_shelf_low_gain_at_dc_and_high_gain_at_nyquist():
    """Pins the low-vs-high shelf convention: a sign swap must fail.

    The shelves are exercised through the mixer's own atomic bundle
    (``mixer._eq``) rather than the full chain, because the 80 Hz mic
    high-pass legitimately removes the 20 Hz test tone. Low shelf
    (+12 dB): +12 dB at the DC end (a 20 Hz sine as DC proxy -- the
    shelf sits within 0.06% of the asymptote there), 0 dB at the high
    end (10 kHz). High shelf (+12 dB): +12 dB at the Nyquist end (the
    f0 = 6 kHz shelf has only reached +10.8 dB at 10 kHz, so the
    asymptote is probed at 20 kHz, within 0.03% of +12 dB), 0 dB at
    the low end (100 Hz).
    """
    def shelf_ratio(mixer: MicMixer, index: int, freq: float) -> float:
        b, a = mixer._eq[0][index]
        x = _sine(freq, 1.0)
        y = lfilter(b, a, x)
        skip = int(FS * 0.25)  # let the section's transient settle
        return _rms(y[skip:]) / _rms(x[skip:])

    mixer = MicMixer()
    mixer.set_bass_db(12.0)
    assert shelf_ratio(mixer, 1, 20.0) == pytest.approx(TARGET, rel=1e-2)
    assert shelf_ratio(mixer, 1, 10000.0) == pytest.approx(1.0, rel=1e-3)

    mixer = MicMixer()
    mixer.set_treble_db(12.0)
    assert shelf_ratio(mixer, 2, 20000.0) == pytest.approx(TARGET, rel=1e-2)
    assert shelf_ratio(mixer, 2, 100.0) == pytest.approx(1.0, rel=1e-3)


def test_comb_fallback_matches_reference():
    """A block longer than the delay takes the per-sample fallback; it
    must match a naive per-sample comb reference y[n] = x[n] + fb *
    y[n - D] with zero pre-history."""
    mixer = MicMixer()
    mixer.set_echo(0.5)  # -> feedback 0.3
    fb, d = mixer._echo_fb, mixer._echo_delay
    x = np.zeros(d + 500, dtype=np.float32)
    x[0] = 1.0

    ref_buf = np.zeros(d + x.size, dtype=np.float64)  # [pre-history | y]
    for i in range(x.size):
        ref_buf[d + i] = x[i] + fb * ref_buf[i]
    ref = ref_buf[d:]

    got = mixer._comb(x)
    assert got.dtype == np.float32
    assert np.allclose(got, ref, rtol=1e-4, atol=1e-6)


def test_comb_cross_call_continuity_large_blocks():
    """Two consecutive calls longer than the delay must produce the same
    output as one call of double the length (lossless history handoff)."""
    def run(block: int, calls: int) -> np.ndarray:
        mixer = MicMixer()
        mixer.set_echo(0.5)
        rng = np.random.default_rng(7)
        x = (0.5 * rng.standard_normal(block * calls)).astype(np.float32)
        return np.concatenate(
            [mixer._comb(x[i * block:(i + 1) * block]) for i in range(calls)]
        )

    two_calls = run(7000, 2)
    one_call = run(14000, 1)
    assert np.allclose(two_calls, one_call, rtol=1e-4, atol=1e-6)


def test_echo_stable_at_max_feedback():
    mixer = MicMixer()
    mixer.set_echo(1.0)  # -> maximum feedback 0.6 (always < 1)
    assert mixer._echo_fb == pytest.approx(mic_mixer.MAX_ECHO_FEEDBACK)
    rng = np.random.default_rng(42)
    for _ in range(30):
        x = (0.9 * rng.standard_normal(BLOCK)).astype(np.float32)
        y = mixer._process_samples(x)
        assert np.all(np.isfinite(y))
        assert float(np.max(np.abs(y))) <= 1.0


def test_echo_output_dtype_float32():
    mixer = MicMixer()
    mixer.set_echo(0.5)
    y = mixer._process_samples(_sine(440.0, 0.1))
    assert y.dtype == np.float32


def test_gain_setter_clamps():
    mixer = MicMixer()
    mixer.set_gain_db(999.0)
    assert mixer._gain_db == pytest.approx(24.0)
    assert mixer._gain == pytest.approx(10.0 ** (24.0 / 20.0))
    mixer.set_gain_db(-999.0)
    assert mixer._gain_db == pytest.approx(-24.0)
    assert mixer._gain == pytest.approx(10.0 ** (-24.0 / 20.0))
    mixer.set_bass_db(999.0)
    assert mixer._bass_db == pytest.approx(12.0)
    mixer.set_treble_db(-999.0)
    assert mixer._treble_db == pytest.approx(-12.0)


def test_callback_exception_degrades_to_silence(monkeypatch):
    mixer = MicMixer()
    mixer._stream = object()  # pretend streaming; the chain must not raise

    def _boom(x):
        raise RuntimeError("boom")

    monkeypatch.setattr(mixer, "_process_samples", _boom)
    indata = np.full((BLOCK, 1), 0.5, dtype=np.float32)
    outdata = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    mixer._callback(indata, outdata, BLOCK, None, None)  # must not propagate
    assert not np.any(outdata)
    assert mixer.last_error  # the failure latched once, as a short string

    # While latched the DSP is not retried and the callback stays silent.
    calls = []

    def _boom_again(x):
        calls.append(1)
        raise RuntimeError("boom again")

    monkeypatch.setattr(mixer, "_process_samples", _boom_again)
    outdata.fill(0.5)
    mixer._callback(indata, outdata, BLOCK, None, None)
    assert calls == []
    assert not np.any(outdata)
    # A parameter change re-arms the chain and clears the latch.
    mixer.set_gain_db(0.0)
    assert mixer.last_error is None


# ----------------------------------------------------------- device access

def test_available_and_device_list_in_venv():
    assert available() is True
    devices = list_input_devices()
    assert isinstance(devices, list)
    for index, name in devices:
        assert isinstance(index, int)
        assert isinstance(name, str)


def test_start_refused_under_dummy_audio(monkeypatch):
    monkeypatch.setenv("EZKARAOKE_DUMMY_AUDIO", "1")
    mixer = MicMixer()
    assert mixer.start() is False
    assert mixer.is_running() is False


# ------------------------------------------------------ fake-stream lifecycle

class _FakeStream:
    instances: list[_FakeStream] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        _FakeStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _use_fake_stream(monkeypatch):
    _FakeStream.instances.clear()
    monkeypatch.setattr(mic_mixer.sd, "Stream", _FakeStream)


def test_fake_stream_lifecycle(monkeypatch):
    monkeypatch.setenv("EZKARAOKE_DUMMY_AUDIO", "0")
    _use_fake_stream(monkeypatch)
    mixer = MicMixer()
    assert mixer.start() is True
    assert mixer.is_running() is True
    assert len(_FakeStream.instances) == 1
    stream = _FakeStream.instances[0]
    assert stream.started
    # start() is idempotent: no second stream is opened
    assert mixer.start() is True
    assert len(_FakeStream.instances) == 1
    assert stream.kwargs["device"] == (None, None)
    assert stream.kwargs["channels"] == (1, 2)
    assert stream.kwargs["samplerate"] == FS
    # The captured callback routes the mono signal into both output columns.
    indata = np.zeros((BLOCK, 1), dtype=np.float32)
    outdata = np.zeros((BLOCK, 2), dtype=np.float32)
    indata[5, 0] = 0.25
    stream.kwargs["callback"](indata, outdata, BLOCK, None, None)
    assert np.allclose(outdata[:, 0], outdata[:, 1])
    assert outdata[5, 0] == pytest.approx(0.25, abs=1e-6)
    mixer.stop()
    assert stream.stopped and stream.closed
    assert mixer.is_running() is False
    mixer.stop()  # idempotent, no raise


def test_stream_open_failure_returns_false(monkeypatch):
    monkeypatch.setenv("EZKARAOKE_DUMMY_AUDIO", "0")

    def _raise(*args, **kwargs):
        raise mic_mixer.sd.PaError("no default input device")

    monkeypatch.setattr(mic_mixer.sd, "Stream", _raise)
    mixer = MicMixer()
    assert mixer.start() is False
    assert mixer.is_running() is False


def test_start_failure_closes_stream(monkeypatch):
    monkeypatch.setenv("EZKARAOKE_DUMMY_AUDIO", "0")

    class _FailingStream(_FakeStream):
        def start(self):
            raise mic_mixer.sd.PaError("cannot start stream")

    _use_fake_stream(monkeypatch)
    monkeypatch.setattr(mic_mixer.sd, "Stream", _FailingStream)
    mixer = MicMixer()
    assert mixer.start() is False
    assert mixer.is_running() is False
    assert len(_FakeStream.instances) == 1
    stream = _FakeStream.instances[0]
    assert stream.closed  # the failed stream is released, not leaked


def test_unavailable_is_safe_noop(monkeypatch):
    monkeypatch.setattr(mic_mixer, "_HAVE", False)
    assert available() is False
    assert list_input_devices() == []
    mixer = MicMixer()
    assert mixer.start() is False
    assert mixer.is_running() is False
    mixer.stop()  # nothing to stop, must not raise
