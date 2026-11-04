"""Agenda de turnos con el calendario de Zimbra.

- Al redactar, si el mail propone un día y una hora, se consulta el libre/ocupado de Zimbra
  (solo horarios, sin el contenido de las citas) y se le dice a la IA si puede confirmar o qué alternativas ofrecer.
- Al confirmar el operador, se crea la cita en el calendario de Zimbra. La cita no lleva invitados,
  así que Zimbra no le envía nada a nadie.
"""
import datetime as dt
import json
import re
import uuid

import core

PASO_MIN = 30  # granularidad de las alternativas que se ofrecen
DIAS_ALTERNATIVAS = 10  # días hábiles hacia adelante en los que se buscan alternativas
NOMBRE_DIA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Palabras que sugieren que el mail habla de un turno, un día o una hora; si no aparece ninguna no se consulta a la IA
_PISTAS = re.compile(
    r"\b(turno|cita|appointment|reserv|agend|disponib|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|"
    r"monday|tuesday|wednesday|thursday|friday|ma[nñ]ana|tomorrow|semana|week|"
    r"\d{1,2}[:.h]\d{2}|\d{1,2}\s*(hs|h|am|pm)|\d{1,2}/\d{1,2})", re.I)


class ErrorAgenda(Exception):
    pass


# ---------------------------------------------------------------- configuración

def reglas(cfg):
    desde = dt.time.fromisoformat(cfg.get("agenda_desde") or "09:00")
    hasta = dt.time.fromisoformat(cfg.get("agenda_hasta") or "13:00")
    dias = {int(d) for d in (cfg.get("agenda_dias") or "1,2,3,4,5").split(",") if d.strip()}
    try:
        duraciones = json.loads(cfg.get("agenda_duraciones") or "{}")
    except ValueError:
        duraciones = {}
    return {"desde": desde, "hasta": hasta, "dias": dias, "duracion": int(cfg.get("agenda_duracion") or 30),
            "duraciones": {k: int(v) for k, v in duraciones.items() if str(v).isdigit()},
            "reserva": int(cfg.get("agenda_reserva_diaria") or 0)}


def duracion(r, categoria):
    return r["duraciones"].get(categoria or "", r["duracion"])


def describir(inicio, fin=None):
    texto = f"{NOMBRE_DIA[inicio.weekday()]} {inicio:%d/%m} a las {inicio:%H:%M}"
    return texto + (f" (hasta las {fin:%H:%M})" if fin else "")


# ---------------------------------------------------------------- Zimbra

def _utc(t):
    return t.astimezone(dt.timezone.utc)


def ocupado(cfg, desde, hasta):
    """Bloques ocupados [(inicio, fin)] en hora local, según el libre/ocupado de Zimbra."""
    ini_ms, fin_ms = int(desde.timestamp() * 1000), int(hasta.timestamp() * 1000)
    try:
        texto = core.zimbra(cfg, f"/?fmt=ifb&start={ini_ms}&end={fin_ms}").decode("utf-8", errors="replace")
    except ValueError as e:
        raise ErrorAgenda(str(e)) from None
    except Exception as e:
        raise ErrorAgenda(f"No se pudo leer la agenda de Zimbra: {e}") from None
    bloques = []
    for a, b in re.findall(r"^FREEBUSY[^:]*:(\d{8}T\d{6}Z)/(\d{8}T\d{6}Z)", texto.replace("\r", ""), re.M):
        f = lambda s: dt.datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).astimezone()
        bloques.append((f(a), f(b)))
    return bloques


def _se_superpone(inicio, fin, bloques):
    return any(a < fin and inicio < b for a, b in bloques)


def _dentro_de_horario(r, inicio, fin):
    return (inicio.isoweekday() in r["dias"] and inicio.date() == fin.date()
            and inicio.time() >= r["desde"] and fin.time() <= r["hasta"])


def minutos_libres_del_dia(r, dia, bloques):
    """Minutos libres que tiene un día dentro del horario de atención."""
    a = dia.replace(hour=r["desde"].hour, minute=r["desde"].minute, second=0, microsecond=0)
    b = dia.replace(hour=r["hasta"].hour, minute=r["hasta"].minute, second=0, microsecond=0)
    ocupados = sorted((max(x, a), min(y, b)) for x, y in bloques if x < b and a < y)
    total, cursor = 0, a
    for x, y in ocupados:  # se unen los bloques superpuestos para no contarlos dos veces
        if y > cursor:
            total += (y - max(x, cursor)).total_seconds() / 60
            cursor = max(cursor, y)
    return (b - a).total_seconds() / 60 - total


