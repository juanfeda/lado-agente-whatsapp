# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio.

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio le llega
al agente por el system prompt (config/prompts.yaml), asi que para CONTESTAR preguntas
no hace falta nada de aca. Este archivo es el lugar para las ACCIONES —reservar, cobrar,
abrir un ticket— y conectarlas al ciclo de tool use de Claude es un paso aparte.

Casos de uso elegidos para Lado Inmobiliaria:
  1. Responder preguntas frecuentes  -> buscar_en_knowledge()
  3. Calificar y atender leads       -> registrar_lead() / calificar_lead() / escalar_a_vendedor()
  5. Soporte post-venta              -> crear_ticket() / consultar_ticket() / escalar_ticket()
"""

import logging
import os
import uuid
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")

# ── Derivación a operadores humanos ──────────────────────────
# Numeros de WhatsApp del equipo. Se cargan del .env, no se hardcodean.
OPERADOR_ALQUILERES = os.getenv("OPERADOR_ALQUILERES", "")
OPERADOR_OTRO = os.getenv("OPERADOR_OTRO", "")

_ZERNIO_API_KEY = os.getenv("ZERNIO_API_KEY", "")
_ZERNIO_ACCOUNT_ID = os.getenv("ZERNIO_ACCOUNT_ID", "")
_ZERNIO_BASE_URL = (os.getenv("ZERNIO_BASE_URL") or "https://zernio.com/api/v1").rstrip("/")


def operador_para(categoria: str) -> str:
    """Devuelve el numero de operador segun la categoria clasificada."""
    return OPERADOR_ALQUILERES if categoria == "alquiler" else OPERADOR_OTRO


async def notificar_operador(operador: str, telefono_cliente: str, mensaje_cliente: str) -> bool:
    """
    Le manda al operador humano un mensaje de WhatsApp avisando de la conversacion derivada.

    OJO — limite real de WhatsApp: para que esto le llegue al operador, el operador tiene
    que haberle escrito antes al numero principal del bot (ventana de 24hs abierta), o hay
    que usar un template aprobado por Meta. Sin eso, Zernio va a devolver un error de envio.
    La forma mas simple: que cada operador le mande un "hola" al numero del bot una vez.

    NOTA: el endpoint exacto para iniciar conversacion con un numero (no un conversation_id
    existente) hay que confirmarlo contra el dashboard/docs de tu cuenta Zernio — este es el
    patron mas comun de su API, pero puede variar. Probalo con un mensaje de prueba antes de
    ponerlo en producción.
    """
    if not _ZERNIO_API_KEY or not operador:
        logger.error("No se puede notificar al operador: falta ZERNIO_API_KEY u operador")
        return False

    texto = (
        f"Nueva conversacion derivada.\n"
        f"Cliente: {telefono_cliente}\n"
        f"Mensaje: {mensaje_cliente}"
    )

    url = f"{_ZERNIO_BASE_URL}/whatsapp/messages"
    headers = {
        "Authorization": f"Bearer {_ZERNIO_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"accountId": _ZERNIO_ACCOUNT_ID, "to": operador, "message": texto}

    try:
        async with httpx.AsyncClient(timeout=30.0) as cliente:
            r = await cliente.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        logger.error(f"Error de red notificando al operador {operador}: {e}")
        return False

    if r.status_code == 200:
        return True

    logger.error(f"Zernio rechazo la notificacion al operador [{r.status_code}]: {r.text[:300]}")
    return False


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# Ventas / leads — TODO: hoy queda registrado solo en logs.
# Si mas adelante querés que esto escriba directo en tu Lado CRM
# en vez de un archivo aparte, lo conectamos vía la API de tu CRM.
# ════════════════════════════════════════════════════════════

def registrar_lead(telefono: str, nombre: str = "", interes: str = "") -> dict:
    """Registra un lead nuevo (comprar, alquilar o tasar) con sus datos de contacto."""
    lead_id = str(uuid.uuid4())[:8]
    logger.info(f"[LEAD {lead_id}] telefono={telefono} nombre={nombre!r} interes={interes!r}")
    return {"lead_id": lead_id, "telefono": telefono, "nombre": nombre, "interes": interes}


def calificar_lead(telefono: str, presupuesto: str = "", zona: str = "", urgencia: str = "") -> str:
    """
    Clasifica un lead segun que tan avanzado esta: 'frio', 'tibio' o 'caliente'.
    Regla simple: si tiene presupuesto, zona y urgencia definidos, es 'caliente'.
    """
    datos_completos = sum(bool(x) for x in (presupuesto, zona, urgencia))
    if datos_completos == 3:
        return "caliente"
    if datos_completos >= 1:
        return "tibio"
    return "frio"


def escalar_a_vendedor(telefono: str, contexto: str) -> dict:
    """Deriva la conversacion a un vendedor humano del equipo comercial."""
    logger.info(f"[ESCALADO A VENDEDOR] telefono={telefono} contexto={contexto!r}")
    return {"escalado": True, "telefono": telefono}


# ════════════════════════════════════════════════════════════
# Soporte post-venta
# ════════════════════════════════════════════════════════════

def crear_ticket(telefono: str, problema: str) -> str:
    """Abre un ticket de soporte post-venta y devuelve su id."""
    ticket_id = str(uuid.uuid4())[:8]
    logger.info(f"[TICKET {ticket_id}] telefono={telefono} problema={problema!r}")
    return ticket_id


def consultar_ticket(ticket_id: str) -> dict:
    """Consulta el estado de un ticket. TODO: hoy no hay almacenamiento persistente de tickets."""
    logger.info(f"[CONSULTA TICKET] ticket_id={ticket_id}")
    return {"ticket_id": ticket_id, "estado": "desconocido"}


def escalar_ticket(ticket_id: str, razon: str) -> dict:
    """Escala un ticket de soporte al equipo humano."""
    logger.info(f"[ESCALADO TICKET {ticket_id}] razon={razon!r}")
    return {"escalado": True, "ticket_id": ticket_id}
