"""speech.manager — orquestração TTS para Decky Voice Reader.

Encapsula providers, speaker, normalizer, chunker e persistência de estado.
Expõe métodos usados pelo main.py do plugin.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from speech.normalizer import normalize
from speech.chunker import chunk
from speech.speaker import Speaker, Priority
from speech.ducking import duck, restore

from providers.tts.base import TTSProvider
from providers.tts.piper_provider import PiperTTSProvider
from providers.tts.edge_provider import EdgeTTSProvider
from providers.tts.omnivoice_provider import OmniVoiceProvider
from providers.tts.piper_downloader import PiperDownloader

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "pt_BR-faber-medium"
DEFAULT_SPEED = 1.0
DEFAULT_VOLUME = 80
DEFAULT_DUCKING = True
DEFAULT_DUCKING_LEVEL = 25

TTS_TEMP_DIR = os.path.join(tempfile.gettempdir(), "decky-translator-tts")


class TTSManager:
    """Gerencia todo o ciclo de vida TTS do plugin.

    Responsabilidades:
      - Manter configurações TTS sincronizadas com SettingsManager.
      - Instanciar o provider correto (piper/edge/omnivoice).
      - Reproduzir texto via Speaker com ducking.
      - Fornecer status em tempo real.
      - Gerenciar download do modelo Piper.
    """

    def __init__(self, plugin: Any, settings_manager: Any):
        self._plugin = plugin
        self._settings = settings_manager

        # config
        self._provider_name: str = "piper"
        self._voice: str = DEFAULT_VOICE
        self._speed: float = DEFAULT_SPEED
        self._volume: int = DEFAULT_VOLUME
        self._auto_read: bool = False
        self._ducking: bool = DEFAULT_DUCKING
        self._ducking_level: int = DEFAULT_DUCKING_LEVEL
        self._online_endpoint: str = ""
        self._online_api_key: str = ""
        self._online_compat: str = "openai"

        # runtime
        self._speaker = Speaker()
        self._speaker.start()
        self._last_text: str = ""
        self._last_translated_text: str = ""
        self._lock = threading.Lock()
        self._piper_downloader = PiperDownloader()

        os.makedirs(TTS_TEMP_DIR, exist_ok=True)

    # ── config loading / saving ─────────────────────────────────────────

    def load_settings(self) -> None:
        """Carrega config TTS do SettingsManager."""
        if not self._settings:
            return
        self._provider_name = self._settings.get_setting("tts_provider", "piper")
        self._voice = self._settings.get_setting("tts_ptbr_voice", DEFAULT_VOICE)
        self._speed = float(self._settings.get_setting("tts_speed", DEFAULT_SPEED))
        self._volume = int(self._settings.get_setting("tts_volume", DEFAULT_VOLUME))
        self._auto_read = bool(self._settings.get_setting("tts_auto_read", False))
        self._ducking = bool(self._settings.get_setting("tts_ducking", DEFAULT_DUCKING))
        self._ducking_level = int(self._settings.get_setting("tts_ducking_level", DEFAULT_DUCKING_LEVEL))
        self._online_endpoint = str(self._settings.get_setting("online_endpoint", ""))
        self._online_api_key = str(self._settings.get_setting("online_api_key", ""))
        self._online_compat = self._settings.get_setting("online_compat", "openai") or "openai"
        if self._online_compat not in ("openai", "simple"):
            self._online_compat = "openai"

    def get_settings(self) -> dict:
        """Retorna dict com configurações TTS atuais (api key mascarada)."""
        key = self._online_api_key
        masked = ""
        if key:
            if len(key) <= 4:
                masked = "****"
            else:
                masked = "*" * (len(key) - 4) + key[-4:]
        return {
            "tts_provider": self._provider_name,
            "tts_ptbr_voice": self._voice,
            "tts_speed": self._speed,
            "tts_volume": self._volume,
            "tts_auto_read": self._auto_read,
            "tts_ducking": self._ducking,
            "tts_ducking_level": self._ducking_level,
            "online_endpoint": self._online_endpoint,
            "online_api_key": masked,
            "online_compat": self._online_compat,
            "piper_downloaded": self._piper_downloader.is_downloaded(self._voice),
            "piper_downloading": self._piper_downloader.is_downloading(),
            "piper_progress": self._piper_downloader.get_progress(),
        }

    def set_settings(self, settings: dict) -> bool:
        """Aplica e persiste configurações TTS."""
        try:
            dirty = False

            if "tts_provider" in settings:
                v = settings["tts_provider"]
                if v in ("piper", "edge", "omnivoice"):
                    if self._provider_name != v:
                        self._provider_name = v
                        dirty = True
                        # Provider trocou: força voz padrão compatível para
                        # não ficar voz do Edge no Piper (ou vice-versa)
                        if v == "piper":
                            default_voice = DEFAULT_VOICE
                        elif v == "edge":
                            default_voice = "pt-BR-FranciscaNeural"
                        else:
                            default_voice = self._voice
                        if default_voice != self._voice:
                            self._voice = default_voice
                            dirty = True

            if "tts_ptbr_voice" in settings:
                v = str(settings["tts_ptbr_voice"])
                if self._voice != v:
                    # Valida voz por provider: Piper só aceita vozes conhecidas
                    if self._provider_name == "piper" and v not in ("pt_BR-faber-medium",):
                        logger.warning(f"Voz {v} inválida para provider piper; mantendo {self._voice}")
                    else:
                        self._voice = v
                        dirty = True

            if "tts_speed" in settings:
                try:
                    v = float(settings["tts_speed"])
                    v = max(0.5, min(2.0, v))
                    if abs(self._speed - v) > 0.001:
                        self._speed = v
                        dirty = True
                except Exception:
                    logger.warning(f"Invalid tts_speed: {settings['tts_speed']}")

            if "tts_volume" in settings:
                try:
                    v = int(settings["tts_volume"])
                    v = max(0, min(100, v))
                    if self._volume != v:
                        self._volume = v
                        dirty = True
                except Exception:
                    logger.warning(f"Invalid tts_volume: {settings['tts_volume']}")

            if "tts_auto_read" in settings:
                v = bool(settings["tts_auto_read"])
                if self._auto_read != v:
                    self._auto_read = v
                    dirty = True

            if "tts_ducking" in settings:
                v = bool(settings["tts_ducking"])
                if self._ducking != v:
                    self._ducking = v
                    dirty = True

            if "tts_ducking_level" in settings:
                try:
                    v = int(settings["tts_ducking_level"])
                    v = max(0, min(100, v))
                    if self._ducking_level != v:
                        self._ducking_level = v
                        dirty = True
                except Exception:
                    logger.warning(f"Invalid tts_ducking_level: {settings['tts_ducking_level']}")

            if "online_endpoint" in settings:
                v = str(settings["online_endpoint"]).strip()
                if self._online_endpoint != v:
                    self._online_endpoint = v
                    dirty = True

            if "online_api_key" in settings:
                v = str(settings["online_api_key"])
                # só atualiza se não for a string mascarada
                if "*" not in v or v == self._online_api_key:
                    if self._online_api_key != v:
                        self._online_api_key = v
                        dirty = True
                else:
                    logger.debug("online_api_key ignored because it looks masked")

            if "online_compat" in settings:
                v = settings["online_compat"]
                if v in ("openai", "simple"):
                    if self._online_compat != v:
                        self._online_compat = v
                        dirty = True

            if dirty and self._settings:
                for key, value in self._flatten_settings().items():
                    self._settings.set_setting(key, value)

            return True
        except Exception as e:
            logger.error(f"set_tts_settings failed: {e}")
            logger.error(traceback.format_exc())
            return False

    def _flatten_settings(self) -> dict:
        return {
            "tts_provider": self._provider_name,
            "tts_ptbr_voice": self._voice,
            "tts_speed": self._speed,
            "tts_volume": self._volume,
            "tts_auto_read": self._auto_read,
            "tts_ducking": self._ducking,
            "tts_ducking_level": self._ducking_level,
            "online_endpoint": self._online_endpoint,
            "online_api_key": self._online_api_key,
            "online_compat": self._online_compat,
        }

    # ── provider factory ────────────────────────────────────────────────

    def _create_provider(self) -> Optional[TTSProvider]:
        """Cria provider TTS baseado na config atual."""
        if self._provider_name == "piper":
            model_dir = self._piper_downloader.model_dir_for_voice(self._voice)
            return PiperTTSProvider(
                voice=self._voice,
                speed=self._speed,
                model_dir=model_dir,
                download_dir=model_dir,
            )
        if self._provider_name == "edge":
            # Edge TTS usa rate +/-%. speed 1.0 => +0%, 1.2 => +20%, 0.8 => -20%
            delta = int(round((self._speed - 1.0) * 100))
            rate = f"{delta:+d}%"
            return EdgeTTSProvider(
                voice=self._voice if self._voice else EdgeTTSProvider.DEFAULT_VOICE,
                rate=rate,
            )
        if self._provider_name == "omnivoice":
            return OmniVoiceProvider(
                endpoint=self._online_endpoint,
                api_key=self._online_api_key,
                compat=self._online_compat,
                voice=self._voice,
            )
        return None

    def _provider_is_ready(self, provider: Optional[TTSProvider]) -> tuple[bool, str]:
        if provider is None:
            return False, "Provider desconhecido"
        if self._provider_name == "piper":
            if not self._piper_downloader.is_downloaded(self._voice):
                return False, "Modelo Piper não instalado. Use 'Instalar voz offline'."
        if self._provider_name in ("edge", "omnivoice") and not provider.is_available():
            return False, "Dependências do provider online não disponíveis"
        return True, ""

    # ── speech pipeline ─────────────────────────────────────────────────

    def _prepare_text(self, text: str) -> list[str]:
        """Normaliza e quebra texto em chunks."""
        cleaned = normalize(text)
        if not cleaned:
            return []
        return chunk(cleaned, max_chars=400)

    def _temp_wav_path(self, prefix: str = "tts") -> str:
        ts = int(time.time() * 1000)
        return os.path.join(TTS_TEMP_DIR, f"{prefix}_{ts}.wav")

    def speak_text(self, text: str, priority: int = Priority.NORMAL) -> dict:
        """Fala um texto arbitrário (última tradução ou teste)."""
        if not text or not text.strip():
            return {"ok": False, "error": "Texto vazio"}

        chunks = self._prepare_text(text)
        if not chunks:
            return {"ok": False, "error": "Texto vazio após normalização"}

        provider = self._create_provider()
        ready, err = self._provider_is_ready(provider)
        if not ready:
            return {"ok": False, "error": err}

        # Pre-synthesize chunks and enqueue them with ducking
        duck_cfg = {
            "enabled": self._ducking,
            "level_percent": self._ducking_level,
        }

        for idx, txt in enumerate(chunks):
            wav_path = self._temp_wav_path(f"tts_{idx}")
            self._speaker.speak(
                text=txt,
                wav_path=wav_path,
                provider=provider,
                ducking_config=duck_cfg,
                priority=priority,
            )

        return {"ok": True, "queued": len(chunks)}

    def speak_last_translation(self) -> dict:
        """Fala a última tradução guardada (manual ou pós-tradução)."""
        with self._lock:
            text = self._last_translated_text or self._last_text

        if not text or not text.strip():
            return {"ok": False, "error": "Nenhuma tradução disponível para ler"}

        return self.speak_text(text, priority=Priority.NORMAL)

    def set_last_text(self, original: str, translated: str) -> None:
        """Atualiza texto original/traduzido para speak_last_translation."""
        with self._lock:
            self._last_text = original or ""
            self._last_translated_text = translated or ""

    def stop(self) -> dict:
        """Para fala atual e limpa fila."""
        self._speaker.stop_current()
        cleared = self._speaker.clear_queue()
        return {"ok": True, "cleared": cleared}

    def test_voice(self, text: Optional[str] = None) -> dict:
        """Testa voz: sintetiza e toca."""
        preview = (text or "").strip()[:400] or "Olá, teste de voz em português do Brasil."
        provider = self._create_provider()
        ready, err = self._provider_is_ready(provider)
        if not ready:
            return {"ok": False, "error": err}

        # Se for provider online, valida endpoint/key antes
        if self._provider_name == "omnivoice":
            if not self._online_endpoint:
                return {"ok": False, "error": "Endpoint vazio. Configure a URL do OmniVoice."}
            if not self._online_api_key:
                return {"ok": False, "error": "API Key vazia. Configure a chave do OmniVoice."}

        result = self.speak_text(preview, priority=Priority.HIGH)
        return result

    def get_status(self) -> dict:
        return {
            "provider": self._provider_name,
            "voice": self._voice,
            "speed": self._speed,
            "volume": self._volume,
            "auto_read": self._auto_read,
            "ducking": self._ducking,
            "ducking_level": self._ducking_level,
            "endpoint_configured": bool(self._online_endpoint),
            "has_api_key": bool(self._online_api_key),
            "compat": self._online_compat,
            "speaking": self._speaker.is_speaking,
            "queue_size": self._speaker.queue_size,
            "piper_downloaded": self._piper_downloader.is_downloaded(self._voice),
            "piper_downloading": self._piper_downloader.is_downloading(),
            "piper_progress": self._piper_downloader.get_progress(),
        }

    # ── piper model download ────────────────────────────────────────────

    def get_piper_status(self) -> dict:
        return self._piper_downloader.get_status(self._voice)

    def download_piper_model(self) -> bool:
        return self._piper_downloader.start_download(self._voice)

    def cancel_piper_download(self) -> bool:
        self._piper_downloader.cancel_download()
        return True

    def shutdown(self) -> None:
        try:
            self._speaker.stop()
        except Exception as e:
            logger.warning(f"TTSManager shutdown speaker error: {e}")
