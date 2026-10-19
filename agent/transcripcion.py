# agent/transcripcion.py — Pasa audios de WhatsApp a texto
"""
Transcribe notas de voz (Claude no procesa audio). Usa Groq (Whisper) si esta GROQ_API_KEY,
o OpenAI si esta OPENAI_API_KEY. Sin ninguna, los audios se derivan a una persona.
"""

import logging
import os

import httpx

logger = logging.getLogger("agentkit")



def _proveedor() -> tuple[str, str, str]:
    """(url, clave, modelo) del servicio configurado, o ("", "", "")."""
    groq = os.getenv("GROQ_API_KEY", "").strip()
    if groq:
        return (
            "https://api.groq.com/openai/v1/audio/transcriptions",
            groq,
            os.getenv("TRANSCRIPTION_MODEL") or "whisper-large-v3-turbo",
        )
    oai = os.getenv("OPENAI_API_KEY", "").strip()
    if oai:
        return (
            "https://api.openai.com/v1/audio/transcriptions",
            oai,
            os.getenv("TRANSCRIPTION_MODEL") or "gpt-4o-mini-transcribe",
        )
    return "", "", ""


_EXTENSIONES = {
    "ogg": "ogg", "opus": "ogg", "mpeg": "mp3", "mp3": "mp3", "mp4": "m4a", "m4a": "m4a",
    "aac": "m4a", "wav": "wav", "webm": "webm", "amr": "amr",
}


def transcripcion_disponible() -> bool:
    return bool(_proveedor()[1])


def _extension(mime: str) -> str:
    sub = (mime or "").split(";")[0].split("/")[-1].strip().lower()
    return _EXTENSIONES.get(sub, "ogg")  # las notas de voz de WhatsApp son ogg/opus


async def transcribir(audio: bytes, mime: str = "") -> str:
    """Devuelve el texto del audio, o "" si no se pudo transcribir."""
    url, clave, modelo = _proveedor()
    if not clave or not audio:
        return ""
    try:
        async with httpx.AsyncClient(timeout=60.0) as cliente:
            r = await cliente.post(
                url,
                headers={"Authorization": f"Bearer {clave}"},
                data={"model": modelo, "language": "es"},
                files={"file": (f"audio.{_extension(mime)}", audio, mime or "audio/ogg")},
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red transcribiendo audio: {e}")
        return ""
    if r.status_code != 200:
        logger.error(f"Transcripcion rechazada [{r.status_code}]: {r.text[:300]}")
        return ""
    return (r.json().get("text") or "").strip()
