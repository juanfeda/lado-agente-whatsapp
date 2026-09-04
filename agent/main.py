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

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    desmarcar_derivado,
    guardar_mensaje,
    incrementar_contador_mensajes,
    inicializar_db,
    liberar_evento,
    limpiar_etapa,
    limpiar_eventos_viejos,
    limpiar_historial,
    limpiar_mensajes_viejos,
    marcar_derivado,
    marcar_etapa,
    marcar_evento_procesado,
    marcar_ultimo_cliente,
    mensajes_recientes_de,
    obtener_derivacion,
    obtener_etapa,
    obtener_historial,
    obtener_ultimo_cliente,
    resetear_todos_los_contadores,
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


DIAS_RETENCION_HISTORIAL = int(os.getenv("DIAS_RETENCION_HISTORIAL") or "7")

# Numero de telefono de pruebas: cada 5 mensajes se reinicia solo, para poder probar
# el ciclo de derivacion sin llamar a /admin/reiniciar a mano cada vez.
NUMERO_PRUEBA = os.getenv("NUMERO_PRUEBA") or "5492216737240"

# Cada cuantos mensajes se reinicia solo el NUMERO_PRUEBA (0 = nunca reiniciar).
CICLO_RESET_PRUEBA = int(os.getenv("CICLO_RESET_PRUEBA") or "5")


async def _limpieza_periodica():
    """
    Corre la limpieza de historial una vez por dia mientras el servidor este vivo.

    Railway no reinicia el contenedor todos los dias, asi que limpiar solo al arrancar
    (en el lifespan) no alcanza para que la retencion de 7 dias se cumpla de verdad en
    un servicio que puede quedar corriendo semanas sin reiniciarse.
    """
    while True:
        await asyncio.sleep(24 * 60 * 60)  # 24 horas
        try:
            await limpiar_eventos_viejos(dias=DIAS_RETENCION_HISTORIAL)
            await limpiar_mensajes_viejos(dias=DIAS_RETENCION_HISTORIAL)
        except Exception as e:  # noqa: BLE001 — un fallo puntual no debe matar la tarea de fondo
            logger.error(f"Error en la limpieza periodica: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos(dias=DIAS_RETENCION_HISTORIAL)
    await limpiar_mensajes_viejos(dias=DIAS_RETENCION_HISTORIAL)
    logger.info(f"Base de datos lista (retencion de historial: {DIAS_RETENCION_HISTORIAL} dias)")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    tarea_limpieza = asyncio.create_task(_limpieza_periodica())

    yield

    tarea_limpieza.cancel()


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)

# Clave para el endpoint de administracion (/admin/desmarcar). Poné un valor propio
# en el .env; sin ADMIN_KEY configurada, el endpoint queda deshabilitado por seguridad.
_ADMIN_KEY = os.getenv("ADMIN_KEY", "")


@app.post("/admin/desmarcar")
async def admin_desmarcar(telefono: str, key: str):
    """
    Reactiva al agente para un cliente que ya estaba derivado (por ejemplo, cuando un
    operador termino de atenderlo, o para reiniciar una prueba).

    Uso: POST https://tu-dominio/admin/desmarcar?telefono=5491122334455&key=TU_ADMIN_KEY
    """
    if not _ADMIN_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_KEY no configurada")
    if key != _ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Clave incorrecta")

    await desmarcar_derivado(telefono)
    logger.info(f"Desmarcado manualmente: {telefono}")
    return {"status": "ok", "telefono": telefono}


@app.post("/admin/reiniciar")
async def admin_reiniciar(telefono: str, key: str):
    """
    Borra TODO el historial de conversacion de un numero, lo desmarca, y lo vuelve a
    poner en el menu principal — para poder probar de cero.

    Uso: POST https://tu-dominio/admin/reiniciar?telefono=5491122334455&key=TU_ADMIN_KEY
    """
    if not _ADMIN_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_KEY no configurada")
    if key != _ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Clave incorrecta")

    await limpiar_historial(telefono)
    await desmarcar_derivado(telefono)
    await limpiar_etapa(telefono)
    logger.info(f"Historial reiniciado manualmente: {telefono}")
    return {"status": "ok", "telefono": telefono}


