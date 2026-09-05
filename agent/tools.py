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
# Base real de la API de Zernio: los endpoints van despues como /v1/...
_ZERNIO_BASE_URL = (os.getenv("ZERNIO_BASE_URL") or "https://zernio.com/api").rstrip("/")
# Nombre del template aprobado por Meta para notificar a los operadores. Sin esto,
# Zernio va a rechazar el envio salvo que tu cuenta tenga Direct Send habilitado.
_ZERNIO_TEMPLATE_NOTIFICACION = os.getenv("ZERNIO_TEMPLATE_NOTIFICACION", "")
_ZERNIO_TEMPLATE_IDIOMA = os.getenv("ZERNIO_TEMPLATE_IDIOMA") or "es"

# ── Busqueda de propiedades en la web de Lado ────────────────
_LADOWEB_API_URL = os.getenv("LADOWEB_API_URL", "")
_LADOWEB_API_KEY = os.getenv("LADOWEB_API_KEY", "")


async def buscar_propiedades(
    operation: str = "",
    type: str = "",
    zone: str = "",
    min_price: float | None = None,
    max_price: float | None = None,
    rooms: int | None = None,
) -> dict:
    """
    Busca propiedades activas en ladoinmobiliaria.com.ar via propiedades_api.php.

    Cualquier parametro vacio/None no se manda (no filtra por esa columna).
    """
    if not _LADOWEB_API_URL or not _LADOWEB_API_KEY:
        return {"error": "La busqueda de propiedades no esta configurada (falta LADOWEB_API_URL o LADOWEB_API_KEY)"}

    params = {}
    if operation:
        params["operation"] = operation
    if type:
        params["type"] = type
    if zone:
        params["zone"] = zone
    if min_price is not None:
        params["min_price"] = min_price
    if max_price is not None:
        params["max_price"] = max_price
    if rooms is not None:
        params["rooms"] = rooms

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(
                _LADOWEB_API_URL, params=params, headers={"X-Api-Key": _LADOWEB_API_KEY}
            )
    except httpx.HTTPError as e:
        logger.error(f"Error buscando propiedades: {e}")
        return {"error": "No se pudo conectar con el buscador de propiedades"}

    if r.status_code != 200:
        logger.error(f"propiedades_api.php respondio {r.status_code}: {r.text[:200]}")
        return {"error": "El buscador de propiedades respondio con un error"}

    try:
        return r.json()
    except ValueError:
        logger.error(f"propiedades_api.php no devolvio JSON valido. Respuesta cruda: {r.text[:500]}")
        return {"error": "El buscador de propiedades devolvio una respuesta invalida"}


# ── Carga de leads en el CRM de Lado ─────────────────────────
_CRM_API_URL = os.getenv("CRM_API_URL", "")
_CRM_API_KEY = os.getenv("CRM_API_KEY", "")


async def buscar_contacto_crm(telefono: str) -> dict:
    """
    Busca si este telefono ya esta en la agenda del CRM. Usa el mismo endpoint
    leads_api.php pero con GET, pasando el telefono por query string.

    Retorna {"existe": bool, "nombre": str, "apellido": str} — nombre/apellido
    vacios si no existe.
    """
    if not _CRM_API_URL or not _CRM_API_KEY:
        logger.warning("No se pudo verificar el contacto: falta CRM_API_URL o CRM_API_KEY")
        return {"existe": False, "nombre": "", "apellido": ""}

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(
                _CRM_API_URL, params={"telefono": telefono}, headers={"X-Api-Key": _CRM_API_KEY}
            )
    except httpx.HTTPError as e:
        logger.error(f"Error verificando contacto en el CRM: {e}")
        return {"existe": False, "nombre": "", "apellido": ""}

    if r.status_code != 200:
        logger.error(f"leads_api.php (GET) respondio {r.status_code}: {r.text[:200]}")
        return {"existe": False, "nombre": "", "apellido": ""}

    try:
        return r.json()
    except ValueError:
        logger.error(f"leads_api.php (GET) no devolvio JSON valido: {r.text[:300]}")
        return {"existe": False, "nombre": "", "apellido": ""}


