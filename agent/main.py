# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import clasificar_intencion, obtener_mensaje_error
from agent.memory import (
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    marcar_derivado,
    marcar_evento_procesado,
    obtener_derivacion,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante
from agent.tools import OPERADOR_ALQUILERES, OPERADOR_OTRO, notificar_operador, operador_para

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5 segundos y,
    si no lo reciben, reintentan el mismo evento hasta 7 veces. Como llamar a Claude tarda
    mas que eso, procesar antes de contestar hace que el cliente reciba la misma respuesta
    repetida. Por eso: responder primero, trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio or not msg.texto.strip():
            continue

        # Los mensajes de los propios operadores (ej: el "hola" para abrir la ventana
        # de 24hs) no son consultas de clientes: se ignoran para que no se autoderiven.
        if msg.telefono in (OPERADOR_ALQUILERES, OPERADOR_OTRO):
            logger.info(f"Mensaje de un operador ({msg.telefono}): se ignora, no es un cliente")
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


MENSAJE_DERIVACION = {
    "alquiler": "Gracias! Ya te estamos derivando con nuestro equipo de alquileres, en breve te contactan.",
    "otro": "Gracias! Ya te estamos derivando con un asesor de nuestro equipo, en breve te contactan.",
}


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            derivacion = await obtener_derivacion(msg.telefono)

            if derivacion is not None:
                # Ya esta derivado: el agente no contesta, solo le reenvia el mensaje
                # nuevo al operador que se quedo con la conversacion.
                await notificar_operador(derivacion["operador"], msg.telefono, msg.texto)
                logger.info(f"Mensaje de {msg.telefono} reenviado al operador (sin respuesta de IA)")
                return

            # Si vos (u otro humano) ya le escribiste primero a este cliente por la app
            # de WhatsApp Business, el bot no se mete: lo marca como derivado en silencio
            # (sin mandarle ningun mensaje) y listo, queda en manos humanas.
            conversation_id = msg.contexto.get("conversation_id", "")
            if conversation_id and await proveedor.conversacion_iniciada_por_negocio(conversation_id):
                await marcar_derivado(msg.telefono, "otro", operador_para("otro"))
                logger.info(f"Conversacion con {msg.telefono} la inicio el negocio: el bot no responde")
                return

            # Primera vez que este cliente escribe (o volvio a escribir sin estar
            # derivado): clasificamos y derivamos de una, no generamos respuesta de IA.
            categoria = await clasificar_intencion(msg.texto)
            operador = operador_para(categoria)

            await marcar_derivado(msg.telefono, categoria, operador)
            await notificar_operador(operador, msg.telefono, msg.texto)

            respuesta = MENSAJE_DERIVACION[categoria]
            es_respuesta_real = False  # es un aviso fijo, no una respuesta conversacional

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")
