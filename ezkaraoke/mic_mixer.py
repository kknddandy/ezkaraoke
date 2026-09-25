"""Microphone mixer: capture -> HPF -> gain -> 2-shelf EQ -> comb echo -> output.

Architecture (deliberately NO virtual sink): a single PortAudio *duplex*
stream captures the microphone, runs the DSP chain in the callback and
plays the result into the default output sink. The OS audio server sums it
with libvlc's output, so the player and the mic mix down in the sound card,
not in Python.

The heavy dependencies (numpy / sounddevice / scipy) are imported
defensively: without them the module still imports and is a safe no-op --
``available()`` is False, ``list_input_devices()`` returns [] and
``MicMixer.start()`` returns False (mirroring the ``EZKARAOKE_DUMMY_AUDIO``
no-op used by player.py for tests and dummy-audio setups).
"""

from __future__ import annotations

import math
import os

_HAVE = True
try:
    import numpy as np
    import sounddevice as sd
    from scipy.signal import lfilter
except Exception:  # noqa: BLE001 - ImportError, missing PortAudio runtime, ...
    np = None
    sd = None
    lfilter = None
    _HAVE = False

#: Audio EQ Cookbook (RBJ) standard resonance for the shelves.
_Q = 0.707
BASS_F0 = 150.0     # Hz, low-shelf centre
TREBLE_F0 = 6000.0  # Hz, high-shelf centre
ECHO_DELAY_S = 0.130          # feedback-comb delay (~130 ms)
MAX_ECHO_FEEDBACK = 0.6       # slider 1.0 maps here (always < 1, stable)
HPF_F0 = 80.0                 # Hz, mic high-pass (rumble / acoustic-feedback safety)

#: Parameter ranges, kept in sync with config.py (mic_gain_db, mic_bass_db
#: and mic_treble_db are validated to the same bounds).
GAIN_DB_RANGE = (-24.0, 24.0)
SHELF_DB_RANGE = (-12.0, 12.0)


def available() -> bool:
    """True when numpy + sounddevice are importable (deps installed)."""
    return _HAVE


def list_input_devices() -> list[tuple[int, str]]:
    """(index, name) pairs of devices with at least one input channel.

    Returns [] when the dependencies are missing or PortAudio cannot be
    queried (headless machine, no audio server, ...). Never raises.
    """
    if not _HAVE:
        return []
    try:
        devices = sd.query_devices()
    except Exception:  # noqa: BLE001 - e.g. PortAudioError on a headless box
        return []
    return [
        (index, device.get("name", ""))
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]