def _respeta_reserva(r, inicio, minutos, bloques):
    """True si, después de dar este turno, al día le siguen quedando los minutos libres de reserva."""
    return minutos_libres_del_dia(r, inicio, bloques) - minutos >= r["reserva"]


def revisar(cfg, inicio, minutos):
    """Revisa un horario pedido. Devuelve el estado y, si no se puede, alternativas libres cercanas."""
    r = reglas(cfg)
    fin = inicio + dt.timedelta(minutes=minutos)
    ahora = dt.datetime.now().astimezone()
    resultado = {"inicio": inicio.isoformat(timespec="minutes"), "fin": fin.isoformat(timespec="minutes"),
                 "minutos": minutos, "descripcion": describir(inicio, fin)}
    bloques = ocupado(cfg, min(inicio, ahora).replace(hour=0, minute=0),
                      inicio + dt.timedelta(days=DIAS_ALTERNATIVAS * 2))
    if inicio < ahora:
        estado = "pasado"
    elif not _dentro_de_horario(r, inicio, fin):
        estado = "fuera_de_horario"
    elif _se_superpone(inicio, fin, bloques):
        estado = "ocupado"
    elif not _respeta_reserva(r, inicio, minutos, bloques):
        estado = "dia_completo"  # el horario está libre, pero el día quedaría sin su reserva de minutos libres
    else:
        estado = "libre"
    resultado["estado"] = estado
    if estado != "libre":
        # Desde el comienzo del día pedido, así se ofrecen también horarios más temprano ese mismo día
        dia = inicio.replace(hour=0, minute=0, second=0, microsecond=0)
        resultado["alternativas"] = alternativas(r, max(dia, ahora), minutos, bloques)
    return resultado


def alternativas(r, desde, minutos, bloques, cantidad=3):
    """Primeros horarios libres desde 'desde', en el horario de atención, cada PASO_MIN minutos."""
    if not r["dias"]:
        return []
    t = desde.replace(second=0, microsecond=0)
    t += dt.timedelta(minutes=(-t.minute) % PASO_MIN)
    opciones, dias_vistos = [], set()
    limite = t + dt.timedelta(days=DIAS_ALTERNATIVAS * 3)  # corte de seguridad
    while len(opciones) < cantidad and len(dias_vistos) <= DIAS_ALTERNATIVAS and t < limite:
        fin = t + dt.timedelta(minutes=minutos)
        if t.isoweekday() in r["dias"]:
            dias_vistos.add(t.date())
            if t.time() < r["desde"]:
                t = t.replace(hour=r["desde"].hour, minute=r["desde"].minute)
                continue
            if (_dentro_de_horario(r, t, fin) and not _se_superpone(t, fin, bloques)
                    and _respeta_reserva(r, t, minutos, bloques)):
                opciones.append({"inicio": t.isoformat(timespec="minutes"), "descripcion": describir(t)})
                t = fin  # la siguiente opción, después de esta
                continue
        if t.time() >= r["hasta"] or t.isoweekday() not in r["dias"]:
            t = (t + dt.timedelta(days=1)).replace(hour=r["desde"].hour, minute=r["desde"].minute)
        else:
            t += dt.timedelta(minutes=PASO_MIN)
    return opciones


def crear_cita(cfg, inicio, fin, titulo, descripcion):
    """Crea la cita en el calendario de Zimbra (importando un evento iCalendar, sin invitados)."""
    uid = f"dashboard-{uuid.uuid4()}"
    escapar = lambda s: re.sub(r"([,;\\])", r"\\\1", s).replace("\n", "\\n")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Dashboard Mails//ES", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{_utc(dt.datetime.now().astimezone()):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{_utc(inicio):%Y%m%dT%H%M%SZ}", f"DTEND:{_utc(fin):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{escapar(titulo)}", f"DESCRIPTION:{escapar(descripcion)}",
        "STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR", ""])
    try:
        core.zimbra(cfg, "/calendar?fmt=ics", metodo="POST", datos=ics.encode("utf-8"), tipo="text/calendar; charset=utf-8")
    except ValueError as e:
        raise ErrorAgenda(str(e)) from None
    except Exception as e:
        raise ErrorAgenda(f"Zimbra no aceptó la cita: {e}") from None
    return uid