async def crear_lead_crm(
    telefono: str,
    nombre: str = "",
    apellido: str = "",
    tipo: str = "compra",
    zona: str = "",
    tipo_propiedad: str = "",
    ambientes: int | None = None,
    dormitorios: int | None = None,
    presupuesto_min: float | None = None,
    presupuesto_max: float | None = None,
    notas: str = "",
    crear_lead: bool = True,
) -> bool:
    """
    Actualiza (o crea) al cliente en la agenda del CRM via leads_api.php. Si
    crear_lead=True, ademas carga un lead nuevo en crm_leads (usalo solo cuando el
    cliente estaba buscando propiedades para comprar/alquilar — no para "Otras
    consultas", donde solo corresponde la agenda).
    No frena la conversacion si falla — solo loguea el error.
    """
    if not _CRM_API_URL or not _CRM_API_KEY:
        logger.warning("No se pudo cargar el lead en el CRM: falta CRM_API_URL o CRM_API_KEY")
        return False

    payload = {
        "telefono": telefono,
        "nombre": nombre,
        "apellido": apellido,
        "tipo": tipo,
        "zona_preferida": zona,
        "tipo_propiedad": tipo_propiedad,
        "notas": notas,
        "crear_lead": crear_lead,
    }
    if ambientes is not None:
        payload["ambientes"] = ambientes
    if dormitorios is not None:
        payload["dormitorios"] = dormitorios
    if presupuesto_min is not None:
        payload["presupuesto_min"] = presupuesto_min
    if presupuesto_max is not None:
        payload["presupuesto_max"] = presupuesto_max

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.post(_CRM_API_URL, json=payload, headers={"X-Api-Key": _CRM_API_KEY})
    except httpx.HTTPError as e:
        logger.error(f"Error cargando lead en el CRM: {e}")
        return False

    if r.status_code != 200:
        logger.error(f"leads_api.php respondio {r.status_code}: {r.text[:300]}")
        return False

    logger.info(f"Lead cargado en el CRM: {r.text[:200]}")
    return True


def operador_para(categoria: str) -> str:
    """
    Devuelve el numero de operador segun la categoria, o "" si no hay operador (categoria
    'otro' se queda en el numero del bot, atendida a mano por vos — no se notifica a nadie).
    """
    return OPERADOR_ALQUILERES if categoria == "alquiler" else ""


async def notificar_operador(operador: str, telefono_cliente: str, mensaje_cliente: str) -> bool:
    """
    Le manda al operador humano un mensaje de WhatsApp avisando de la conversacion derivada.

    Usa POST /v1/inbox/conversations ("Create conversation") de Zernio: crea la conversacion
    si no existe, o reusa la que ya haya (por eso funciona si el operador le escribio antes
    al bot para abrir la ventana de 24hs). Se manda con category="utility" (WhatsApp Direct
    Send) para que Zernio no exija un template aprobado por Meta.

    OJO: category="utility" solo funciona en cuentas de WhatsApp "eligible" segun Meta —
    generalmente requiere haber pasado la verificacion de negocio. Si tu cuenta todavia no
    esta verificada, este envio puede seguir rechazandose hasta que se apruebe.
    """
    if not _ZERNIO_API_KEY or not operador:
        logger.error("No se puede notificar al operador: falta ZERNIO_API_KEY u operador")
        return False

    texto = (
        f"Nueva conversacion derivada.\n"
        f"Cliente: {telefono_cliente}\n"
        f"Mensaje: {mensaje_cliente}"
    )

    url = f"{_ZERNIO_BASE_URL}/v1/inbox/conversations"
    headers = {
        "Authorization": f"Bearer {_ZERNIO_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"accountId": _ZERNIO_ACCOUNT_ID, "participantId": operador}

    if _ZERNIO_TEMPLATE_NOTIFICACION:
        # Camino confiable: template aprobado por Meta. No depende de Direct Send
        # ni de que el operador te haya escrito antes.
        payload["templateName"] = _ZERNIO_TEMPLATE_NOTIFICACION
        payload["templateLanguage"] = _ZERNIO_TEMPLATE_IDIOMA
        payload["templateParams"] = [telefono_cliente, mensaje_cliente]
    else:
        # Fallback: solo funciona si tu cuenta tiene Direct Send habilitado por Meta.
        payload["message"] = texto
        payload["category"] = "utility"

    try:
        async with httpx.AsyncClient(timeout=30.0) as cliente:
            r = await cliente.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        logger.error(f"Error de red notificando al operador {operador}: {e}")
        return False

    if r.status_code in (200, 201):
        logger.info(f"Zernio acepto la notificacion al operador. Respuesta: {r.text[:300]}")
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
