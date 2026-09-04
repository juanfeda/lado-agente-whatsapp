# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, Integer, String, Text, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class ConversacionDerivada(Base):
    """
    Conversaciones que ya se derivaron a un operador humano.

    Mientras "activo" sea True, el agente deja de responderle a ese cliente: solo
    reenvia sus mensajes al operador. Un humano la "desmarca" (activo=False) para
    que el agente vuelva a contestar.
    """

    __tablename__ = "conversaciones_derivadas"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    categoria: Mapped[str] = mapped_column(String(30))  # "alquiler" u "otro"
    operador: Mapped[str] = mapped_column(String(50))
    derivado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class EstadoMenu(Base):
    """
    En que etapa del menu de botones esta cada cliente. Etapas posibles:
      menu_principal   -> se le mando el menu Ventas/Alquileres/Tasaciones/Otras
      menu_ventas      -> se le mando el submenu de Ventas
      menu_alquileres  -> se le mando el submenu de Alquileres
      ia_venta         -> la IA lo esta ayudando a buscar propiedades EN VENTA
      ia_alquiler      -> la IA lo esta ayudando a buscar propiedades EN ALQUILER
    Cuando el cliente queda derivado (ConversacionDerivada), esta tabla ya no importa.
    """

    __tablename__ = "estado_menu"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    etapa: Mapped[str] = mapped_column(String(30))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def marcar_etapa(telefono: str, etapa: str):
    """Guarda en que etapa del menu esta este cliente."""
    async with async_session() as session:
        await session.execute(delete(EstadoMenu).where(EstadoMenu.telefono == telefono))
        session.add(EstadoMenu(telefono=telefono, etapa=etapa, actualizado_en=ahora()))
        await session.commit()


async def obtener_etapa(telefono: str) -> str | None:
    """La etapa actual del menu para este cliente, o None si todavia no empezo."""
    async with async_session() as session:
        resultado = await session.execute(select(EstadoMenu).where(EstadoMenu.telefono == telefono))
        fila = resultado.scalar_one_or_none()
    return fila.etapa if fila else None


async def limpiar_etapa(telefono: str):
    """Borra la etapa del menu de este cliente (para /admin/reiniciar)."""
    async with async_session() as session:
        await session.execute(delete(EstadoMenu).where(EstadoMenu.telefono == telefono))
        await session.commit()


class UltimoClientePorOperador(Base):
    """
    Ultimo cliente que se le notifico a cada operador. Sirve para que /tomo y /listo
    funcionen SIN tener que copiar y pegar el telefono del cliente.
    """

    __tablename__ = "ultimo_cliente_por_operador"

    operador: Mapped[str] = mapped_column(String(50), primary_key=True)
    telefono_cliente: Mapped[str] = mapped_column(String(50))
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


async def marcar_ultimo_cliente(operador: str, telefono_cliente: str):
    """Recuerda cual fue el ultimo cliente que se le noti a este operador."""
    async with async_session() as session:
        await session.execute(delete(UltimoClientePorOperador).where(UltimoClientePorOperador.operador == operador))
        session.add(
            UltimoClientePorOperador(operador=operador, telefono_cliente=telefono_cliente, actualizado_en=ahora())
        )
        await session.commit()


async def obtener_ultimo_cliente(operador: str) -> str | None:
    """El ultimo cliente notificado a este operador, o None si no hay ninguno."""
    async with async_session() as session:
        resultado = await session.execute(
            select(UltimoClientePorOperador).where(UltimoClientePorOperador.operador == operador)
        )
        fila = resultado.scalar_one_or_none()
    return fila.telefono_cliente if fila else None


class MensajePropio(Base):
    """
    IDs de los mensajes que el propio bot mando (via su API), para poder distinguirlos
    de los que un humano manda a mano desde el inbox de Zernio o la app de WhatsApp.
    """

    __tablename__ = "mensajes_propios"

    message_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def marcar_mensaje_propio(message_id: str):
    """Registra que ESTE mensaje lo mando el bot, no un humano."""
    if not message_id:
        return
    async with async_session() as session:
        session.add(MensajePropio(message_id=message_id, creado_en=ahora()))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # ya estaba registrado, no pasa nada


async def es_mensaje_propio(message_id: str) -> bool:
    """True si ESTE mensaje lo mando el bot (no un humano a mano)."""
    if not message_id:
        return False
    async with async_session() as session:
        resultado = await session.execute(
            select(MensajePropio).where(MensajePropio.message_id == message_id)
        )
        return resultado.scalar_one_or_none() is not None


