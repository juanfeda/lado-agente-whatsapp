# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import logging
import os

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.tools import buscar_contacto_crm, buscar_propiedades

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz             $5 / $25 por millon de tokens
#   claude-sonnet-5   el balanceado (default)  $3 / $15
#   claude-haiku-4-5  el mas barato y rapido   $1 / $5
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True

# Tool que Claude puede llamar para buscar propiedades reales en ladoinmobiliaria.com.ar.
# Los tipos y zonas quedan libres (no un enum cerrado) porque no conocemos de antemano
# todos los valores que carga el equipo en la base.
HERRAMIENTAS = [
    {
        "name": "buscar_propiedades",
        "description": (
            "Busca propiedades activas y publicadas en ladoinmobiliaria.com.ar. Usala "
            "cuando el cliente pregunte por propiedades disponibles, precios, zonas, "
            "o quiera ver opciones concretas para comprar o alquilar. No inventes "
            "propiedades ni precios: si no tenes datos, usa esta herramienta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["venta", "alquiler"],
                    "description": "Tipo de operacion que busca el cliente",
                },
                "type": {
                    "type": "string",
                    "description": "Tipo de propiedad, ej: casa, departamento, terreno, local",
                },
                "zone": {
                    "type": "string",
                    "description": "Zona o barrio, ej: Ensenada, Punta Lara, La Plata",
                },
                "min_price": {"type": "number", "description": "Precio minimo en la moneda de la propiedad"},
                "max_price": {"type": "number", "description": "Precio maximo en la moneda de la propiedad"},
                "rooms": {"type": "integer", "description": "Cantidad minima de ambientes"},
            },
        },
    },
    {
        "name": "verificar_contacto",
        "description": (
            "Consulta si el cliente (por su numero de WhatsApp) ya esta cargado en la "
            "agenda del CRM. Llamala SIEMPRE antes de derivar_a_humano, apenas tengas "
            "nombre y apellido del cliente. Si ya existe con datos distintos a los que "
            "te acaba de dar, preguntale si quiere actualizarlos antes de continuar."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "derivar_a_humano",
        "description": (
            "Llamala cuando el cliente ya esta listo para que un humano continue. "
            "Requisito obligatorio: antes de llamarla, siempre pedile nombre, apellido "
            "y confirmá su numero de telefono de contacto (no alcanza con que quiera "
            "coordinar una visita o hablar con alguien — sin esos 3 datos, pedilos "
            "primero). Tambien llamala si el cliente pidio explicitamente hablar con "
            "alguien del equipo. Pasale un resumen breve con la propiedad de interes "
            "(si la hay) y el contexto de la consulta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operacion": {
                    "type": "string",
                    "enum": ["venta", "alquiler"],
                    "description": (
                        "A que operacion corresponde la consulta que vas a derivar. Usa la "
                        "operacion REAL de la que termino hablando el cliente, aunque haya "
                        "arrancado por la otra (ej: empezo preguntando por venta pero termino "
                        "pidiendo alquilar, ahi va 'alquiler')."
                    ),
                },
                "nombre_cliente": {
                    "type": "string",
                    "description": "Nombre de pila del cliente, tal como te lo confirmo.",
                },
                "apellido_cliente": {
                    "type": "string",
                    "description": "Apellido del cliente, tal como te lo confirmo.",
                },
                "contacto": {
                    "type": "string",
                    "description": "Telefono o mail de contacto confirmado (si es distinto de su WhatsApp). Vacio si es el mismo WhatsApp.",
                },
                "zona": {"type": "string", "description": "Zona o barrio de interes, si se menciono. Vacio si no."},
                "tipo_propiedad": {
                    "type": "string",
                    "description": "Tipo de propiedad que busca (casa, departamento, terreno, local, etc.). Vacio si no se menciono.",
                },
                "ambientes": {"type": "integer", "description": "Cantidad de ambientes que busca, si la menciono"},
                "dormitorios": {"type": "integer", "description": "Cantidad de dormitorios que busca, si la menciono"},
                "presupuesto_min": {"type": "number", "description": "Presupuesto minimo mencionado, si lo hay"},
                "presupuesto_max": {"type": "number", "description": "Presupuesto maximo mencionado, si lo hay"},
                "resumen": {
                    "type": "string",
                    "description": "Resumen para el humano que va a seguir: propiedad de interes y contexto de la consulta",
                },
            },
            "required": ["operacion", "nombre_cliente", "apellido_cliente", "resumen"],
        },
    },
]


