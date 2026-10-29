"""Aprendizaje a partir de las respuestas que el operador realmente envía.

1. Ejemplos: al marcar un mail como respondido se guarda la versión final (y el borrador original de la IA, si hubo).
   Al redactar un mail nuevo se le pasan a la IA las respuestas enviadas a las consultas más parecidas.
2. Preferencias: cada cierta cantidad de correcciones, la IA compara sus borradores con lo que el operador terminó
   enviando y actualiza una lista de reglas de estilo, que el operador puede leer y editar en la pestaña Aprendizaje.
"""
import difflib
import json
import math
import threading

import core

MIN_CORRECCIONES_PARA_ANALIZAR = 3  # correcciones nuevas que disparan el análisis automático
SIN_CAMBIOS = 0.98  # similitud a partir de la cual se considera que el borrador se envió tal cual
_analizando = threading.Lock()


def similitud(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "", autojunk=False).ratio()


def registrar(con, mail_id):
    """Guarda (o actualiza) el ejemplo de un mail recién marcado como respondido."""
    m = con.execute("SELECT * FROM mails WHERE id=?", (mail_id,)).fetchone()
    b = con.execute("SELECT * FROM borradores WHERE mail_id=?", (mail_id,)).fetchone()
    if not m or not b or not (b["texto"] or "").strip():
        return None
    faq_id = None
    if b["analisis"]:
        faq_id = json.loads(b["analisis"]).get("faq_id")
    sim = similitud(b["texto_ia"], b["texto"]) if b["texto_ia"] else None
    con.execute(
        """INSERT INTO ejemplos (mail_id, asunto, consulta, texto_ia, texto_final, faq_id, similitud, creado)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(mail_id) DO UPDATE SET texto_ia=excluded.texto_ia, texto_final=excluded.texto_final,
           faq_id=excluded.faq_id, similitud=excluded.similitud""",
        (mail_id, m["asunto"], ((m["asunto"] or "") + "\n" + (m["cuerpo"] or ""))[:6000],
         b["texto_ia"], b["texto"], faq_id, sim, core.ahora()),
    )
    con.commit()
    return sim


def rellenar(con):
    """Al iniciar: incorpora como ejemplos los mails que ya estaban respondidos antes de existir esta función."""
    for r in con.execute("""SELECT m.id FROM mails m JOIN borradores b ON b.mail_id = m.id
                            WHERE m.estado='respondido' AND m.id NOT IN (SELECT mail_id FROM ejemplos WHERE mail_id IS NOT NULL)""").fetchall():
        registrar(con, r["id"])


def ejemplos_parecidos(con, texto, limite=3, excluir_mail=None):
    filas = [dict(r) for r in con.execute("SELECT * FROM ejemplos WHERE usar=1 AND (mail_id IS NULL OR mail_id != ?)",
                                          (excluir_mail or -1,))]
    if not filas:
        return []
    docs = [set(core._tokens(f["consulta"])) for f in filas]
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    consulta = set(core._tokens(texto))
    n = len(filas)
    puntajes = []
    for f, d in zip(filas, docs):
        comunes = consulta & d
        if len(comunes) < 2:  # con una sola palabra en común el ejemplo no suele ser parecido
            continue
        puntajes.append((sum(math.log(1 + n / df[t]) for t in comunes) / math.sqrt(len(d) or 1), f))
    puntajes.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in puntajes[:limite]]


SISTEMA_ANALISIS = """Analizás cómo un operador de la sección consular de una embajada corrige los borradores que le escribe una IA antes de enviarlos.

Recibís la lista actual de PREFERENCIAS (reglas de estilo que la IA debe seguir) y pares de BORRADOR DE LA IA / VERSIÓN FINAL ENVIADA.
Compará cada par y deducí qué cambia el operador de forma consistente: saludo, despedida, tratamiento (usted/vos), largo, nivel de detalle, estructura, frases que siempre agrega o quita, información que prefiere omitir o incluir, formato de listas, etc.

Reglas:
- Devolvé la lista COMPLETA y actualizada de preferencias: conservá las actuales que sigan siendo válidas (el operador pudo haberlas escrito a mano), corregí o eliminá solo las que las correcciones contradigan claramente, y agregá las nuevas que se repitan.
- Cada preferencia es una instrucción breve, general y accionable, en español (ej. "Saludar con 'Estimado/a' seguido del nombre, sin 'Hola'").
- NUNCA incluyas datos de casos particulares (nombres, fechas, números de trámite, direcciones de personas).
- No agregues reglas por un solo cambio aislado, salvo que sea muy claro.
- Máximo 15 preferencias.

Devolvé SOLO un JSON: {"preferencias": ["..."], "resumen": "<una o dos oraciones en español: qué aprendiste o cambiaste respecto de la lista anterior>"}"""


def pendientes_de_analizar(con, cfg=None):
    cfg = cfg or core.leer_config(con)
    return con.execute(
        "SELECT COUNT(*) FROM ejemplos WHERE id > ? AND texto_ia IS NOT NULL AND similitud < ? AND usar=1",
        (int(cfg.get("aprendizaje_ultimo_ejemplo") or 0), SIN_CAMBIOS)).fetchone()[0]


