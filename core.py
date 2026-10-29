"""Lógica compartida del dashboard: base de datos, IMAP, CSV y sugerencias de FAQ.

Solo usa la biblioteca estándar de Python, así no hay que instalar nada más.
"""
import base64
import csv
import ctypes
import datetime as dt
import email
import imaplib
import io
import json
import math
import os
import re
import sqlite3
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(BASE, "datos")
DB_PATH = os.environ.get("DASHBOARD_DB") or os.path.join(DATOS, "dashboard.db")

ESTADOS = ("nuevo", "pedido", "borrador", "respondido", "archivado")

SCHEMA = """
CREATE TABLE IF NOT EXISTS faq (
    id INTEGER PRIMARY KEY,
    pregunta TEXT NOT NULL,
    respuesta TEXT NOT NULL,
    categoria TEXT NOT NULL DEFAULT '',
    usos INTEGER NOT NULL DEFAULT 0,
    creado TEXT,
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE,
    uid TEXT,
    remitente TEXT,
    remitente_email TEXT,
    asunto TEXT,
    fecha TEXT,
    cuerpo TEXT,
    estado TEXT NOT NULL DEFAULT 'nuevo',
    instruccion TEXT NOT NULL DEFAULT '',
    origen TEXT NOT NULL DEFAULT 'imap',
    importado TEXT
);
CREATE INDEX IF NOT EXISTS mails_uid ON mails(origen, uid);
CREATE TABLE IF NOT EXISTS borradores (
    mail_id INTEGER PRIMARY KEY REFERENCES mails(id) ON DELETE CASCADE,
    texto TEXT NOT NULL DEFAULT '',
    faq_ids TEXT NOT NULL DEFAULT '[]',
    autor TEXT NOT NULL DEFAULT 'manual',
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS notas (
    id INTEGER PRIMARY KEY,
    mail_id INTEGER NOT NULL REFERENCES mails(id) ON DELETE CASCADE,
    texto TEXT NOT NULL,
    creado TEXT
);
CREATE TABLE IF NOT EXISTS ejemplos (
    id INTEGER PRIMARY KEY,
    mail_id INTEGER UNIQUE REFERENCES mails(id) ON DELETE SET NULL,
    asunto TEXT,
    consulta TEXT NOT NULL,
    texto_ia TEXT,
    texto_final TEXT NOT NULL,
    faq_id INTEGER,
    similitud REAL,
    usar INTEGER NOT NULL DEFAULT 1,
    creado TEXT
);
CREATE TABLE IF NOT EXISTS config (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""

CONFIG_DEFECTO = {
    "correo_metodo": "rest",  # "rest" = API del webmail Zimbra (HTTPS); "imap" = IMAP
    "imap_host": "correo.mrecic.gov.ar",
    "imap_puerto": "993",
    "imap_usuario": "",
    "imap_carpeta": "INBOX",
    "imap_dias": "30",
    "imap_verificar_ssl": "1",
    "firma": "",
    "tono": "Cordial y formal, tratando de usted. Respuestas claras y breves.",
    "ia_proveedor": "1min",  # "1min" o "gemini"
    "ia_modelo": "gpt-4.1-mini",  # modelo de 1min.ai
    "gemini_modelo": "gemini-2.5-flash",
    "ia_auto": "1",
    # Aprendizaje: reglas de estilo deducidas de las correcciones (una por línea, editables)
    "preferencias": "",
    "aprendizaje_auto": "1",
    "aprendizaje_ultimo_ejemplo": "0",
    "aprendizaje_resumen": "",
    "aprendizaje_fecha": "",
}

# Claves que se guardan cifradas y nunca se devuelven a la interfaz
SECRETOS = ("imap_password", "ia_key", "gemini_key")


def ahora():
    return dt.datetime.now().isoformat(timespec="seconds")


def conectar():
    os.makedirs(DATOS, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    columnas = {r["name"] for r in con.execute("PRAGMA table_info(borradores)")}
    if "analisis" not in columnas:
        con.execute("ALTER TABLE borradores ADD COLUMN analisis TEXT")
    if "texto_ia" not in columnas:  # lo que escribió la IA, para comparar con la versión final
        con.execute("ALTER TABLE borradores ADD COLUMN texto_ia TEXT")
    return con


# ---------------------------------------------------------------- config

def leer_config(con, incluir_secretos=False):
    cfg = dict(CONFIG_DEFECTO)
    for fila in con.execute("SELECT clave, valor FROM config"):
        cfg[fila["clave"]] = fila["valor"]
    for clave in SECRETOS:
        cifrado = cfg.pop(clave, "")
        if incluir_secretos and cifrado:
            cfg[clave] = desproteger(cifrado)
        cfg[clave + "_guardada"] = bool(cifrado)
    return cfg


def guardar_config(con, datos):
    for clave, valor in datos.items():
        if clave in SECRETOS:
            if not valor:
                continue  # vacío = conservar el valor guardado
            valor = proteger(valor.strip())
        elif clave not in CONFIG_DEFECTO:
            continue
        con.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES (?, ?)", (clave, str(valor)))
    con.commit()


# La contraseña se cifra con DPAPI de Windows: solo tu usuario de Windows en esta PC puede descifrarla.

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(datos, cifrar):
    buf = ctypes.create_string_buffer(datos, len(datos))
    entrada = _Blob(len(datos), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    salida = _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if cifrar else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(entrada), None, None, None, None, 0, ctypes.byref(salida)):
        raise OSError("No se pudo cifrar/descifrar la contraseña con Windows")
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


def proteger(texto):
    return base64.b64encode(_dpapi(texto.encode("utf-8"), True)).decode("ascii")


def desproteger(valor):
    return _dpapi(base64.b64decode(valor), False).decode("utf-8")


# ---------------------------------------------------------------- IMAP

class _HtmlATexto(HTMLParser):
    BLOQUES = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__()
        self.partes = []
        self._omitir = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._omitir += 1
        elif tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._omitir = max(0, self._omitir - 1)
        elif tag in self.BLOQUES:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._omitir:
            self.partes.append(data)


def html_a_texto(html):
    p = _HtmlATexto()
    p.feed(html)
    texto = "".join(p.partes).replace("\xa0", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", texto).strip()


def _decodificar_header(valor):
    if not valor:
        return ""
    try:
        return str(make_header(decode_header(valor)))
    except Exception:
        return valor


def _contenido(parte):
    carga = parte.get_payload(decode=True) or b""
    charset = parte.get_content_charset() or "utf-8"
    try:
        return carga.decode(charset, errors="replace")
    except LookupError:
        return carga.decode("latin-1", errors="replace")


def extraer_cuerpo(msg):
    plano, html = None, None
    for parte in msg.walk() if msg.is_multipart() else [msg]:
        if parte.get_content_disposition() == "attachment":
            continue
        tipo = parte.get_content_type()
        if tipo == "text/plain" and plano is None:
            plano = _contenido(parte)
        elif tipo == "text/html" and html is None:
            html = _contenido(parte)
    if plano and plano.strip():
        return plano.strip()
    return html_a_texto(html) if html else ""


_MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _guardar_mensaje(con, crudo, uid_txt):
    """Guarda un mail (MIME crudo) si no existía. Devuelve el id nuevo o None."""
    msg = email.message_from_bytes(crudo)
    nombre, direccion = parseaddr(_decodificar_header(msg.get("From")))
    try:
        fecha = parsedate_to_datetime(msg.get("Date")).astimezone().isoformat(timespec="minutes")
    except Exception:
        fecha = ahora()
    message_id = (msg.get("Message-ID") or "").strip() or uid_txt
    cur = con.execute(
        """INSERT OR IGNORE INTO mails
           (message_id, uid, remitente, remitente_email, asunto, fecha, cuerpo, origen, importado)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'imap', ?)""",
        (message_id, uid_txt, nombre or direccion, direccion,
         _decodificar_header(msg.get("Subject")) or "(sin asunto)", fecha, extraer_cuerpo(msg), ahora()),
    )
    con.commit()
    return cur.lastrowid if cur.rowcount else None


def sincronizar(con):
    """Trae los mails recientes en modo solo lectura (no los marca como leídos)."""
    cfg = leer_config(con, incluir_secretos=True)
    if not cfg.get("imap_usuario") or not cfg.get("imap_password"):
        raise ValueError("Falta configurar el usuario y la contraseña del correo (pestaña Configuración).")

    contexto = ssl.create_default_context()
    if cfg.get("imap_verificar_ssl") != "1":
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE

    desde = dt.date.today() - dt.timedelta(days=int(cfg.get("imap_dias") or 30))
    if cfg.get("correo_metodo") != "imap":
        return _sincronizar_rest(con, cfg, contexto, desde)
    criterio = f"{desde.day:02d}-{_MESES[desde.month - 1]}-{desde.year}"
    carpeta = cfg.get("imap_carpeta") or "INBOX"

    nuevos = []
    M = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_puerto"] or 993), ssl_context=contexto, timeout=30)
    try:
        M.login(cfg["imap_usuario"], cfg["imap_password"])
        typ, _ = M.select(f'"{carpeta}"', readonly=True)
        if typ != "OK":
            raise ValueError(f"No se encontró la carpeta '{carpeta}'.")
        typ, data = M.uid("search", None, "SINCE", criterio)
        uids = data[0].split() if data and data[0] else []
        conocidos = {r["uid"] for r in con.execute("SELECT uid FROM mails WHERE origen='imap'")}
        for uid in uids:
            uid_txt = f"{carpeta}:{uid.decode()}"
            if uid_txt in conocidos:
                continue
            typ, partes = M.uid("fetch", uid, "(BODY.PEEK[])")
            crudo = next((p[1] for p in partes if isinstance(p, tuple)), None)
            if typ != "OK" or crudo is None:
                continue
            nuevo = _guardar_mensaje(con, crudo, uid_txt)
            if nuevo:
                nuevos.append(nuevo)
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"nuevos": len(nuevos), "ids": nuevos, "revisados": len(uids)}


def _sincronizar_rest(con, cfg, contexto, desde):
    """Lee la carpeta con la API REST de Zimbra (la misma puerta HTTPS que el webmail).

    Solo hace pedidos GET: no envía, no borra y no marca los mails como leídos.
    """
    base = f"https://{cfg['imap_host'].strip().rstrip('/')}/home/~"
    carpeta = cfg.get("imap_carpeta") or "INBOX"
    ruta_carpeta = "inbox" if carpeta.upper() == "INBOX" else urllib.parse.quote(carpeta.strip("/"))
    auth = "Basic " + base64.b64encode(f"{cfg['imap_usuario']}:{cfg['imap_password']}".encode("utf-8")).decode("ascii")

    def pedir(url):
        req = urllib.request.Request(url, headers={"Authorization": auth, "User-Agent": "DashboardMails"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=contexto) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ValueError("El correo rechazó el usuario o la contraseña. Revisalos en Configuración "
                                 "(probá con el correo completo, ej. usuario@mrecic.gov.ar).") from None
            if e.code == 404:
                raise ValueError(f"No se encontró la carpeta '{carpeta}' en el correo.") from None
            raise

    lista = json.loads(pedir(f"{base}/{ruta_carpeta}?fmt=json&limit=500") or b"{}")
    limite_ms = dt.datetime.combine(desde, dt.time()).timestamp() * 1000
    mensajes = [m for m in lista.get("m", []) if m.get("d", 0) >= limite_ms]
    conocidos = {r["uid"] for r in con.execute("SELECT uid FROM mails WHERE origen='imap'")}
    nuevos = []
    for m in sorted(mensajes, key=lambda x: x.get("d", 0)):
        uid_txt = f"rest:{m['id']}"
        if uid_txt in conocidos:
            continue
        nuevo = _guardar_mensaje(con, pedir(f"{base}/?id={urllib.parse.quote(str(m['id']))}"), uid_txt)
        if nuevo:
            nuevos.append(nuevo)
    return {"nuevos": len(nuevos), "ids": nuevos, "revisados": len(mensajes)}


# ---------------------------------------------------------------- CSV

def _decodificar_csv(datos):
    for codif in ("utf-8-sig", "cp1252"):
        try:
            return datos.decode(codif)
        except UnicodeDecodeError:
            continue
    return datos.decode("latin-1")


def importar_csv(con, datos):
    texto = _decodificar_csv(datos)
    # El separador se toma de la primera línea: csv.Sniffer se confunde con respuestas de varias líneas
    # que tienen comillas adentro, y adivina mal cómo están escapadas.
    primera = texto.split("\n", 1)[0]
    separador = max(";,\t", key=primera.count)
    filas = [f for f in csv.reader(io.StringIO(texto), csv.excel, delimiter=separador) if any(c.strip() for c in f)]
    if not filas:
        return {"importadas": 0, "repetidas": 0, "omitidas": 0}

    cab = [normalizar(c) for c in filas[0]]

    def buscar(*claves):
        return next((i for i, c in enumerate(cab) if any(k in c for k in claves)), None)

    i_preg = buscar("pregunta", "question", "consulta", "tema")
    i_resp = buscar("respuesta", "answer", "contestacion")
    i_cat = buscar("categoria", "category", "seccion")
    if i_preg is not None and i_resp is not None:
        filas = filas[1:]
    else:
        i_preg, i_resp, i_cat = 0, 1, None

    existentes = {normalizar(r["pregunta"]) for r in con.execute("SELECT pregunta FROM faq")}
    importadas = repetidas = omitidas = 0
    for f in filas:
        if len(f) <= max(i_preg, i_resp):
            omitidas += 1
            continue
        preg, resp = f[i_preg].strip(), f[i_resp].strip()
        if not preg or not resp:
            omitidas += 1
            continue
        if normalizar(preg) in existentes:
            repetidas += 1
            continue
        cat = f[i_cat].strip() if i_cat is not None and len(f) > i_cat else ""
        con.execute(
            "INSERT INTO faq (pregunta, respuesta, categoria, creado, actualizado) VALUES (?, ?, ?, ?, ?)",
            (preg, resp, cat, ahora(), ahora()),
        )
        existentes.add(normalizar(preg))
        importadas += 1
    con.commit()
    return {"importadas": importadas, "repetidas": repetidas, "omitidas": omitidas}


def exportar_csv(con):
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    w.writerow(["pregunta", "respuesta", "categoria"])
    for r in con.execute("SELECT pregunta, respuesta, categoria FROM faq ORDER BY categoria, id"):
        w.writerow([r["pregunta"], r["respuesta"], r["categoria"]])
    return "﻿" + salida.getvalue()  # BOM para que Excel lea bien los acentos


# ---------------------------------------------------------------- sugerencias

_VACIAS = set("""a al algo algun alguna ante antes aqui asi aun bien buen buenas buenos como con
contra cual cuales cuando de del desde donde dos el ella ellos en entre era es esa ese eso esta
estaba estan este esto estos favor fue gracias ha hace hay hola la las le les lo los mas me mi
mis mucho muy necesito ni no nos o otra otro para pero poco por porque puede pueden que quiero
quisiera saber se sea segun ser si sin sobre solo son su sus tambien tengo tiene todo tu un una
uno unos usted ustedes y ya yo saludos cordial cordiales atentamente estimado estimada estimados
consulta consultar quería queria quisiera mail correo""".split())


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", texto).strip()


def _tokens(texto):
    palabras = re.findall(r"[a-z0-9]{3,}", normalizar(texto))
    # recorte muy simple de plurales para que "visas" coincida con "visa"
    return [p[:-1] if len(p) > 4 and p.endswith("s") else p for p in palabras if p not in _VACIAS]


def sugerir_faq(con, texto, limite=5):
    faqs = con.execute("SELECT * FROM faq").fetchall()
    if not faqs:
        return []
    docs = [set(_tokens(f["pregunta"] + " " + f["pregunta"] + " " + f["categoria"] + " " + f["respuesta"][:400])) for f in faqs]
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    consulta = set(_tokens(texto))
    n = len(faqs)
    puntajes = []
    for f, d in zip(faqs, docs):
        comunes = consulta & d
        if not comunes:
            continue
        p = sum(math.log(1 + n / df[t]) for t in comunes) / math.sqrt(len(d) or 1)
        puntajes.append((p, f))
    puntajes.sort(key=lambda x: x[0], reverse=True)
    return [dict(f) | {"puntaje": round(p, 3)} for p, f in puntajes[:limite]]


# ---------------------------------------------------------------- consultas comunes

def mail_completo(con, mail_id):
    m = con.execute("SELECT * FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not m:
        return None
    b = con.execute("SELECT * FROM borradores WHERE mail_id=?", (mail_id,)).fetchone()
    notas = con.execute("SELECT * FROM notas WHERE mail_id=? ORDER BY id", (mail_id,)).fetchall()
    return {
        "mail": dict(m),
        "borrador": dict(b) | {"faq_ids": json.loads(b["faq_ids"]),
                               "analisis": json.loads(b["analisis"]) if b["analisis"] else None} if b else None,
        "notas": [dict(n) for n in notas],
        "sugerencias": sugerir_faq(con, (m["asunto"] or "") + "\n" + (m["cuerpo"] or "")),
    }


def guardar_borrador(con, mail_id, texto, faq_ids=(), autor="manual", analisis=None):
    """Guarda el borrador. Si analisis es None se conserva el análisis de IA anterior (ediciones a mano)."""
    con.execute(
        """INSERT INTO borradores (mail_id, texto, faq_ids, autor, actualizado, analisis, texto_ia)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(mail_id) DO UPDATE SET texto=excluded.texto, faq_ids=excluded.faq_ids,
           autor=excluded.autor, actualizado=excluded.actualizado,
           analisis=COALESCE(excluded.analisis, borradores.analisis),
           texto_ia=COALESCE(excluded.texto_ia, borradores.texto_ia)""",
        (mail_id, texto, json.dumps(list(faq_ids)), autor, ahora(),
         json.dumps(analisis, ensure_ascii=False) if analisis is not None else None,
         texto if autor in ("ia", "claude") else None),
    )
    con.execute("UPDATE mails SET estado='borrador' WHERE id=? AND estado IN ('nuevo', 'pedido')", (mail_id,))
    con.commit()
