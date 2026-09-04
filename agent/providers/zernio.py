# agent/providers/zernio.py — Adaptador para Zernio
# Generado por AgentKit

"""
Zernio corre sobre la WhatsApp Cloud API de Meta: resuelve la conexion de la WhatsApp
Business Account, el inbox unificado y los webhooks firmados.

Documentacion: https://docs.zernio.com/platforms/whatsapp
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.memory import es_mensaje_propio, marcar_derivado, marcar_mensaje_propio
from agent.providers.base import MensajeEntrante, ProveedorWhatsApp
from agent.tools import operador_para

logger = logging.getLogger("agentkit")

BASE_URL_POR_DEFECTO = "https://zernio.com/api/v1"


class ProveedorZernio(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Zernio."""

    def __init__(self):
        self.api_key = os.getenv("ZERNIO_API_KEY", "")
        self.webhook_secret = os.getenv("ZERNIO_WEBHOOK_SECRET", "")
        # Opcional: solo se usa para el chequeo de conexion al arrancar.
        # Para responder, el account_id sale del propio webhook.
        self.account_id = os.getenv("ZERNIO_ACCOUNT_ID", "")
        # Ojo con el "or": os.getenv(clave, default) solo usa el default si la clave NO
        # existe. Como el .env trae "ZERNIO_BASE_URL=" vacia, os.getenv devuelve "" y el
        # default nunca se aplicaria. El "or" cubre los dos casos.
        self.base_url = (os.getenv("ZERNIO_BASE_URL") or BASE_URL_POR_DEFECTO).rstrip("/")

        if not self.api_key:
            logger.warning("ZERNIO_API_KEY no esta configurada: el agente no va a poder responder")
        if not self.webhook_secret:
            logger.warning(
                "ZERNIO_WEBHOOK_SECRET no esta configurado: los webhooks NO se verifican. "
                "Sirve para probar, pero no lo dejes asi en produccion."
            )

    # ── Recibir ──────────────────────────────────────────────────────────

    async def verificar_firma(self, request: Request) -> bool:
        """Compara el header X-Zernio-Signature contra el HMAC-SHA256 del cuerpo crudo."""
        if not self.webhook_secret:
            return True  # modo pruebas, ya se advirtio al arrancar

        firma_recibida = request.headers.get("X-Zernio-Signature") or request.headers.get(
            "X-Late-Signature"
        )
        if not firma_recibida:
            logger.warning("Llego un webhook sin firma X-Zernio-Signature: rechazado")
            return False

        cuerpo = await request.body()
        firma_esperada = hmac.new(
            self.webhook_secret.encode("utf-8"), cuerpo, hashlib.sha256
        ).hexdigest()

        # compare_digest sobre str exige ASCII puro. Los headers HTTP pueden traer bytes
        # que Starlette decodifica como latin-1, y ahi tiraria TypeError: eso escaparia del
        # handler como un 500, y el proveedor reintentaria el evento siete veces.
        try:
            iguales = hmac.compare_digest(firma_esperada, firma_recibida)
        except TypeError:
            logger.warning("La firma del webhook trae caracteres invalidos: rechazado")
            return False

        if not iguales:
            logger.warning("Firma de webhook invalida: rechazado")
            return False
        return True

    async def _procesar_mensaje_saliente(self, mensaje: dict, destinatario_hint: dict) -> None:
        """
        Chequea un mensaje SALIENTE (mandado desde el numero del bot, por cualquier via)
        y, si no lo mando el propio bot, marca esa conversacion como derivada en silencio.

        Se llama tanto para el evento message.sent como para un message.received que
        venga marcado como saliente (eco de un mensaje mandado desde la app nativa).
        """
        message_id = mensaje.get("platformMessageId") or mensaje.get("id") or ""
        if not message_id or await es_mensaje_propio(message_id):
            return  # lo mando el bot, no hacer nada

        telefono = ""
        if isinstance(destinatario_hint, dict):
            telefono = (destinatario_hint.get("phoneNumber") or destinatario_hint.get("id") or "")
        elif isinstance(destinatario_hint, str):
            telefono = destinatario_hint
        telefono = telefono.lstrip("+")

        if telefono:
            await marcar_derivado(telefono, "otro", operador_para("otro"))
            logger.info(f"Mensaje manual detectado hacia {telefono}: el bot deja de responderle")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Normaliza el evento message.received de Zernio."""
        payload = await request.json()

        evento = payload.get("event")

        # DIAGNOSTICO TEMPORAL: mostrar el payload crudo de cualquier evento que no sea
        # un mensaje entrante normal del cliente, para confirmar los nombres de campo
        # reales que usa esta cuenta.
        direccion = (payload.get("message") or {}).get("direction")
        if evento != "message.received" or direccion != "incoming":
            logger.warning(f"PAYLOAD CRUDO evento={evento!r} direction={direccion!r}: {payload}")

        if evento == "message.sent":
            mensaje = payload.get("message") or {}
            if mensaje.get("platform") == "whatsapp":
                destinatario = mensaje.get("recipient") or mensaje.get("to") or {}
                await self._procesar_mensaje_saliente(mensaje, destinatario)
            return []

        if evento != "message.received":
            # message.delivered, message.read, etc. no se contestan
            logger.debug(f"Evento ignorado: {evento}")
            return []

        mensaje = payload.get("message") or {}
        if mensaje.get("platform") != "whatsapp":
            return []

        remitente = mensaje.get("sender") or {}
        # phoneNumber viene en E.164 con "+". Desde abril de 2026 puede faltar
        # (los usuarios con username de WhatsApp no exponen su numero), asi que
        # caemos al identificador que Meta si garantiza.
        telefono = (remitente.get("phoneNumber") or "").lstrip("+")
        if not telefono:
            telefono = remitente.get("businessScopedUserId") or remitente.get("id") or ""

        es_saliente = mensaje.get("direction") != "incoming"

        if es_saliente:
            # Eco de un mensaje mandado desde la app nativa con el numero del bot:
            # llega como "message.received" pero en realidad es saliente hacia el cliente.
            # Aca "sender" en realidad describe al negocio, no al cliente — el destinatario
            # (el cliente) suele venir en "recipient"/"to"; si no esta, no hay forma de saber
            # a quien va y se ignora.
            destinatario = mensaje.get("recipient") or mensaje.get("to") or remitente
            await self._procesar_mensaje_saliente(mensaje, destinatario)
            return []

        cuenta = payload.get("account") or {}

        # Si el cliente toco un boton o una fila de una lista, el texto libre viene
        # vacio — lo que importa es el "id" que nosotros le pusimos a esa opcion al
        # mandar el menu. Lo usamos como si fuera el texto del mensaje, asi el resto
        # del codigo (que compara contra ids como "ventas", "alquileres", etc.) no
        # tiene que saber nada de interactivos.
        metadata = mensaje.get("metadata") or {}
        interactive_id = metadata.get("interactiveId") or metadata.get("interactive_id") or ""
        texto = interactive_id or (mensaje.get("text") or "")

        return [
            MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=mensaje.get("platformMessageId") or mensaje.get("id") or "",
                es_propio=False,  # ya filtramos los salientes arriba
                contexto={
                    "evento_id": payload.get("id", ""),
                    "conversation_id": mensaje.get("conversationId", ""),
                    "account_id": cuenta.get("id", ""),
                },
            )
        ]

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Responde dentro de la conversacion que abrio el cliente."""
        return await self._enviar_payload(contexto, {"message": mensaje})

    async def enviar_lista(
        self, telefono: str, contexto: dict | None, cuerpo: str, boton: str, secciones: list[dict]
    ) -> bool:
        """
        Manda un mensaje de lista de WhatsApp (hasta 10 filas). Usalo para menus con
        mas de 3 opciones. "secciones" es una lista de {"titulo": str, "filas": [{"id":
        str, "titulo": str, "descripcion": str opcional}]}.
        """
        interactive = {
            "type": "list",
            "body": {"text": cuerpo},
            "action": {
                "button": boton[:20],
                "sections": [
                    {
                        "title": s.get("titulo", "")[:24],
                        "rows": [
                            {
                                "id": f["id"],
                                "title": f["titulo"][:24],
                                **({"description": f["descripcion"][:72]} if f.get("descripcion") else {}),
                            }
                            for f in s.get("filas", [])
                        ],
                    }
                    for s in secciones
                ],
            },
        }
        return await self._enviar_payload(contexto, {"interactive": interactive})

    async def enviar_botones(
        self, telefono: str, contexto: dict | None, cuerpo: str, opciones: list[dict]
    ) -> bool:
        """
        Manda un mensaje con hasta 3 botones de respuesta rapida. "opciones" es una
        lista de {"id": str, "titulo": str}.
        """
        return await self._enviar_payload(
            contexto,
            {
                "message": cuerpo,
                "buttons": [{"id": o["id"], "title": o["titulo"][:20]} for o in opciones[:3]],
            },
        )

    async def _enviar_payload(self, contexto: dict | None, extra: dict) -> bool:
        """Logica compartida de envio: arma la request, la manda, y registra el ID propio."""
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id

        if not self.api_key:
            logger.error("No se puede enviar: falta ZERNIO_API_KEY")
            return False
        if not conversation_id or not account_id:
            logger.error(
                "No se puede enviar: el mensaje no trae conversation_id o account_id"
            )
            return False

        url = f"{self.base_url}/inbox/conversations/{conversation_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Si el webhook se reintenta, esta clave evita que Zernio mande la respuesta dos veces
        evento_id = contexto.get("evento_id")
        if evento_id:
            headers["Idempotency-Key"] = f"agentkit-{evento_id}"

        payload = {"accountId": account_id, **extra}

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Zernio: {e}")
            return False

        if r.status_code in (200, 201):
            # Registramos el ID del mensaje que MANDAMOS NOSOTROS, para poder
            # distinguirlo despues de un mensaje que un humano mande a mano.
            # El campo puede venir de varias formas segun el endpoint; probamos todas.
            try:
                cuerpo_ok = r.json()
                anidado = cuerpo_ok.get("message") or cuerpo_ok.get("data") or {}
                message_id = (
                    cuerpo_ok.get("id")
                    or cuerpo_ok.get("platform_message_id")
                    or cuerpo_ok.get("platformMessageId")
                    or cuerpo_ok.get("messageId")
                    or anidado.get("id")
                    or anidado.get("platform_message_id")
                    or anidado.get("platformMessageId")
                    or anidado.get("messageId")
                    or ""
                )
                if message_id:
                    await marcar_mensaje_propio(message_id)
                else:
                    logger.warning(
                        f"Zernio no devolvio un id de mensaje reconocible al enviar. Respuesta cruda: {r.text[:300]}"
                    )
            except ValueError:
                pass
            return True

        # Zernio responde {"error": ..., "type": ..., "code": ...}.
        # Loguear los tres ahorra muchisimo tiempo de diagnostico.
        detalle = r.text[:500]
        try:
            cuerpo = r.json()
            detalle = (
                f"{cuerpo.get('error')} "
                f"(type={cuerpo.get('type')}, code={cuerpo.get('code')})"
            )
        except ValueError:
            pass
        logger.error(f"Zernio rechazo el envio [{r.status_code}]: {detalle}")
        return False

    # ── Diagnostico ──────────────────────────────────────────────────────

    async def verificar_conexion(self) -> tuple[bool, str]:
        """
        Pregunta a Zernio el estado real del numero contra Meta.
        Es la via soportada para saber si el canal esta vivo.
        """
        if not self.api_key:
            return False, "Falta ZERNIO_API_KEY"
        if not self.account_id:
            return True, "ZERNIO_ACCOUNT_ID no configurado: se omite el chequeo del numero"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/whatsapp/number-info",
                    params={"accountId": self.account_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Zernio: {e}"

        if r.status_code != 200:
            return False, f"Zernio respondio {r.status_code}: {r.text[:200]}"

        telefono = r.json().get("phone") or {}
        return True, (
            f"Numero {telefono.get('display_phone_number', '?')} conectado "
            f"(calidad: {telefono.get('quality_rating', '?')})"
        )

    async def conversacion_iniciada_por_negocio(self, conversation_id: str) -> bool:
        """
        True si el primer mensaje de esta conversacion lo mandaste VOS (por la app de
        WhatsApp Business, no por el bot), no el cliente.

        Se usa para que el bot no interfiera en conversaciones que ya arranco un humano:
        si el cliente jamas escribio primero, no es una consulta que el bot deba atender.

        NOTA: pide la conversacion completa ordenada por fecha y mira el primer mensaje.
        Confirmar el endpoint exacto contra tu cuenta si Zernio lo cambia de nombre.
        """
        if not self.api_key or not conversation_id:
            return False  # sin como confirmarlo, se asume que no (el bot atiende normal)

        url = f"{self.base_url}/inbox/conversations/{conversation_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    url,
                    params={"order": "asc", "limit": 1},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            logger.warning(f"No se pudo chequear el origen de la conversacion: {e}")
            return False

        if r.status_code != 200:
            logger.warning(f"Zernio respondio {r.status_code} chequeando la conversacion")
            return False

        mensajes = r.json().get("data") or r.json().get("messages") or []
        if not mensajes:
            return False

        primer_mensaje = mensajes[0]
        return primer_mensaje.get("direction") == "outgoing"