# ---------------------------------------------------------------- detección del turno pedido

SISTEMA_TURNO = """Extraés de un correo enviado a la sección consular de una embajada si la persona pide, propone o confirma un turno (cita presencial), y para qué día y hora.

Reglas:
- La referencia para fechas relativas ("el martes", "el próximo lunes", "mañana", "la semana que viene", "next Monday") es SIEMPRE la FECHA DE ENVÍO DEL CORREO que se indica arriba. Nunca uses fechas de mensajes anteriores citados. "El próximo lunes" es el primer lunes posterior a la fecha de envío.
- Nunca devuelvas una fecha anterior a la fecha de envío.
- Si da un rango horario ("entre las 9:30 y las 11", "de 10 a 12", "antes de las 11"), poné el comienzo en "hora" y el final en "hora_hasta". Si da una sola hora, "hora_hasta" va en null.
- Si propone varias opciones, ponelas todas en "opciones", en el orden en que las da.
- Si menciona un día sin hora, dejá "hora" en null. Si no propone ningún día concreto, "opciones" va vacía.
- No inventes fechas: solo las que la persona escribió.
- "categoria": elegí, de la lista dada, la categoría del trámite para el que pide el turno, o null.

Devolvé SOLO un JSON: {"pide_turno": true|false, "opciones": [{"fecha": "AAAA-MM-DD", "hora": "HH:MM" | null, "hora_hasta": "HH:MM" | null}], "categoria": "<categoría>" | null}"""

# Comienzo de un mensaje anterior citado al responder ("On ... wrote:", "El ... escribió:", "-----Mensaje original-----", etc.)
_CITA = re.compile(r"^\s*(on .{5,200}wrote:|el .{5,200}escribi[oó]:|-{3,}\s*(mensaje original|original message)|"
                   r"de:\s.+|from:\s.+|enviado el:|sent:\s)", re.I)


def solo_mensaje_nuevo(texto):
    """Quita la parte citada de mensajes anteriores: sus fechas no tienen que confundir a la IA."""
    lineas = []
    for linea in (texto or "").splitlines():
        if _CITA.match(linea):
            break
        if not linea.lstrip().startswith(">"):
            lineas.append(linea)
    return "\n".join(lineas).strip() or (texto or "")


def _revisar_opcion(cfg, o, minutos):
    """Revisa una opción pedida: una hora puntual o un rango (prueba cada horario del rango en el que el turno entra completo)."""
    inicio = dt.datetime.fromisoformat(f"{o['fecha']}T{o['hora']}").astimezone()
    if not o.get("hora_hasta"):
        return revisar(cfg, inicio, minutos)
    hasta = dt.datetime.fromisoformat(f"{o['fecha']}T{o['hora_hasta']}").astimezone()
    if hasta - inicio < dt.timedelta(minutes=minutos):  # rango más corto que el turno: se prueba solo el comienzo
        hasta = inicio + dt.timedelta(minutes=minutos)
    rango = f"{NOMBRE_DIA[inicio.weekday()]} {inicio:%d/%m} entre las {inicio:%H:%M} y las {hasta:%H:%M}"
    pruebas, t = [], inicio
    while t + dt.timedelta(minutes=minutos) <= hasta:
        rev = revisar(cfg, t, minutos)
        if rev["estado"] == "libre":
            return rev | {"pedido": rango}
        pruebas.append(rev)
        t += dt.timedelta(minutes=PASO_MIN)
    estados = {p["estado"] for p in pruebas}
    estado = estados.pop() if len(estados) == 1 else "ocupado"  # si los motivos se mezclan, se informa "ocupado"
    return {"descripcion": rango, "estado": estado, "minutos": minutos,
            "alternativas": pruebas[0].get("alternativas", []) if pruebas else []}


