from __future__ import annotations

import asyncio
import importlib.util
import io
import threading

from .config import Settings


class VoiceUnavailable(RuntimeError):
    pass


class LocalVoice:
    """Lazy, in-process local Whisper STT and Kokoro TTS engines."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stt = None
        self._tts = None
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()

    def status(self) -> dict:
        stt_installed = importlib.util.find_spec("faster_whisper") is not None
        tts_installed = importlib.util.find_spec("kokoro") is not None and importlib.util.find_spec("soundfile") is not None
        return {
            "enabled": self.settings.voice_enabled,
            "stt": "ready" if self.settings.voice_enabled and stt_installed else "not installed" if self.settings.voice_enabled else "disabled",
            "tts": "ready" if self.settings.voice_enabled and tts_installed else "not installed" if self.settings.voice_enabled else "disabled",
            "stt_model": self.settings.voice_stt_model,
            "tts_voice": self.settings.voice_tts_name,
            "browser_fallback": self.settings.voice_browser_fallback,
        }

    async def transcribe(self, audio: bytes) -> str:
        if not self.settings.voice_enabled:
            raise VoiceUnavailable("Local voice is disabled in config/cortexos.toml")
        if not audio or len(audio) > 20 * 1024 * 1024:
            raise ValueError("Audio is empty or exceeds the 20 MB limit")
        return await asyncio.to_thread(self._transcribe, audio)

    def _transcribe(self, audio: bytes) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceUnavailable('Whisper is not installed. Run: pip install -e ".[voice]"') from exc
        with self._stt_lock:
            if self._stt is None:
                try:
                    self._stt = WhisperModel(self.settings.voice_stt_model,
                                             device=self.settings.voice_stt_device,
                                             compute_type=self.settings.voice_stt_compute_type)
                except Exception as exc:
                    raise VoiceUnavailable(f"Could not load local Whisper model: {exc}") from exc
            try:
                segments, _ = self._stt.transcribe(io.BytesIO(audio), beam_size=3, vad_filter=True)
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:
                raise VoiceUnavailable(f"Local Whisper could not decode this recording: {exc}") from exc

    async def synthesize(self, text: str) -> bytes:
        if not self.settings.voice_enabled:
            raise VoiceUnavailable("Local voice is disabled in config/cortexos.toml")
        clean = text.strip()
        if not clean or len(clean) > 5000:
            raise ValueError("Speech text must contain 1 to 5000 characters")
        return await asyncio.to_thread(self._synthesize, clean)

    def _synthesize(self, text: str) -> bytes:
        try:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
        except ImportError as exc:
            raise VoiceUnavailable('Kokoro is not installed. Run: pip install -e ".[voice]"') from exc
        with self._tts_lock:
            if self._tts is None:
                try:
                    self._tts = KPipeline(lang_code=self.settings.voice_tts_language)
                except Exception as exc:
                    raise VoiceUnavailable(f"Could not load Kokoro. Ensure espeak-ng is installed: {exc}") from exc
            try:
                chunks = []
                for _, _, audio in self._tts(text, voice=self.settings.voice_tts_name, speed=1):
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
                if not chunks:
                    raise VoiceUnavailable("Kokoro returned no audio")
                wav = io.BytesIO()
                sf.write(wav, np.concatenate(chunks), 24000, format="WAV", subtype="PCM_16")
                return wav.getvalue()
            except VoiceUnavailable:
                raise
            except Exception as exc:
                raise VoiceUnavailable(f"Kokoro speech generation failed: {exc}") from exc
