#!/usr/bin/env python3
"""
audit.py — Auditoria del repo-plantilla whatsapp-agentkit.

Este repo no contiene el agente: contiene las INSTRUCCIONES (CLAUDE.md) con las
que Claude Code genera el agente para el negocio del usuario. Por eso el codigo
que hay que auditar vive dentro de bloques markdown, y un error de sintaxis ahi
no lo detecta nadie hasta que un usuario real lo sufre.

Este script corre 6 chequeos:

  1. Los bloques ```python de CLAUDE.md compilan
  2. Los bloques ```yaml de CLAUDE.md parsean
  3. No quedan rastros de proveedores eliminados (twilio, whapi)
  4. Toda variable de entorno usada en CLAUDE.md esta en .env.example
  5. Los links del README responden
  6. Las imagenes referenciadas en el README existen y miden lo que dicen

Uso:
    python3 scripts/audit.py
    python3 scripts/audit.py --skip-links
    python3 scripts/audit.py --repo /ruta/al/repo

Dependencias: libreria estandar + pyyaml (ya es dependencia del proyecto).
Exit code: 0 si todo pasa, 1 si algun chequeo tiene errores.
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: falta pyyaml. Instalalo con:  pip install pyyaml")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
# Configuracion
# ══════════════════════════════════════════════════════════════════════════

# Terminos que NO deben aparecer en ningun archivo del repo (chequeo 3).
# Ordenados de mas largo a mas corto para reportar el match mas especifico.
TERMINOS_PROHIBIDOS = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "twilio",
    "whapi",
]

# Carpetas que nunca se recorren.
DIRS_IGNORADOS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", ".idea", ".vscode",
}

# Rutas relativas al repo que se excluyen del grep del chequeo 3.
RUTAS_IGNORADAS = {
    "docs/assets",   # binarios de imagen
    ".env",          # secretos locales del usuario, no viaja a GitHub
}

# Variables de entorno del sistema, no del proyecto (chequeo 4).
ENV_DEL_SISTEMA = {"PATH", "HOME", "USER", "PWD", "LANG"}

# Varios sitios rechazan el User-Agent por defecto de urllib (chequeo 5).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_HTTP = 10

# Ancho minimo recomendado para una imagen del README.
ANCHO_MINIMO_PNG = 600

# Cuantas coincidencias se listan por archivo antes de resumir.
MAX_HITS_POR_ARCHIVO = 6


# ══════════════════════════════════════════════════════════════════════════
# Resultado de un chequeo
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Resultado:
    """Lo que devuelve cada chequeo: errores rompen (exit 1), advertencias no."""

    numero: int
    titulo: str
    errores: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
    omitido: bool = False

    @property
    def paso(self) -> bool:
        return not self.errores

    def error(self, mensaje: str, que_hacer: str = "") -> None:
        if que_hacer:
            mensaje = f"{mensaje}\n           -> {que_hacer}"
        self.errores.append(mensaje)

    def advertir(self, mensaje: str, que_hacer: str = "") -> None:
        if que_hacer:
            mensaje = f"{mensaje}\n           -> {que_hacer}"
        self.advertencias.append(mensaje)


# ══════════════════════════════════════════════════════════════════════════
# Utilidades de archivos
# ══════════════════════════════════════════════════════════════════════════

def leer_texto(ruta: Path) -> str:
    """Lee un archivo de texto en UTF-8. Lanza FileNotFoundError si no existe."""
    return ruta.read_text(encoding="utf-8")


def es_binario(ruta: Path) -> bool:
    """
    Heuristica: si el primer bloque tiene un byte nulo o no es UTF-8, es binario.

    Usamos un decodificador incremental a proposito: leer 4096 bytes puede
    cortar un caracter multibyte por la mitad (los recuadros ── de este repo
    ocupan 3 bytes), y un decode() comun lo tomaria como binario, salteando
    el archivo del chequeo 3 sin decir nada.
    """
    try:
        with ruta.open("rb") as f:
            trozo = f.read(4096)
        if b"\x00" in trozo:
            return True
        codecs.getincrementaldecoder("utf-8")().decode(trozo, final=False)
        return False
    except (UnicodeDecodeError, OSError):
        return True


def recorrer_archivos_de_texto(raiz: Path, excluir: set[Path]) -> list[Path]:
    """
    Devuelve todos los archivos de texto del repo, salteando carpetas ignoradas,
    binarios y las rutas de `excluir` (rutas ABSOLUTAS ya resueltas).
    """
    encontrados: list[Path] = []
    for carpeta, subcarpetas, archivos in os.walk(raiz):
        subcarpetas[:] = [d for d in subcarpetas if d not in DIRS_IGNORADOS]
        carpeta_path = Path(carpeta)
        for nombre in archivos:
            ruta = carpeta_path / nombre
            try:
                absoluta = ruta.resolve()
            except OSError:
                continue
            # Comparacion por ruta absoluta: nunca por substring del nombre,
            # asi este mismo script no se auto-reporta ni excluye a otros.
            if absoluta in excluir:
                continue
            if any(absoluta == e or e in absoluta.parents for e in excluir):
                continue
            if es_binario(ruta):
                continue
            encontrados.append(ruta)
    return sorted(encontrados)


def relativa(ruta: Path, raiz: Path) -> str:
    """Ruta legible relativa al repo (o absoluta si esta fuera)."""
    try:
        return str(ruta.resolve().relative_to(raiz))
    except ValueError:
        return str(ruta)


# ══════════════════════════════════════════════════════════════════════════
# Extraccion de bloques de codigo de un markdown
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class BloqueCodigo:
    lenguaje: str
    codigo: str
    linea_apertura: int   # linea (1-based) donde esta la ``` de apertura
    linea_inicio: int     # linea (1-based) de la primera linea de codigo


ABRE_FENCE = re.compile(r"^(?P<indent>\s*)```(?P<lang>[A-Za-z0-9_+-]*)\s*$")
CIERRA_FENCE = re.compile(r"^\s*```\s*$")


def extraer_bloques(markdown: str, lenguaje: str) -> list[BloqueCodigo]:
    """
    Extrae los bloques ```<lenguaje> de un markdown, conservando el numero de
    linea REAL dentro del archivo (no la linea relativa al bloque).

    Maneja fences indentados (los que estan dentro de una lista numerada) y
    les quita la indentacion comun para que el contenido compile igual.
    """
    bloques: list[BloqueCodigo] = []
    lineas = markdown.splitlines()

    dentro = False
    lang_actual = ""
    indent_actual = 0
    linea_apertura = 0
    buffer: list[str] = []

    for i, linea in enumerate(lineas, start=1):
        if not dentro:
            m = ABRE_FENCE.match(linea)
            if m:
                dentro = True
                lang_actual = m.group("lang").lower()
                indent_actual = len(m.group("indent"))
                linea_apertura = i
                buffer = []
            continue

        # Estamos dentro de un bloque: buscamos el cierre.
        if CIERRA_FENCE.match(linea):
            if lang_actual == lenguaje.lower():
                # Quitamos la indentacion del fence a cada linea del cuerpo.
                cuerpo = [
                    l[indent_actual:] if l[:indent_actual].strip() == "" else l
                    for l in buffer
                ]
                bloques.append(BloqueCodigo(
                    lenguaje=lang_actual,
                    codigo="\n".join(cuerpo),
                    linea_apertura=linea_apertura,
                    linea_inicio=linea_apertura + 1,
                ))
            dentro = False
            lang_actual = ""
            buffer = []
            continue

        buffer.append(linea)

    return bloques


def parece_fragmento(codigo: str) -> bool:
    """
    True si el bloque es obviamente ilustrativo y no codigo completo:
    tiene un '...' como linea propia o un comentario tipo '# ...'.
    """
    for linea in codigo.splitlines():
        limpia = linea.strip()
        if limpia == "..." or limpia == "# ...":
            return True
        if limpia.startswith("# ...") or limpia.endswith("..."):
            if limpia.startswith("#") or limpia == "...":
                return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 1 — Los bloques Python de CLAUDE.md compilan
# ══════════════════════════════════════════════════════════════════════════

def chequeo_1_python(raiz: Path) -> Resultado:
    r = Resultado(1, "Bloques Python de CLAUDE.md compilan")
    ruta = raiz / "CLAUDE.md"

    try:
        contenido = leer_texto(ruta)
    except FileNotFoundError:
        r.error("falta el archivo CLAUDE.md",
                "CLAUDE.md es el cerebro del repo-plantilla: sin el, "
                "'/build-agent' no tiene instrucciones que seguir.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer CLAUDE.md: {e}")
        return r

    bloques = extraer_bloques(contenido, "python")
    if not bloques:
        r.advertir("no se encontro ningun bloque ```python en CLAUDE.md",
                   "Revisa que las plantillas de codigo esten marcadas como "
                   "```python y no como ``` a secas.")
        return r

    fallados = 0
    for b in bloques:
        nombre = f"CLAUDE.md:bloque@{b.linea_apertura}"
        try:
            compile(b.codigo, nombre, "exec")
        except SyntaxError as e:
            # e.lineno es relativo al bloque -> lo traducimos a linea real.
            linea_real = b.linea_inicio + (e.lineno - 1 if e.lineno else 0)
            detalle = f"{type(e).__name__}: {e.msg}"
            ubicacion = (f"CLAUDE.md:{b.linea_apertura} (bloque ```python), "
                         f"error en CLAUDE.md:{linea_real}")
            if parece_fragmento(b.codigo):
                r.advertir(
                    f"{ubicacion} — {detalle}",
                    "Parece un fragmento ilustrativo (tiene '...'), no codigo "
                    "completo. Si es a proposito, ignoralo.",
                )
            else:
                fallados += 1
                r.error(
                    f"{ubicacion} — {detalle}",
                    f"Abri CLAUDE.md en la linea {linea_real} y corregi la "
                    f"sintaxis. Claude Code copia este bloque tal cual.",
                )
        except ValueError as e:
            fallados += 1
            r.error(f"CLAUDE.md:{b.linea_apertura} — el bloque no se pudo "
                    f"compilar: {e}",
                    "Suele ser un caracter invalido dentro del bloque.")

    r.notas.append(
        f"{len(bloques) - fallados}/{len(bloques)} bloques ```python compilan"
    )
    return r


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 2 — Los bloques YAML de CLAUDE.md parsean
# ══════════════════════════════════════════════════════════════════════════