def analizar_mail(con, mail):
    """Si el mail propone un día y una hora para un turno, revisa la agenda. Devuelve un dict o None."""
    import ia

    cfg = core.leer_config(con, incluir_secretos=True)
    if cfg.get("agenda_activa") != "1":
        return None
    texto = f"{mail['asunto'] or ''}\n{solo_mensaje_nuevo(mail['cuerpo'])}"
    if not _PISTAS.search(texto):
        return None
    try:
        fecha_mail = dt.datetime.fromisoformat(mail["fecha"]).astimezone()
    except (TypeError, ValueError):
        fecha_mail = dt.datetime.now().astimezone()
    categorias = sorted({r["categoria"] for r in con.execute("SELECT DISTINCT categoria FROM faq") if r["categoria"]})
    usuario = (f"FECHA DE ENVÍO DEL CORREO: {NOMBRE_DIA[fecha_mail.weekday()]} {fecha_mail:%Y-%m-%d}\n"
               f"CATEGORÍAS: {', '.join(categorias) or '(ninguna)'}\n\nCORREO (solo el mensaje nuevo):\n{texto[:6000]}")
    datos, _ = ia.completar_json(con, SISTEMA_TURNO, usuario, max_tokens=800)
    opciones = [o for o in datos.get("opciones") or [] if isinstance(o, dict) and o.get("fecha")]
    # Red de seguridad: una fecha anterior al envío del mail es un error de lectura (por ejemplo, de un mensaje citado)
    opciones = [o for o in opciones if o["fecha"] >= f"{fecha_mail:%Y-%m-%d}"]
    if not datos.get("pide_turno") or not opciones:
        return None
    categoria = datos.get("categoria") if datos.get("categoria") in categorias else None
    r = reglas(cfg)
    minutos = duracion(r, categoria)
    revisadas = []
    for o in opciones[:4]:
        if not o.get("hora") and o.get("hora_hasta"):  # "antes de las 11": desde que abre la atención
            o["hora"] = r["desde"].strftime("%H:%M")
        if not o.get("hora"):
            revisadas.append({"descripcion": f"{o['fecha']} (sin hora)", "estado": "sin_hora"})
            continue
        try:
            rev = _revisar_opcion(cfg, o, minutos)
        except ValueError:
            continue
        revisadas.append(rev)
        if rev["estado"] == "libre":
            break
    libre = next((x for x in revisadas if x["estado"] == "libre"), None)
    if not libre:  # sin hora concreta o todo ocupado: alternativas a partir del día pedido
        base = revisadas[-1] if revisadas and revisadas[-1].get("alternativas") is not None else None
        if base is None:
            dia = dt.datetime.fromisoformat(opciones[0]["fecha"]).astimezone()
            bloques = ocupado(cfg, dia, dia + dt.timedelta(days=DIAS_ALTERNATIVAS * 2))
            alts = alternativas(r, max(dia.replace(hour=r["desde"].hour, minute=r["desde"].minute),
                                       dt.datetime.now().astimezone()), minutos, bloques)
        else:
            alts = base["alternativas"]
    return {"categoria": categoria, "minutos": minutos, "revisadas": revisadas,
            "estado": "libre" if libre else "no_disponible",
            "turno": libre, "alternativas": [] if libre else alts, "agendado": None}


def instruccion_para_ia(info):
    """Texto que se agrega al pedido de redacción según lo que dijo la agenda."""
    if info is None:
        return ""
    if info.get("error"):
        return ("AGENDA: no se pudo consultar la agenda. NO confirmes ningún turno; escribí "
                "[COMPLETAR: verificar disponibilidad en la agenda] donde corresponda.")
    ESTADOS = {"libre": "libre", "ocupado": "ocupado", "fuera_de_horario": "fuera del horario de atención",
               "pasado": "fecha ya pasada", "sin_hora": "no indicó hora",
               "dia_completo": "ese día ya no tiene lugar (siempre se reserva tiempo libre en la agenda)"}
    pedidos = "; ".join(
        (f"pidió {x['pedido']}: el {x['descripcion']}" if x.get("pedido") else x["descripcion"])
        + f" → {ESTADOS.get(x['estado'], x['estado'])}" for x in info["revisadas"])
    if info["estado"] == "libre":
        return (f"AGENDA (consultada en el calendario de la embajada): {pedidos}. "
                f"El turno del {info['turno']['descripcion']} ESTÁ LIBRE: confirmalo explícitamente con ese día y esa hora.")
    alts = "; ".join(a["descripcion"] for a in info["alternativas"]) or "(no se encontraron horarios libres en los próximos días)"
    return (f"AGENDA (consultada en el calendario de la embajada): {pedidos}. NO confirmes el horario pedido "
            f"porque no está disponible. Explicalo con amabilidad y ofrecé estas alternativas libres: {alts}. "
            "Pedile que responda con la que prefiera.")