@app.post("/admin/reiniciar_contadores")
async def admin_reiniciar_contadores(key: str):
    """
    Vuelve a 0 el contador de mensajes (el de "cada 5 mensajes") de TODOS los
    telefonos, no solo el numero de pruebas. No toca historial ni derivaciones.

    Uso: POST https://tu-dominio/admin/reiniciar_contadores?key=TU_ADMIN_KEY
    """
    if not _ADMIN_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_KEY no configurada")
    if key != _ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Clave incorrecta")

    cantidad = await resetear_todos_los_contadores()
    logger.info(f"Contadores reiniciados manualmente: {cantidad} numeros")
    return {"status": "ok", "reiniciados": cantidad}


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
        # de 24hs) no son consultas de clientes. Comandos que entienden:
        #   /tomo <telefono>  -> el bot deja de responderle a ese cliente (lo vas a atender vos a mano)
        #   /listo <telefono> -> el bot vuelve a responderle normal a ese cliente
        if msg.telefono in (OPERADOR_ALQUILERES, OPERADOR_OTRO):
            texto = msg.texto.strip()
            texto_lower = texto.lower()

            if texto_lower.startswith("/tomo") or texto_lower.startswith("/listo"):
                es_tomo = texto_lower.startswith("/tomo")
                partes = texto.split()
                telefono_escrito = "".join(c for c in (partes[1] if len(partes) > 1 else "") if c.isdigit())
                # Sin numero: usamos el ultimo cliente que se le notifico a este operador,
                # asi no hace falta copiar y pegar nada.
                telefono_cliente = telefono_escrito or await obtener_ultimo_cliente(msg.telefono)

                if not telefono_cliente:
                    comando = "/tomo" if es_tomo else "/listo"
                    await proveedor.enviar_mensaje(
                        msg.telefono,
                        f"No tengo ningun cliente reciente tuyo. Usa: {comando} 5491122334455",
                        msg.contexto,
                    )
                elif es_tomo:
                    await marcar_derivado(telefono_cliente, "otro", msg.telefono)
                    logger.info(f"Operador {msg.telefono} tomo a {telefono_cliente} con /tomo")
                    await proveedor.enviar_mensaje(
                        msg.telefono, f"Listo, el bot deja de responderle a {telefono_cliente}. Atendelo vos.", msg.contexto
                    )
                else:
                    await desmarcar_derivado(telefono_cliente)
                    logger.info(f"Operador {msg.telefono} desmarco a {telefono_cliente} con /listo")
                    await proveedor.enviar_mensaje(
                        msg.telefono, f"Listo, {telefono_cliente} vuelve a atenderlo el bot.", msg.contexto
                    )
            else:
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


MENUS = {
    "principal": {
        "cuerpo": "Hola! Bienvenido a Lado Inmobiliaria. ¿En qué te podemos ayudar?",
        "boton": "Ver opciones",
        "opciones": [
            {"id": "ventas", "titulo": "Ventas"},
            {"id": "alquileres", "titulo": "Alquileres"},
            {"id": "tasaciones", "titulo": "Tasaciones"},
            {"id": "otras", "titulo": "Otras consultas"},
        ],
    },
    "ventas": {
        "cuerpo": "Perfecto, ¿qué necesitás?",
        "opciones": [
            {"id": "consultar_venta", "titulo": "Ver propiedades"},
            {"id": "otras_venta", "titulo": "Otras consultas"},
        ],
    },
    "alquileres": {
        "cuerpo": "Perfecto, ¿qué necesitás?",
        "opciones": [
            {"id": "consultar_alquiler", "titulo": "Ver propiedades"},
            {"id": "otras_alquiler", "titulo": "Otras consultas"},
        ],
    },
}