def analizar(con):
    """Actualiza las preferencias a partir de las correcciones recientes. Devuelve el resultado."""
    import ia

    if not _analizando.acquire(blocking=False):
        raise ia.ErrorIA("Ya hay un análisis en curso.")
    try:
        cfg = core.leer_config(con)
        pares = con.execute(
            """SELECT * FROM ejemplos WHERE texto_ia IS NOT NULL AND similitud < ? AND usar=1
               ORDER BY id DESC LIMIT 12""", (SIN_CAMBIOS,)).fetchall()
        if not pares:
            raise ia.ErrorIA("Todavía no hay correcciones para analizar: redactá algunos mails con IA, "
                             "corregilos y marcalos como respondidos.")
        actuales = cfg.get("preferencias") or "(ninguna todavía)"
        usuario = f"PREFERENCIAS ACTUALES:\n{actuales}\n\n" + "\n\n".join(
            f"=== Par {i}\nBORRADOR DE LA IA:\n{p['texto_ia'][:2500]}\n\nVERSIÓN FINAL ENVIADA:\n{p['texto_final'][:2500]}"
            for i, p in enumerate(pares, 1))
        datos, modelo = ia.completar_json(con, SISTEMA_ANALISIS, usuario, max_tokens=4000)
        prefs = [str(p).strip(" -•\t") for p in datos.get("preferencias") or [] if str(p).strip(" -•\t")][:15]
        if not prefs:
            raise ia.ErrorIA("La IA no devolvió preferencias. Probá de nuevo.")
        ultimo = con.execute("SELECT COALESCE(MAX(id), 0) FROM ejemplos").fetchone()[0]
        core.guardar_config(con, {
            "preferencias": "\n".join(prefs),
            "aprendizaje_ultimo_ejemplo": str(ultimo),
            "aprendizaje_resumen": str(datos.get("resumen") or "").strip(),
            "aprendizaje_fecha": core.ahora(),
        })
        return {"preferencias": prefs, "resumen": datos.get("resumen"), "modelo": modelo, "pares": len(pares)}
    finally:
        _analizando.release()


def analizar_si_corresponde():
    """Se llama al marcar un mail como respondido: si hay suficientes correcciones nuevas, analiza en segundo plano."""
    con = core.conectar()
    try:
        cfg = core.leer_config(con)
        if cfg.get("aprendizaje_auto") != "1" or pendientes_de_analizar(con, cfg) < MIN_CORRECCIONES_PARA_ANALIZAR:
            return
    finally:
        con.close()

    def tarea():
        c = core.conectar()
        try:
            r = analizar(c)
            print(f"[{core.ahora()}] Aprendizaje: preferencias actualizadas con {r['pares']} correcciones.", flush=True)
        except Exception as e:
            print(f"[{core.ahora()}] Aprendizaje: no se pudo analizar ({e})", flush=True)
        finally:
            c.close()

    threading.Thread(target=tarea, daemon=True).start()


def estadisticas(con):
    filas = [dict(r) for r in con.execute(
        """SELECT e.id, e.mail_id, e.asunto, e.faq_id, e.similitud, e.usar, e.creado, e.texto_final, e.texto_ia,
                  f.pregunta AS faq_pregunta
           FROM ejemplos e LEFT JOIN faq f ON f.id = e.faq_id ORDER BY e.id DESC""")]
    con_ia = [f for f in filas if f["similitud"] is not None]
    recientes = con_ia[:10]
    por_faq = {}
    for f in con_ia:
        if f["faq_id"]:
            d = por_faq.setdefault(f["faq_id"], {"faq_id": f["faq_id"], "pregunta": f["faq_pregunta"], "usos": 0, "suma": 0.0})
            d["usos"] += 1
            d["suma"] += f["similitud"]
    plantillas = sorted(({"faq_id": d["faq_id"], "pregunta": d["pregunta"], "usos": d["usos"],
                          "cambio": round(1 - d["suma"] / d["usos"], 3)} for d in por_faq.values()),
                        key=lambda d: d["cambio"], reverse=True)
    cfg = core.leer_config(con)
    return {
        "total": len(filas),
        "con_ia": len(con_ia),
        "sin_cambios": sum(f["similitud"] >= SIN_CAMBIOS for f in con_ia),
        "cambio_reciente": round(1 - sum(f["similitud"] for f in recientes) / len(recientes), 3) if recientes else None,
        "pendientes": pendientes_de_analizar(con, cfg),
        "minimo": MIN_CORRECCIONES_PARA_ANALIZAR,
        "preferencias": cfg.get("preferencias") or "",
        "auto": cfg.get("aprendizaje_auto") == "1",
        "resumen": cfg.get("aprendizaje_resumen") or "",
        "fecha": cfg.get("aprendizaje_fecha") or "",
        "plantillas": plantillas[:10],
        "ejemplos": filas[:200],
    }