def _shelf_coeffs(
    f0: float, gain_db: float, samplerate: float, high_shelf: bool
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Biquad shelf coefficients, Audio EQ Cookbook (RBJ), Q = 0.707.

    Returns ((b0, b1, b2), (a0, a1, a2)) normalized by a0 (a0 == 1).
    Verified numerically: low shelf has exactly ``gain_db`` at DC and 0 dB
    up top; high shelf exactly ``gain_db`` at Nyquist and 0 dB down low;
    ``gain_db == 0`` is the identity filter.
    """
    ga = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / samplerate
    cos_w0 = math.cos(w0)
    alpha = math.sin(w0) / (2.0 * _Q)
    t = 2.0 * math.sqrt(ga) * alpha  # the cookbook's 2*sqrt(A)*alpha term
    if high_shelf:
        b0 = ga * ((ga + 1.0) + (ga - 1.0) * cos_w0 + t)
        b1 = -2.0 * ga * ((ga - 1.0) + (ga + 1.0) * cos_w0)
        b2 = ga * ((ga + 1.0) + (ga - 1.0) * cos_w0 - t)
        a0 = (ga + 1.0) - (ga - 1.0) * cos_w0 + t
        a1 = 2.0 * ((ga - 1.0) - (ga + 1.0) * cos_w0)
        a2 = (ga + 1.0) - (ga - 1.0) * cos_w0 - t
    else:
        b0 = ga * ((ga + 1.0) - (ga - 1.0) * cos_w0 + t)
        b1 = 2.0 * ga * ((ga - 1.0) - (ga + 1.0) * cos_w0)
        b2 = ga * ((ga + 1.0) - (ga - 1.0) * cos_w0 - t)
        a0 = (ga + 1.0) + (ga - 1.0) * cos_w0 + t
        a1 = -2.0 * ((ga - 1.0) + (ga + 1.0) * cos_w0)
        a2 = (ga + 1.0) + (ga - 1.0) * cos_w0 - t
    return (b0 / a0, b1 / a0, b2 / a0), (1.0, a1 / a0, a2 / a0)


def _hpf_coeffs(
    f0: float, samplerate: float
) -> tuple[list[float], list[float]]:
    """1-pole high-pass coefficients for ``lfilter`` (b, a), a[0] == 1.

    ``a = 1 - exp(-2*pi*f0/fs)``; the denominator ``[1, -(1 - a)]`` puts a
    real pole at ``exp(-2*pi*f0/fs)`` and the numerator ``[1, -1]`` a zero
    at DC. Response: flat in the passband (about +0.05 dB at Nyquist),
    -3 dB at f0, and DC fully removed.

    Note: the Gate 1 sketch suggested ``b = [a]``. With this same
    denominator that would be a 1-pole *low-pass* (0 dB at DC, about
    -41 dB at 10 kHz) and would have muted the voice; ``[1, -1]`` is the
    numerator that makes the sketched denominator the intended high-pass.
    """
    a = 1.0 - math.exp(-2.0 * math.pi * f0 / samplerate)
    return [1.0, -1.0], [1.0, -(1.0 - a)]


class MicMixer:
    """Microphone capture + vocal-effects chain on one PortAudio stream.

    The parameter setters run on the GUI thread while the audio callback
    runs on the PortAudio thread. The high-pass AND both EQ shelves AND
    their per-section ``lfilter`` ``zi`` state are bundled into ONE
    attribute (``self._eq``) that the setters swap atomically, so the
    callback never sees a half-built section set. Gain / mute / echo
    feedback are plain float/bool attributes; single reads are atomic
    under the GIL.

    A DSP exception in the callback latches in ``self._last_error``
    (read-only via :attr:`last_error`): while latched the callback emits
    silence instead of retrying the failing chain on every block. A new
    ``start()`` or any parameter setter (except ``set_muted``) clears the
    latch.
    """

    def __init__(
        self,
        samplerate: int = 48000,
        blocksize: int = 1024,
        input_device=None,
        output_device=None,
    ) -> None:
        self.samplerate = int(samplerate)
        self.blocksize = int(blocksize)
        self._input_device = input_device
        self._output_device = output_device
        self._stream = None
        self._gain_db = 0.0
        self._gain = 1.0
        self._muted = False
        self._bass_db = 0.0
        self._treble_db = 0.0
        self._echo_fb = 0.0
        self._last_error = None
        # Fixed delay of the feedback comb: 6240 samples @ 48 kHz.
        self._echo_delay = int(self.samplerate * ECHO_DELAY_S)
        self._comb_hist = (
            np.zeros(self._echo_delay, dtype=np.float32)
            if np is not None
            else None
        )
        self._eq = self._build_eq_bundle()

    # ------------------------------------------------------------- params

    def _build_eq_bundle(self) -> tuple:
        """(sections, zi) -- the single atomic EQ parameter object.

        ``sections``: ((b_hpf, a_hpf), (b_low, a_low), (b_high, a_high)),
        normalized.
        ``zi``: {"hpf": state, "low": state, "high": state} -- per-section
        lfilter state, kept INSIDE the bundle so a setter swap can never
        pair a new section set with stale state (or vice versa). A fresh
        bundle starts with zeroed state (a fresh filter transient, no DC
        step).
        """
        sections = (
            _hpf_coeffs(HPF_F0, self.samplerate),
            _shelf_coeffs(BASS_F0, self._bass_db, self.samplerate, False),
            _shelf_coeffs(TREBLE_F0, self._treble_db, self.samplerate, True),
        )
        if np is None:
            zi = {"hpf": (0.0,), "low": (0.0, 0.0), "high": (0.0, 0.0)}
        else:
            zi = {
                "hpf": np.zeros(1, dtype=np.float64),
                "low": np.zeros(2, dtype=np.float64),
                "high": np.zeros(2, dtype=np.float64),
            }
        return (sections, zi)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def set_gain_db(self, db: float) -> None:
        # Clamped to the config range (-24..24 dB).
        db = min(GAIN_DB_RANGE[1], max(GAIN_DB_RANGE[0], float(db)))
        self._gain_db = db
        self._gain = 10.0 ** (db / 20.0)
        self._last_error = None  # a parameter change re-arms the DSP

    def set_bass_db(self, db: float) -> None:
        db = min(SHELF_DB_RANGE[1], max(SHELF_DB_RANGE[0], float(db)))
        self._bass_db = db
        self._eq = self._build_eq_bundle()  # atomic swap, fresh zi
        self._last_error = None

    def set_treble_db(self, db: float) -> None:
        db = min(SHELF_DB_RANGE[1], max(SHELF_DB_RANGE[0], float(db)))
        self._treble_db = db
        self._eq = self._build_eq_bundle()  # atomic swap, fresh zi
        self._last_error = None

    def set_echo(self, amount: float) -> None:
        """amount in 0.0..1.0 -> comb feedback 0.0..0.6 (always < 1)."""
        amount = min(1.0, max(0.0, float(amount)))
        self._echo_fb = amount * MAX_ECHO_FEEDBACK
        self._last_error = None

    def is_running(self) -> bool:
        return self._stream is not None

    @property
    def last_error(self) -> str | None:
        """DSP exception latched by the audio callback, or None.

        While set, the callback emits silence without retrying the chain;
        a new ``start()`` or any parameter setter (except ``set_muted``)
        clears the latch.
        """
        return self._last_error

    # ------------------------------------------------------------ stream

    def start(self) -> bool:
        """Open the duplex stream. Idempotent; never raises.

        Returns True when streaming; False when the dependencies are
        missing, EZKARAOKE_DUMMY_AUDIO=1 (test/dummy-audio no-op), there is
        no input device, or PortAudio fails (PaError caught as Exception).
        A stream that fails to start is closed before being discarded (no
        PortAudio resource leak).
        """
        if not available():
            return False
        if self._stream is not None:
            return True
        if os.environ.get("EZKARAOKE_DUMMY_AUDIO") == "1":
            return False
        stream = None
        try:
            self._comb_hist = np.zeros(self._echo_delay, dtype=np.float32)
            self._eq = self._build_eq_bundle()
            self._last_error = None
            stream = sd.Stream(
                device=(self._input_device, self._output_device),
                channels=(1, 2),
                samplerate=self.samplerate,
                dtype="float32",
                blocksize=self.blocksize,
                latency="low",  # live vocal monitoring: keep the buffer short
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            return True
        except Exception:  # noqa: BLE001 - PaError / no device / ...
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
            self._stream = None
            return False

    def stop(self) -> None:
        """Stop and close the stream; idempotent; never raises."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:  # noqa: BLE001 - already stopped / torn down
            pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - ditto
            pass

    def _callback(self, indata, outdata, frames, time_info, status) -> None:
        """PortAudio realtime callback; must never raise.

        Any error degrades to silence for that block instead of killing the
        audio thread, and latches in ``self._last_error``: while latched
        the callback skips the DSP entirely (no retry on every block)
        until a parameter change or a new ``start()`` re-arms it. No
        Qt/signal calls here (the callback is not on the GUI thread).
        """
        try:
            if (
                self._muted
                or self._stream is None
                or self._last_error is not None
            ):
                outdata.fill(0)
                return
            mono = self._process_samples(indata[:, 0].copy())
            outdata[:, 0] = mono
            outdata[:, 1] = mono
        except Exception as exc:  # noqa: BLE001 - silence beats a dead audio thread
            if self._last_error is None:
                self._last_error = f"{type(exc).__name__}: {exc}"
            outdata.fill(0)

    # ---------------------------------------------------------------- dsp

    def _process_samples(self, x):
        """One block through the chain: HPF -> gain -> shelves -> comb -> clip.

        ``x``: (frames,) float32. Exposed for tests (no real device
        needed). Runs on the PortAudio thread when streaming; reads only
        the atomic parameters described in the class docstring.
        """
        if x.size == 0 or not _HAVE:
            return x
        sections, zi = self._eq
        # 1-pole 80 Hz high-pass: rumble / acoustic-feedback safety.
        x, zf = lfilter(sections[0][0], sections[0][1], x, zi=zi["hpf"])
        zi["hpf"] = zf  # write back into the bundle this call read
        x = x * self._gain
        x, zf = lfilter(sections[1][0], sections[1][1], x, zi=zi["low"])
        zi["low"] = zf
        x, zf = lfilter(sections[2][0], sections[2][1], x, zi=zi["high"])
        zi["high"] = zf
        if self._echo_fb > 0.0:
            x = self._comb(x)
        # lfilter may work in float64; the contract is float32 in and out.
        return np.clip(x, -1.0, 1.0).astype(np.float32, copy=False)

    def _comb(self, x):
        """One feedback-comb pass: y[n] = x[n] + fb * y[n - D].

        ``self._comb_hist`` keeps the last D outputs, oldest first. With
        blocksize <= D this is one vectorized pass per block; a larger
        blocksize falls back to a per-sample loop that maintains the same
        invariant -- the state is never silently corrupted.
        """
        n = x.size
        d = self._echo_delay
        fb = self._echo_fb
        hist = self._comb_hist
        if n <= d:
            y = x + fb * hist[:n]
            # Keep the rolling history float32: ``y`` is float64 after
            # lfilter, so the concatenate would silently promote it.
            self._comb_hist = np.concatenate((hist[n:], y)).astype(
                np.float32, copy=False
            )
            return y
        out = np.empty(n, dtype=np.float32)
        for i in range(n):
            delayed = hist[i] if i < d else out[i - d]
            out[i] = x[i] + fb * delayed
        self._comb_hist = out[n - d :].copy()
        return out
