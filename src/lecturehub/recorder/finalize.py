"""Stream recorder segments into a new, independently verified master."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
import wave
from decimal import Decimal
from pathlib import Path

from .heartbeat import SessionLock, write_json
from .paths import ensure_writable
from .recovery import read_json, session_sample_rate, validate_recorded_total
from .segments import HEADER_SIZE, inspect_segment, segment_paths


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        arguments, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=0x08000000 if os.name == "nt" else 0,  # no console window (the app is a windowed exe)
    )
    if result.returncode:
        raise RuntimeError(f"FFmpeg 검증/인코딩 실패: {result.stderr[-2000:].strip()}")
    return result


# User decision (2026-09-29): masters are MP3 above the 64 kbps upload profile; mono speech at 96 kbps.
# AAC/M4A stays available (explicit aac_bitrate or master_format="aac") and is the fallback if MP3 fails.
FORMATS = {
    "mp3": {"codec": "libmp3lame", "codec_name": "mp3", "suffix": ".mp3", "report": "mp3", "default_bitrate": "96k",
            "extra": []},
    "aac": {"codec": "aac", "codec_name": "aac", "suffix": ".m4a", "report": "aac_m4a", "default_bitrate": None,
            "extra": ["-movflags", "+faststart"]},
}
DEFAULT_FORMAT = "mp3"


def _encoded_duration_ms(ffmpeg: str, path: Path, sample_rate: int, codec_name: str = "aac") -> int:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = _run([
            ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=duration,sample_rate,channels,codec_name", "-of", "json", str(path),
        ])
        streams = json.loads(result.stdout).get("streams", [])
        if (len(streams) != 1 or streams[0].get("codec_name") != codec_name
                or int(streams[0].get("sample_rate", 0)) != sample_rate
                or int(streams[0].get("channels", 0)) != 1):
            raise RuntimeError(f"마스터의 코덱({codec_name})/샘플레이트/채널 검증에 실패했습니다.")
        raw_duration = streams[0].get("duration")
        if raw_duration is None or raw_duration == "N/A":
            raise RuntimeError("인코딩된 스트림의 재생 길이를 읽지 못했습니다.")
        duration_ms = int(Decimal(raw_duration) * 1000)
        # Metadata alone is insufficient: ask the decoder to read the whole file.
        _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])
        return duration_ms
    # A standalone ffmpeg is usable too. Count decoded PCM without loading the
    # recording into RAM. Encoder padding (AAC/MP3) is allowed only within the 100 ms check.
    process = subprocess.Popen(
        [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    count = 0
    try:
        while chunk := process.stdout.read(1024 * 1024):
            count += len(chunk)
    finally:
        process.stdout.close()
        returncode = process.wait()
    if returncode:
        raise RuntimeError("마스터 전체 디코딩 검증에 실패했습니다.")
    return count // 2 * 1000 // sample_rate


def _encode(ffmpeg: str, wav_path: Path, session_dir: Path, token: str, fmt: str, bitrate: str | None,
            rate: int, expected_ms: int) -> tuple[Path, int, str | None]:
    spec = FORMATS[fmt]
    bitrate = bitrate or spec["default_bitrate"]
    candidate = ensure_writable(session_dir / f"master_{token}{spec['suffix']}")
    command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", str(wav_path),
               "-map", "0:a:0", "-c:a", spec["codec"]]
    if bitrate is not None:
        command += ["-b:a", bitrate]
    command += [*spec["extra"], str(candidate)]
    _run(command)
    duration_ms = _encoded_duration_ms(ffmpeg, candidate, rate, spec["codec_name"])
    if abs(duration_ms - expected_ms) > 100:
        raise RuntimeError(
            f"{fmt.upper()} 길이 검증 실패: {duration_ms} ms, 예상 {expected_ms} ms (허용 ±100 ms). "
            f"세그먼트와 WAV를 보존했습니다: {wav_path}"
        )
    return candidate, duration_ms, bitrate


def finalize_session(session_dir: Path, *, aac_bitrate: str | None = None, master_format: str = DEFAULT_FORMAT,
                     bitrate: str | None = None) -> dict:
    """Keep all segments and never reuse an existing master filename.

    Masters are MP3 (96 kbps mono) by default; ``aac_bitrate`` or ``master_format="aac"`` makes an AAC M4A.
    If MP3 encoding fails, AAC is tried once before giving up. The report records format and bitrate policy.
    """
    if aac_bitrate is not None:
        master_format, bitrate = "aac", aac_bitrate
    if master_format not in FORMATS:
        raise ValueError(f"master_format must be one of {sorted(FORMATS)}")
    session_dir = ensure_writable(Path(session_dir))
    if not session_dir.is_dir():
        raise ValueError(f"세션 폴더가 없습니다: {session_dir}")
    if bitrate is not None and not bitrate.strip():
        raise ValueError("bitrate must not be empty")
    with SessionLock(session_dir):
        rate = session_sample_rate(session_dir)
        paths = segment_paths(session_dir)
        if not paths:
            raise ValueError("병합할 WAV 세그먼트가 없습니다.")
        # Interrupted sessions need an explicit recovery first, including when
        # their last header happened to have been flushed before termination.
        stop_exists = (session_dir / "stop.json").exists()
        source_name = "stop.json" if stop_exists else "recovery.json"
        source_report = read_json(session_dir / source_name)
        if not stop_exists and not source_report.get("recovered"):
            raise ValueError("비정상 종료 세션입니다. 먼저 recorder recover를 실행하세요.")
        infos = [inspect_segment(path, rate, allow_stale_lengths=(i == len(paths) - 1))
                 for i, path in enumerate(paths)]
        if not all(info.header_matches for info in infos):
            raise ValueError("WAV 길이가 맞지 않습니다. 먼저 recorder recover를 실행하세요.")
        total_samples = sum(info.samples for info in infos)
        validate_recorded_total(source_report, total_samples, rate, source_name)
        if total_samples == 0:
            raise ValueError("녹음된 오디오 샘플이 없습니다.")
        if total_samples * 2 > 0xFFFFFFFF - 36:
            raise ValueError("WAV 마스터가 RIFF 4 GB 제한을 넘습니다. 세그먼트는 모두 보존됩니다.")
        token = uuid.uuid4().hex
        wav_path = ensure_writable(session_dir / f"master_{token}.wav")
        with wav_path.open("xb") as stream:
            with wave.open(stream, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.setnframes(total_samples)
                for info in infos:
                    with info.path.open("rb") as source:
                        source.seek(HEADER_SIZE)
                        remaining = info.data_bytes
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise RuntimeError(f"병합 중 세그먼트가 짧아졌습니다: {info.path.name}")
                            output.writeframesraw(chunk)
                            remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        with wave.open(str(wav_path), "rb") as check:
            if check.getnframes() != total_samples or check.getframerate() != rate:
                raise RuntimeError("WAV 마스터 길이 검증 실패; 세그먼트를 보존했습니다.")
        expected_ms = total_samples * 1000 // rate
        ffmpeg = shutil.which("ffmpeg")
        master = wav_path
        duration_ms = expected_ms
        used_format, used_bitrate, fallback = None, None, None
        if ffmpeg:
            try:
                master, duration_ms, used_bitrate = _encode(ffmpeg, wav_path, session_dir, token, master_format,
                                                            bitrate, rate, expected_ms)
                used_format = master_format
            except RuntimeError as exc:
                if master_format != "mp3":
                    raise
                fallback = f"MP3 실패 → AAC: {str(exc)[-300:]}"
                master, duration_ms, used_bitrate = _encode(ffmpeg, wav_path, session_dir, token, "aac", None,
                                                            rate, expected_ms)
                used_format = "aac"
        report = {
            "session_dir": str(session_dir), "master": str(master), "wav_master": str(wav_path),
            "format": FORMATS[used_format]["report"] if used_format else "wav",
            "segments": [info.as_dict() for info in infos],
            "segments_preserved": True, "total_samples": total_samples, "total_ms": expected_ms,
            "master_total_ms": duration_ms, "duration_difference_ms": duration_ms - expected_ms,
            "sample_rate": rate, "verified": True,
            "verification_scope": "output_format_and_duration",
            "input_partial": bool(not stop_exists or source_report.get("partial")
                                  or source_report.get("input_partial") or source_report.get("error")
                                  or source_report.get("dropped_samples")
                                  or source_report.get("capture_status_count")),
            "input_cause": source_report.get("cause", "unknown"),
            "input_certainty": source_report.get("certainty", "unknown"),
            "bitrate": used_bitrate,
            "aac_bitrate": used_bitrate if used_format == "aac" else None,
            "bitrate_policy": ("requested" if bitrate else
                               "user_default_mp3_96k" if used_format == "mp3" else "encoder_default_provisional"),
            "fallback": fallback,
        }
        write_json(ensure_writable(session_dir / f"finalize_{token}.json"), report)
        return report
