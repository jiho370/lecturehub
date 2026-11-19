"""WASAPI capture devices, without opening a recording stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DeviceError(RuntimeError):
    """The requested WASAPI device or backend is unavailable."""


@dataclass(frozen=True, slots=True)
class Device:
    index: int
    name: str
    sample_rate: int
    channels: int
    is_loopback: bool
    is_default: bool


_KIND = {"mic": "마이크", "loopback": "PC 소리(스피커)"}
_NO_DEFAULT = {
    "mic": "마이크가 연결되어 있고 Windows 설정 > 개인 정보 및 보안 > 마이크에서 앱 접근이 켜져 있는지 확인하세요.",
    "loopback": "스피커나 이어폰이 연결되어 있는지 확인하세요.",
}


def _load_pyaudio() -> Any:
    try:
        import pyaudiowpatch
    except ImportError as exc:
        raise DeviceError(
            "녹음 장치를 여는 부품(PyAudioWPatch)을 불러오지 못했습니다. 앱을 다시 설치해 보세요. "
            '(소스에서 실행할 때: pip install -e ".[dev,recorder]")'
        ) from exc
    return pyaudiowpatch


def _validate_mode(mode: str | None) -> None:
    if mode not in (None, "mic", "loopback"):
        raise ValueError("mode must be 'mic' or 'loopback'")


def _enumerate_devices(audio: Any, backend: Any, mode: str | None = None) -> list[Device]:
    """Use one PortAudio snapshot so indices and defaults agree."""
    _validate_mode(mode)
    try:
        host = audio.get_host_api_info_by_type(backend.paWASAPI)
    except OSError as exc:
        raise DeviceError("Windows 소리 장치(WASAPI)를 쓸 수 없습니다. Windows 소리 설정에서 장치가 켜져 있는지 "
                          "확인하세요.") from exc
    default_input = int(host.get("defaultInputDevice", -1))
    default_loopback = -1
    if int(host.get("defaultOutputDevice", -1)) >= 0:
        try:
            default_loopback = int(audio.get_default_wasapi_loopback()["index"])
        except (OSError, LookupError):
            # Microphone listing must still work when no output endpoint exists.
            pass
    result = []
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        if info["hostApi"] != host["index"] or int(info["maxInputChannels"]) <= 0:
            continue
        loopback = bool(info.get("isLoopbackDevice", False))
        if mode is not None and loopback != (mode == "loopback"):
            continue
        result.append(
            Device(
                index=int(info["index"]),
                name=str(info["name"]),
                sample_rate=int(info["defaultSampleRate"]),
                channels=int(info["maxInputChannels"]),
                is_loopback=loopback,
                is_default=int(info["index"]) == (default_loopback if loopback else default_input),
            )
        )
    return result


def list_devices(mode: str | None = None) -> list[Device]:
    """List WASAPI microphones and loopbacks, marking the default for each mode.

    Loopback IDs are virtual *input* IDs, rather than the corresponding speaker
    output IDs. Every available loopback is listed, including the default output.
    """
    _validate_mode(mode)
    backend = _load_pyaudio()
    audio = backend.PyAudio()
    try:
        return _enumerate_devices(audio, backend, mode)
    finally:
        audio.terminate()


def _choose_device(devices: list[Device], mode: str, device_id: int | None,
                   name: str | None = None) -> Device:
    _validate_mode(mode)
    if mode is None:
        raise ValueError("mode must be 'mic' or 'loopback'")
    if device_id is not None and name is not None:
        raise ValueError("Use either a device ID or a device name, not both.")
    if name is not None:
        return _match_name(devices, mode, name)
    if device_id is not None:
        for device in devices:
            if device.index == device_id and device.is_loopback == (mode == "loopback"):
                return device
        raise DeviceError(f"{device_id}번 장치는 지금 쓸 수 있는 {_KIND[mode]} 입력이 아닙니다.")
    for device in devices:
        if device.is_default and device.is_loopback == (mode == "loopback"):
            return device
    raise DeviceError(f"기본 {_KIND[mode]} 장치가 없습니다. {_NO_DEFAULT[mode]}")


def _match_name(devices: list[Device], mode: str, name: str) -> Device:
    """Case-insensitive substring match, because WASAPI IDs change on reconnect.

    Several matches are resolved only by the WASAPI default among them; otherwise
    the choice is ambiguous and must be refused rather than guessed.
    """
    needle = name.strip().casefold()
    if not needle:
        raise ValueError("Device name must not be empty.")
    kind = [device for device in devices if device.is_loopback == (mode == "loopback")]
    found = [device for device in kind if needle in device.name.casefold()]
    if len(found) == 1:
        return found[0]
    defaults = [device for device in found if device.is_default]
    if len(defaults) == 1:
        return defaults[0]
    available = ", ".join(f"{device.index}={device.name}" for device in (found or kind)) or "없음"
    problem = "여러 장치가" if found else "장치가 없습니다"
    raise DeviceError(f"'{name}'와 일치하는 WASAPI {mode} {problem}. 후보: {available}")


def select_device(mode: str, device_id: int | None = None, name: str | None = None) -> Device:
    """Select an explicit input ID, a name substring, or the WASAPI default; never guess."""
    _validate_mode(mode)
    if mode is None:
        raise ValueError("mode must be 'mic' or 'loopback'")
    return _choose_device(list_devices(mode), mode, device_id, name)