def chequeo_2_yaml(raiz: Path) -> Resultado:
    r = Resultado(2, "Bloques YAML de CLAUDE.md parsean")
    ruta = raiz / "CLAUDE.md"

    try:
        contenido = leer_texto(ruta)
    except FileNotFoundError:
        r.error("falta el archivo CLAUDE.md",
                "Sin CLAUDE.md no hay plantillas que auditar.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer CLAUDE.md: {e}")
        return r

    bloques = extraer_bloques(contenido, "yaml")
    if not bloques:
        r.advertir("no se encontro ningun bloque ```yaml en CLAUDE.md",
                   "business.yaml y prompts.yaml deberian estar como ```yaml.")
        return r

    fallados = 0
    for b in bloques:
        try:
            # Los placeholders tipo [NOMBRE DEL NEGOCIO] son YAML valido
            # (una lista o un string). Solo falla si safe_load levanta.
            yaml.safe_load(b.codigo)
        except yaml.YAMLError as e:
            fallados += 1
            linea_real = b.linea_apertura
            marca = getattr(e, "problem_mark", None)
            if marca is not None:
                linea_real = b.linea_inicio + marca.line
            problema = getattr(e, "problem", None) or str(e).splitlines()[0]
            r.error(
                f"CLAUDE.md:{b.linea_apertura} (bloque ```yaml), error en "
                f"CLAUDE.md:{linea_real} — {problema}",
                f"Corregi el YAML en la linea {linea_real}. Ojo con la "
                f"indentacion y con los dos puntos sin comillas.",
            )

    r.notas.append(
        f"{len(bloques) - fallados}/{len(bloques)} bloques ```yaml parsean"
    )
    return r


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 3 — Cero rastros de proveedores eliminados
# ══════════════════════════════════════════════════════════════════════════