async def _ejecutar_tool(nombre: str, entrada: dict, telefono_cliente: str = "") -> dict:
    """Despacha una llamada a herramienta hecha por Claude a la funcion real."""
    if nombre == "verificar_contacto":
        return await buscar_contacto_crm(telefono_cliente)
    if nombre == "buscar_propiedades":
        return await buscar_propiedades(
            operation=entrada.get("operation", ""),
            type=entrada.get("type", ""),
            zone=entrada.get("zone", ""),
            min_price=entrada.get("min_price"),
            max_price=entrada.get("max_price"),
            rooms=entrada.get("rooms"),
        )
    return {"error": f"Herramienta desconocida: {nombre}"}


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt(nota_extra: str = "") -> str:
    """
    El system prompt: quien es el agente y que sabe del negocio.

    "nota_extra" es un contexto puntual que se agrega al final (ej: "el cliente ya
    eligio que busca propiedades en VENTA"), sin tener que editar el yaml.
    """
    texto = cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )
    if nota_extra:
        texto += f"\n\n## Contexto de esta conversacion\n{nota_extra}"
    return texto


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def generar_respuesta(
    mensaje: str, historial: list[dict], nota_sistema: str = "", telefono_cliente: str = ""
) -> tuple[str, bool, dict | None]:
    """
    Genera una respuesta con Claude.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        nota_sistema: contexto puntual para esta llamada (ej: "el cliente eligio que
            busca propiedades en VENTA"), se agrega al system prompt.

    Returns:
        (texto, es_respuesta_real, derivar)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.

        "derivar" es None si la conversacion sigue normal, o
        {"operacion": "venta"|"alquiler", "resumen": str} si Claude decidio que el
        cliente ya esta listo para que un humano siga.
        listo para que un humano siga).
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False, None

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt(nota_sistema)
    extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=mensajes,
            tools=HERRAMIENTAS,
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a Claude: {e2}")
                return obtener_mensaje_error(), False, None
        else:
            logger.error(f"Error llamando a Claude: {e}")
            return obtener_mensaje_error(), False, None

    # Loop de tool use: mientras Claude pida usar una herramienta, la ejecutamos y le
    # devolvemos el resultado, hasta un maximo de vueltas para no colgar la respuesta
    # de WhatsApp si algo entra en bucle. derivar_a_humano es especial: corta el loop
    # de una, no hace falta seguir conversando despues de eso.
    vueltas = 0
    derivar = None
    while getattr(respuesta, "stop_reason", None) == "tool_use" and vueltas < 4:
        vueltas += 1

        llamada_derivar = next(
            (b for b in respuesta.content if b.type == "tool_use" and b.name == "derivar_a_humano"), None
        )
        if llamada_derivar is not None:
            derivar = {
                "operacion": llamada_derivar.input.get("operacion", "venta"),
                "resumen": llamada_derivar.input.get("resumen", ""),
                "nombre_cliente": llamada_derivar.input.get("nombre_cliente", ""),
                "apellido_cliente": llamada_derivar.input.get("apellido_cliente", ""),
                "contacto": llamada_derivar.input.get("contacto", ""),
                "zona": llamada_derivar.input.get("zona", ""),
                "tipo_propiedad": llamada_derivar.input.get("tipo_propiedad", ""),
                "ambientes": llamada_derivar.input.get("ambientes"),
                "dormitorios": llamada_derivar.input.get("dormitorios"),
                "presupuesto_min": llamada_derivar.input.get("presupuesto_min"),
                "presupuesto_max": llamada_derivar.input.get("presupuesto_max"),
            }
            logger.info(f"Claude decidio derivar a un humano: {derivar}")
            break

        mensajes.append({"role": "assistant", "content": respuesta.content})

        resultados_tools = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            logger.info(f"Claude llamo a la herramienta {bloque.name} con {bloque.input}")
            resultado = await _ejecutar_tool(bloque.name, bloque.input, telefono_cliente)
            resultados_tools.append(
                {
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": str(resultado),
                }
            )
        mensajes.append({"role": "user", "content": resultados_tools})

        parametros_extra = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}
        try:
            respuesta = await _llamar(parametros_extra)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error llamando a Claude durante el tool use: {e}")
            return obtener_mensaje_error(), False, None

    if getattr(respuesta, "stop_reason", None) == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto(respuesta)
    if not texto and derivar is None:
        logger.warning("Claude devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False, None

    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
    )
    return texto, bool(texto) and derivar is None, derivar
