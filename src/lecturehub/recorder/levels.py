"""PCM16 dBFS (RMS amplitude relative to full scale, not sine peak)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def mono_pcm(data: bytes, channels: int) -> bytes:
    samples = np.frombuffer(data, dtype="<i2")
    if channels == 1:
        return data
    return np.rint(samples.reshape(-1, channels).mean(axis=1)).astype("<i2").tobytes()


def channel_mix_energy(data: bytes, channels: int) -> tuple[float, float]:
    """Return (mean per-channel energy, downmix energy) in full-scale units.

    Averaging channels that are out of phase cancels sound. Comparing the two
    energies exposes that: equal ≈ 0 dB, uncorrelated ≈ -3 dB, cancelling << -6 dB.
    """
    frames = np.frombuffer(data, dtype="<i2").astype(np.float64).reshape(-1, channels) / 32768.0
    mono = frames.mean(axis=1)
    return float(np.mean(frames * frames)), float(np.mean(mono * mono))


@dataclass
class MixMonitor:
    """Accumulate downmix loss over blocks that actually carry sound."""
    min_rms_dbfs: float = -60.0
    channel_energy: float = 0.0
    mono_energy: float = 0.0
    samples: int = 0

    def update(self, data: bytes, channels: int) -> None:
        if channels < 2:
            return
        channel, mono = channel_mix_energy(data, channels)
        if channel <= 0 or 10 * math.log10(channel) < self.min_rms_dbfs:
            return
        count = len(data) // (2 * channels)
        self.channel_energy += channel * count
        self.mono_energy += mono * count
        self.samples += count

    @property
    def loss_db(self) -> float | None:
        if not self.samples or self.channel_energy <= 0:
            return None
        if self.mono_energy <= 0:
            return -120.0
        return round(10 * math.log10(self.mono_energy / self.channel_energy), 2)


def measure_dbfs(data: bytes) -> tuple[float, float]:
    values = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    if not len(values):
        return -math.inf, -math.inf
    peak = float(np.max(np.abs(values)))
    rms = float(np.sqrt(np.mean(values * values)))
    return (20 * math.log10(peak) if peak else -math.inf,
            20 * math.log10(rms) if rms else -math.inf)


@dataclass
class QuietTracker:
    threshold_dbfs: float = -50.0  # D-009: temporary proposal, configurable.
    warning_seconds: float = 30.0
    quiet_samples: int = 0

    def update(self, rms: float, samples: int, sample_rate: int) -> bool:
        self.quiet_samples = self.quiet_samples + samples if rms < self.threshold_dbfs else 0
        return self.quiet_samples >= self.warning_seconds * sample_rate