def chequeo_3_proveedores(raiz: Path, este_script: Path) -> Resultado:
    r = Resultado(3, "Cero rastros de proveedores eliminados (twilio, whapi)")

    patron = re.compile("|".join(re.escape(t) for t in TERMINOS_PROHIBIDOS),
                        re.IGNORECASE)

    # Este mismo script nombra los terminos que busca: se excluye comparando
    # rutas absolutas resueltas, nunca por substring del nombre de archivo.
    excluir = {este_script.resolve()}
    for rel in RUTAS_IGNORADAS:
        excluir.add((raiz / rel).resolve())

    try:
        archivos = recorrer_archivos_de_texto(raiz, excluir)
    except OSError as e:
        r.error(f"no se pudo recorrer el repo: {e}")
        return r

    total_hits = 0
    archivos_con_hits = 0

    for ruta in archivos:
        try:
            contenido = leer_texto(ruta)
        except (OSError, UnicodeDecodeError):
            continue

        hits: list[tuple[int, str, str]] = []
        for numero, linea in enumerate(contenido.splitlines(), start=1):
            m = patron.search(linea)
            if m:
                texto = linea.strip()
                if len(texto) > 110:
                    texto = texto[:107] + "..."
                hits.append((numero, m.group(0), texto))

        if not hits:
            continue

        archivos_con_hits += 1
        total_hits += len(hits)
        nombre = relativa(ruta, raiz)

        for numero, termino, texto in hits[:MAX_HITS_POR_ARCHIVO]:
            r.error(f"{nombre}:{numero} — contiene '{termino}'  |  {texto}")

        restantes = len(hits) - MAX_HITS_POR_ARCHIVO
        if restantes > 0:
            r.error(f"{nombre} — y {restantes} coincidencia(s) mas en el "
                    f"mismo archivo")

    if total_hits:
        r.errores.append(
            f"TOTAL: {total_hits} coincidencia(s) en {archivos_con_hits} "
            f"archivo(s).\n           -> Reescribi esos archivos dejando solo "
            f"los proveedores vigentes: zernio (recomendado) y meta "
            f"(Cloud API directo)."
        )
    else:
        r.notas.append(
            f"{len(archivos)} archivos de texto revisados, sin rastros de "
            f"proveedores eliminados"
        )

    return r


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 4 — Variables de entorno completas
# ══════════════════════════════════════════════════════════════════════════