async def _enviar_menu_principal(msg: MensajeEntrante):
    m = MENUS["principal"]
    await proveedor.enviar_lista(
        msg.telefono, msg.contexto, m["cuerpo"], m["boton"], [{"titulo": "Opciones", "filas": m["opciones"]}]
    )
    await marcar_etapa(msg.telefono, "menu_principal")


async def _enviar_submenu(msg: MensajeEntrante, cual: str, etapa: str):
    m = MENUS[cual]
    await proveedor.enviar_botones(msg.telefono, msg.contexto, m["cuerpo"], m["opciones"])
    await marcar_etapa(msg.telefono, etapa)


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Maneja un mensaje de cliente: el menu de botones primero, y una vez que eligio
    "ver propiedades" en venta o alquiler, la IA lo ayuda a buscar. Corre fuera del
    ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # Numero de pruebas: cuenta CADA mensaje entrante (este cliente derivado o
            # no) y, al llegar a un multiplo de 5, reinicia todo antes de seguir —
            # asi se puede probar el ciclo completo una y otra vez sin llamar a
            # /admin/reiniciar a mano.
            if msg.telefono == NUMERO_PRUEBA:
                total = await incrementar_contador_mensajes(msg.telefono)
                if CICLO_RESET_PRUEBA > 0 and total % CICLO_RESET_PRUEBA == 0:
                    await limpiar_historial(msg.telefono)
                    await desmarcar_derivado(msg.telefono)
                    await limpiar_etapa(msg.telefono)
                    logger.info(f"[PRUEBA] Contador reiniciado para {msg.telefono} en el mensaje #{total}")

            derivacion = await obtener_derivacion(msg.telefono)

            if derivacion is not None:
                # Ya esta derivado: el agente no contesta.
                operador = derivacion["operador"]
                if operador:
                    # Categoria "alquiler" (eligio "Otras consultas" en el submenu de
                    # alquileres): el operador se entera de cada mensaje nuevo.
                    await notificar_operador(operador, msg.telefono, msg.texto)
                    await marcar_ultimo_cliente(operador, msg.telefono)
                # Sin operador: el numero del bot queda en manual, lo atendes vos —
                # no hay a quien notificar.
                logger.info(f"Mensaje de {msg.telefono} en modo derivado (sin respuesta de IA)")
                return

            # Si vos (u otro humano) ya le escribiste primero a este cliente por la app
            # de WhatsApp Business, el bot no se mete: lo marca como derivado en silencio
            # (sin mandarle ningun mensaje) y listo, queda en manos humanas.
            conversation_id = msg.contexto.get("conversation_id", "")
            if conversation_id and await proveedor.conversacion_iniciada_por_negocio(conversation_id):
                await marcar_derivado(msg.telefono, "otro", "")
                logger.info(f"Conversacion con {msg.telefono} la inicio el negocio: el bot no responde")
                return

            # Salvaguarda anti-loop: si llegaron muchos mensajes de este numero en muy
            # poco tiempo, es un ritmo imposible para una persona tipeando — probablemente
            # sea otro bot. Se corta la conversacion automatica y se deriva.
            UMBRAL_MENSAJES = 6
            VENTANA_SEGUNDOS = 60
            recientes = await mensajes_recientes_de(msg.telefono, VENTANA_SEGUNDOS)
            if recientes >= UMBRAL_MENSAJES:
                logger.warning(
                    f"Posible bot detectado en {msg.telefono}: {recientes} mensajes "
                    f"en los ultimos {VENTANA_SEGUNDOS}s. Se corta la IA y se deriva."
                )
                await marcar_derivado(msg.telefono, "otro", "")
                return

            etapa = await obtener_etapa(msg.telefono)

            # ── Sin etapa todavia: primer contacto, mandamos el menu principal ──
            if etapa is None:
                await _enviar_menu_principal(msg)
                return

            # ── Esperando la eleccion del menu principal ──
            if etapa == "menu_principal":
                eleccion = msg.texto.strip().lower()
                if eleccion == "ventas":
                    await _enviar_submenu(msg, "ventas", "menu_ventas")
                elif eleccion == "alquileres":
                    await _enviar_submenu(msg, "alquileres", "menu_alquileres")
                elif eleccion in ("tasaciones", "otras", "otras consultas"):
                    await marcar_derivado(msg.telefono, "otro", "")
                    await limpiar_etapa(msg.telefono)
                    logger.info(f"{msg.telefono} eligio {eleccion}: numero del bot pasa a manual")
                else:
                    await _enviar_menu_principal(msg)  # no se entendio, reenviamos el menu
                return

            # ── Esperando la eleccion del submenu de Ventas ──
            if etapa == "menu_ventas":
                eleccion = msg.texto.strip().lower()
                if eleccion in ("consultar_venta", "ver propiedades"):
                    await marcar_etapa(msg.telefono, "ia_venta")
                    nota = "El cliente ya eligio: busca propiedades EN VENTA. Ayudalo a buscar."
                    respuesta, es_respuesta_real = await generar_respuesta(
                        "Quiero ver propiedades en venta", [], nota_sistema=nota
                    )
                    await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)
                    if es_respuesta_real:
                        await guardar_mensaje(msg.telefono, "assistant", respuesta)
                elif eleccion in ("otras_venta", "otras consultas"):
                    await marcar_derivado(msg.telefono, "otro", "")
                    await limpiar_etapa(msg.telefono)
                    logger.info(f"{msg.telefono} eligio otras consultas (ventas): numero del bot pasa a manual")
                else:
                    await _enviar_submenu(msg, "ventas", "menu_ventas")
                return

            # ── Esperando la eleccion del submenu de Alquileres ──
            if etapa == "menu_alquileres":
                eleccion = msg.texto.strip().lower()
                if eleccion in ("consultar_alquiler", "ver propiedades"):
                    await marcar_etapa(msg.telefono, "ia_alquiler")
                    nota = "El cliente ya eligio: busca propiedades EN ALQUILER. Ayudalo a buscar."
                    respuesta, es_respuesta_real = await generar_respuesta(
                        "Quiero ver propiedades en alquiler", [], nota_sistema=nota
                    )
                    await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)
                    if es_respuesta_real:
                        await guardar_mensaje(msg.telefono, "assistant", respuesta)
                elif eleccion in ("otras_alquiler", "otras consultas"):
                    operador = operador_para("alquiler")
                    await marcar_derivado(msg.telefono, "alquiler", operador)
                    await limpiar_etapa(msg.telefono)
                    await notificar_operador(operador, msg.telefono, "Otras consultas de alquiler (elegido por menu)")
                    await marcar_ultimo_cliente(operador, msg.telefono)
                    logger.info(f"{msg.telefono} eligio otras consultas (alquileres): pasa al operador")
                else:
                    await _enviar_submenu(msg, "alquileres", "menu_alquileres")
                return

            # ── La IA ya esta ayudando a buscar (venta o alquiler) ──
            if etapa in ("ia_venta", "ia_alquiler"):
                historial = await obtener_historial(msg.telefono)
                respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial)

                enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)
                if not enviado:
                    # El evento se marco como procesado ANTES de llegar hasta aca, para que
                    # dos entregas simultaneas no se dupliquen. Si el envio fallo, hay que
                    # soltarlo: si no, el reintento del proveedor se descartaria por
                    # duplicado y el cliente se quedaria sin respuesta para siempre.
                    logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                    await liberar_evento(evento_id)
                    return

                if es_respuesta_real:
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", respuesta)

                logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")
                return

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")