async def marcar_derivado(telefono: str, categoria: str, operador: str):
    """Marca una conversacion como derivada a un operador. Sobreescribe si ya existia."""
    async with async_session() as session:
        await session.execute(delete(ConversacionDerivada).where(ConversacionDerivada.telefono == telefono))
        session.add(
            ConversacionDerivada(
                telefono=telefono, categoria=categoria, operador=operador, derivado_en=ahora(), activo=True
            )
        )
        await session.commit()


async def obtener_derivacion(telefono: str) -> dict | None:
    """Retorna la derivacion activa de un cliente, o None si el agente lo puede atender."""
    async with async_session() as session:
        resultado = await session.execute(
            select(ConversacionDerivada).where(
                ConversacionDerivada.telefono == telefono, ConversacionDerivada.activo.is_(True)
            )
        )
        fila = resultado.scalar_one_or_none()
    if fila is None:
        return None
    return {"categoria": fila.categoria, "operador": fila.operador, "derivado_en": fila.derivado_en}


async def desmarcar_derivado(telefono: str):
    """El operador termino: el agente vuelve a poder responderle a este cliente."""
    async with async_session() as session:
        await session.execute(
            update(ConversacionDerivada)
            .where(ConversacionDerivada.telefono == telefono)
            .values(activo=False)
        )
        await session.commit()


async def mensajes_recientes_de(telefono: str, segundos: int) -> int:
    """
    Cuenta cuantos mensajes del CLIENTE (role='user') llegaron de este numero en los
    ultimos N segundos. Se usa para detectar ritmo imposible para un humano tipeando
    (loops contra otro bot).
    """
    limite = ahora() - timedelta(seconds=segundos)
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje).where(
                Mensaje.telefono == telefono, Mensaje.role == "user", Mensaje.timestamp >= limite
            )
        )
        return len(resultado.scalars().all())


async def contar_mensajes_de(telefono: str) -> int:
    """Cuenta TODOS los mensajes del cliente (role='user') de este numero, sin limite de tiempo."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje).where(Mensaje.telefono == telefono, Mensaje.role == "user")
        )
        return len(resultado.scalars().all())


class ContadorMensajes(Base):
    """
    Cuenta TODOS los mensajes entrantes de un cliente, esten derivados o no — a
    diferencia de la tabla "mensajes", que solo guarda lo que la IA realmente
    conversa (nunca lo que llega despues de derivar). Se usa para reglas basadas
    en "cada N mensajes", como el reinicio automatico del numero de pruebas.
    """

    __tablename__ = "contador_mensajes"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    cantidad: Mapped[int] = mapped_column(Integer, default=0)


async def incrementar_contador_mensajes(telefono: str) -> int:
    """Suma 1 al contador de este telefono y devuelve el nuevo total."""
    async with async_session() as session:
        resultado = await session.execute(
            select(ContadorMensajes).where(ContadorMensajes.telefono == telefono)
        )
        fila = resultado.scalar_one_or_none()
        if fila is None:
            fila = ContadorMensajes(telefono=telefono, cantidad=0)
            session.add(fila)
        fila.cantidad += 1
        nuevo_total = fila.cantidad
        await session.commit()
    return nuevo_total


async def resetear_contador_mensajes(telefono: str):
    """Vuelve el contador de este telefono a 0."""
    async with async_session() as session:
        await session.execute(delete(ContadorMensajes).where(ContadorMensajes.telefono == telefono))
        await session.commit()


async def resetear_todos_los_contadores() -> int:
    """Vuelve a 0 el contador de mensajes de TODOS los telefonos. Retorna cuantos se borraron."""
    async with async_session() as session:
        resultado = await session.execute(delete(ContadorMensajes))
        await session.commit()
    return resultado.rowcount


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")

    async with async_session() as session:
        resultado2 = await session.execute(
            delete(MensajePropio).where(MensajePropio.creado_en < limite)
        )
        await session.commit()
    if resultado2.rowcount:
        logger.info(f"Se limpiaron {resultado2.rowcount} mensajes propios de mas de {dias} dias")


async def limpiar_mensajes_viejos(dias: int = 7):
    """
    Borra los mensajes de historial de conversacion de mas de N dias.

    Con esto, el "contador de 3 mensajes" y el contexto que ve la IA arrancan solos
    de cero para conversaciones que quedaron inactivas mas de una semana, sin tener
    que borrar nada a mano.
    """
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(delete(Mensaje).where(Mensaje.timestamp < limite))
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} mensajes de historial de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()