# os.getenv("X")  /  os.environ.get("X")  /  os.environ["X"]
USO_ENV = re.compile(
    r"""os\.getenv\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""|os\.environ\.get\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""
)

# X=  o  # X=   dentro de .env.example
DECLARA_ENV = re.compile(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def chequeo_4_env(raiz: Path) -> Resultado:
    r = Resultado(4, "Variables de entorno completas en .env.example")
    ruta_md = raiz / "CLAUDE.md"
    ruta_env = raiz / ".env.example"

    try:
        contenido_md = leer_texto(ruta_md)
    except FileNotFoundError:
        r.error("falta el archivo CLAUDE.md",
                "Sin CLAUDE.md no se puede saber que variables usa el agente.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer CLAUDE.md: {e}")
        return r

    try:
        contenido_env = leer_texto(ruta_env)
    except FileNotFoundError:
        r.error("falta el archivo .env.example",
                "Crealo en la raiz del repo: es la plantilla que el usuario "
                "copia a .env durante el setup.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer .env.example: {e}")
        return r

    # Variables usadas en CLAUDE.md, con la linea de la primera aparicion.
    usadas: dict[str, int] = {}
    for numero, linea in enumerate(contenido_md.splitlines(), start=1):
        for m in USO_ENV.finditer(linea):
            nombre = m.group(1) or m.group(2) or m.group(3)
            if nombre in ENV_DEL_SISTEMA:
                continue
            usadas.setdefault(nombre, numero)

    # Variables declaradas en .env.example (cuenten o no como comentario).
    declaradas: dict[str, int] = {}
    for numero, linea in enumerate(contenido_env.splitlines(), start=1):
        m = DECLARA_ENV.match(linea)
        if m:
            declaradas.setdefault(m.group(1), numero)

    if not usadas:
        r.advertir("no se detecto ninguna variable de entorno en CLAUDE.md",
                   "Revisa que las plantillas usen os.getenv(\"...\").")

    faltantes = [v for v in usadas if v not in declaradas]
    for nombre in sorted(faltantes):
        r.error(
            f"{nombre} — usada en CLAUDE.md:{usadas[nombre]} pero no esta en "
            f".env.example",
            f"Agrega una linea '{nombre}=' (o '# {nombre}=' si es opcional) "
            f"en .env.example.",
        )

    sin_usar = [v for v in declaradas if v not in usadas]
    for nombre in sorted(sin_usar):
        r.advertir(
            f"{nombre} — declarada en .env.example:{declaradas[nombre]} pero "
            f"no la usa nadie en CLAUDE.md",
            "Puede ser basura de una version anterior. Si ya no aplica, "
            "borrala del .env.example.",
        )

    r.notas.append(
        f"{len(usadas)} variable(s) usadas en CLAUDE.md, "
        f"{len(declaradas)} declarada(s) en .env.example, "
        f"{len(faltantes)} sin declarar"
    )
    return r


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 5 — Los links del README responden
# ══════════════════════════════════════════════════════════════════════════

COMENTARIO_HTML = re.compile(r"<!--.*?-->", re.DOTALL)
LINK_MARKDOWN = re.compile(r"\[[^\]]*\]\(\s*(<?https?://[^)\s]+>?)")
URL_SUELTA = re.compile(r"https?://[^\s)>\]\"'`]+")


def limpiar_url(url: str) -> str:
    """Saca angulos de markdown y puntuacion pegada al final."""
    url = url.strip().strip("<>")
    while url and url[-1] in ".,;:!?)]}\"'":
        url = url[:-1]
    return url


def extraer_urls(markdown: str) -> list[str]:
    """URLs http(s) del README, sin comentarios HTML, deduplicadas en orden."""
    sin_comentarios = COMENTARIO_HTML.sub("", markdown)
    vistas: dict[str, None] = {}
    for m in LINK_MARKDOWN.finditer(sin_comentarios):
        vistas.setdefault(limpiar_url(m.group(1)), None)
    for m in URL_SUELTA.finditer(sin_comentarios):
        vistas.setdefault(limpiar_url(m.group(0)), None)
    return [u for u in vistas if u]


def pedir(url: str, metodo: str) -> int:
    """Hace la peticion y devuelve el codigo HTTP. Propaga errores de red."""
    req = urllib.request.Request(url, method=metodo)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_HTTP) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def chequeo_5_links(raiz: Path, saltear: bool) -> Resultado:
    r = Resultado(5, "Links del README responden")

    if saltear:
        r.omitido = True
        r.notas.append("omitido por --skip-links (no se toco la red)")
        return r

    ruta = raiz / "README.md"
    try:
        contenido = leer_texto(ruta)
    except FileNotFoundError:
        r.error("falta el archivo README.md",
                "Es la portada del repo en GitHub: creala en la raiz.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer README.md: {e}")
        return r

    urls = extraer_urls(contenido)
    if not urls:
        r.notas.append("el README no tiene links http(s)")
        return r

    ok = 0
    for url in urls:
        try:
            codigo = pedir(url, "HEAD")
            # Varios servidores no implementan HEAD: reintentamos con GET.
            if codigo in (403, 405, 501):
                codigo = pedir(url, "GET")
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Timeout, DNS, TLS: puede ser la red de quien corre el script.
            motivo = getattr(e, "reason", e)
            r.advertir(f"{url} — no se pudo verificar ({motivo})",
                       "Puede ser tu conexion. Abrilo a mano para confirmar.")
            continue

        if codigo in (404, 410):
            r.error(
                f"{url} — HTTP {codigo} (link roto)",
                "Actualiza o borra ese link en README.md. Ojo: algunas "
                "paginas detras de login devuelven 404 a un visitante "
                "anonimo — confirmalo abriendolo en una ventana privada.",
            )
        elif 200 <= codigo < 400 or codigo == 429:
            ok += 1
            if codigo == 429:
                r.notas.append(f"{url} — HTTP 429 (rate limit, se cuenta OK)")
        else:
            r.advertir(f"{url} — HTTP {codigo} (respuesta inesperada)",
                       "Revisalo a mano: puede ser un bloqueo temporal.")

    r.notas.append(f"{ok}/{len(urls)} links respondieron OK")
    return r


# ══════════════════════════════════════════════════════════════════════════
# CHEQUEO 6 — Las imagenes del README existen y miden lo que dicen
# ══════════════════════════════════════════════════════════════════════════

IMAGEN_MARKDOWN = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
IMAGEN_HTML = re.compile(r"""<img[^>]*\ssrc\s*=\s*["']([^"']+)["']""",
                         re.IGNORECASE)

FIRMA_PNG = b"\x89PNG\r\n\x1a\n"


def leer_dimensiones_png(ruta: Path) -> tuple[int, int]:
    """
    Lee ancho y alto directo de la cabecera IHDR del PNG, sin Pillow.

    Layout: 8 bytes de firma, 4 de longitud del chunk, 4 del tipo ("IHDR"),
    y despues ancho y alto como uint32 big-endian -> bytes 16..24.
    """
    with ruta.open("rb") as f:
        cabecera = f.read(24)
    if len(cabecera) < 24 or not cabecera.startswith(FIRMA_PNG):
        raise ValueError("no tiene una firma PNG valida")
    if cabecera[12:16] != b"IHDR":
        raise ValueError("el primer chunk no es IHDR")
    ancho, alto = struct.unpack(">II", cabecera[16:24])
    return ancho, alto


def chequeo_6_imagenes(raiz: Path) -> Resultado:
    r = Resultado(6, "Imagenes del README existen y miden lo que dicen")
    ruta = raiz / "README.md"

    try:
        contenido = leer_texto(ruta)
    except FileNotFoundError:
        r.error("falta el archivo README.md",
                "Es la portada del repo en GitHub: creala en la raiz.")
        return r
    except OSError as e:
        r.error(f"no se pudo leer README.md: {e}")
        return r

    # Las imagenes comentadas con <!-- --> no se renderizan: no cuentan.
    sin_comentarios = COMENTARIO_HTML.sub("", contenido)

    referencias: list[str] = []
    for regex in (IMAGEN_MARKDOWN, IMAGEN_HTML):
        for m in regex.finditer(sin_comentarios):
            src = m.group(1).strip().strip("<>")
            if src not in referencias:
                referencias.append(src)

    locales = [s for s in referencias
               if not s.startswith(("http://", "https://", "data:", "//"))]

    if not locales:
        r.notas.append("el README no referencia imagenes locales")
        if referencias:
            r.notas.append(f"{len(referencias)} imagen(es) remotas, no se "
                           f"verifican aca")
        return r

    for src in locales:
        # Sacamos el fragmento/query y normalizamos la ruta relativa al repo.
        limpio = src.split("#")[0].split("?")[0].lstrip("./")
        archivo = (raiz / limpio)

        if not archivo.is_file():
            r.error(f"README.md referencia '{src}' pero el archivo no existe",
                    f"Crea el archivo en {limpio} o corregi la ruta en el "
                    f"README.")
            continue

        peso_kb = archivo.stat().st_size / 1024

        if archivo.suffix.lower() != ".png":
            r.notas.append(f"{limpio} — existe ({peso_kb:.0f} KB, "
                           f"{archivo.suffix or 'sin extension'}; las "
                           f"dimensiones solo se leen de PNG)")
            continue

        try:
            ancho, alto = leer_dimensiones_png(archivo)
        except (OSError, ValueError, struct.error) as e:
            r.error(f"{limpio} — no se pudo leer la cabecera PNG: {e}",
                    "El archivo esta corrupto o no es un PNG real. "
                    "Regeneralo.")
            continue

        r.notas.append(f"{limpio} — {ancho}x{alto} px ({peso_kb:.0f} KB)")

        if ancho < ANCHO_MINIMO_PNG:
            r.advertir(
                f"{limpio} — mide {ancho}px de ancho, menos de "
                f"{ANCHO_MINIMO_PNG}px",
                "Se va a ver borroso en el README de GitHub. Exportalo mas "
                "grande (1200px de ancho anda bien).",
            )

    return r


# ══════════════════════════════════════════════════════════════════════════
# Salida
# ══════════════════════════════════════════════════════════════════════════

ANCHO = 66


def titulo(texto: str) -> None:
    print("=" * ANCHO)
    print(f"  {texto}")
    print("=" * ANCHO)


def imprimir_resultado(r: Resultado) -> None:
    print()
    print(f"[{r.numero}/6] {r.titulo}")

    for nota in r.notas:
        print(f"  ...    {nota}")

    for e in r.errores:
        print(f"  ERROR  {e}")

    for a in r.advertencias:
        print(f"  AVISO  {a}")

    if r.omitido:
        print("  OMIT   chequeo salteado")
    elif r.paso and not r.advertencias:
        print("  OK     sin problemas")
    elif r.paso:
        print("  OK     pasa, con advertencias")
    else:
        print(f"  FALLA  {len(r.errores)} error(es)")


def imprimir_resumen(resultados: list[Resultado]) -> int:
    print()
    print("-" * ANCHO)
    print("  RESUMEN")
    print("-" * ANCHO)

    for r in resultados:
        if r.omitido:
            estado = "OMIT "
        elif r.paso:
            estado = "OK   "
        else:
            estado = "FALLA"
        print(f"  [{estado}]  {r.numero}. {r.titulo}")

    total = len(resultados)
    pasaron = sum(1 for r in resultados if r.paso)
    omitidos = sum(1 for r in resultados if r.omitido)
    errores = sum(len(r.errores) for r in resultados)
    avisos = sum(len(r.advertencias) for r in resultados)

    print()
    linea = f"  {pasaron}/{total} chequeos OK"
    extras = []
    if errores:
        extras.append(f"{errores} error(es)")
    if avisos:
        extras.append(f"{avisos} advertencia(s)")
    if omitidos:
        extras.append(f"{omitidos} omitido(s)")
    if extras:
        linea += " — " + ", ".join(extras)
    print(linea)

    if errores:
        print("  La auditoria FALLA. Arregla los ERROR de arriba y volve a "
              "correrla.")
        return 1

    print("  Auditoria limpia. El repo-plantilla esta consistente.")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    este_script = Path(__file__).resolve()
    repo_por_defecto = este_script.parent.parent

    parser = argparse.ArgumentParser(
        prog="audit.py",
        description="Auditoria del repo-plantilla whatsapp-agentkit.",
    )
    parser.add_argument(
        "--repo", default=str(repo_por_defecto),
        help="Ruta a la raiz del repo (default: la carpeta padre de scripts/)",
    )
    parser.add_argument(
        "--skip-links", action="store_true",
        help="Saltea el chequeo de links (no toca la red)",
    )
    args = parser.parse_args(argv)

    raiz = Path(args.repo).expanduser().resolve()
    if not raiz.is_dir():
        print(f"ERROR: la ruta del repo no existe: {raiz}")
        return 1

    titulo("AgentKit — Auditoria del repo-plantilla")
    print(f"  Repo:  {raiz}")
    print(f"  Links: {'omitidos (--skip-links)' if args.skip_links else 'se verifican por red'}")

    # Cada chequeo va envuelto: un bug en uno no debe tumbar la auditoria.
    definiciones = [
        (1, "Bloques Python de CLAUDE.md compilan",
         lambda: chequeo_1_python(raiz)),
        (2, "Bloques YAML de CLAUDE.md parsean",
         lambda: chequeo_2_yaml(raiz)),
        (3, "Cero rastros de proveedores eliminados (twilio, whapi)",
         lambda: chequeo_3_proveedores(raiz, este_script)),
        (4, "Variables de entorno completas en .env.example",
         lambda: chequeo_4_env(raiz)),
        (5, "Links del README responden",
         lambda: chequeo_5_links(raiz, args.skip_links)),
        (6, "Imagenes del README existen y miden lo que dicen",
         lambda: chequeo_6_imagenes(raiz)),
    ]

    resultados: list[Resultado] = []
    for numero, nombre, funcion in definiciones:
        try:
            r = funcion()
        except Exception as e:  # red de seguridad: nunca un traceback crudo
            r = Resultado(numero, nombre)
            r.error(f"el chequeo reviento: {type(e).__name__}: {e}",
                    "Es un bug de scripts/audit.py, no del repo. Reportalo.")
        resultados.append(r)
        imprimir_resultado(r)

    return imprimir_resumen(resultados)


if __name__ == "__main__":
    sys.exit(main())
