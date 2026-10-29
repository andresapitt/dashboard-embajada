"""Redacción de borradores con IA, a través de 1min.ai o de Google Gemini.

Los dos proveedores ofrecen una API compatible con la de OpenAI (/chat/completions), así que se usa el mismo código.
Para cada mail, el modelo elige la plantilla de la base de preguntas frecuentes que mejor responde la consulta
y la adapta al mensaje: nombre del remitente, fechas, documentos y demás detalles. Lo que la base no cubre lo deja
marcado como [COMPLETAR: ...] en vez de inventarlo.
"""
import json
import re
import urllib.error
import urllib.request

import core

PROVEEDORES = {
    "1min": {
        "nombre": "1min.ai",
        "api": "https://api.1min.ai/openai/v1",
        "clave": "ia_key",
        "modelo": "ia_modelo",
        "recomendado": "gpt-4.1-mini",
        # Se usan si todavía no hay API key o la lista de modelos no se puede descargar.
        "respaldo": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o", "gpt-5-mini", "gpt-5",
                     "mistral-large-latest", "qwen-plus", "deepseek-flash"],
    },
    "gemini": {
        "nombre": "Gemini",
        "api": "https://generativelanguage.googleapis.com/v1beta/openai",
        "clave": "gemini_key",
        "modelo": "gemini_modelo",
        "recomendado": "gemini-2.5-flash",
        "respaldo": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    },
}
GEMINI_MODELOS = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
MAX_CARACTERES_PLANTILLAS = 50000  # el límite de 1min.ai es 100.000 caracteres por pedido


class ErrorIA(Exception):
    pass


def proveedor_activo(cfg):
    return cfg.get("ia_proveedor") if cfg.get("ia_proveedor") in PROVEEDORES else "1min"


def _pedido(prov, metodo, url, clave, cuerpo=None, timeout=130):
    nombre = PROVEEDORES[prov]["nombre"]
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    headers = {
        "Authorization": f"Bearer {clave}",
        "Content-Type": "application/json",
        # Cloudflare (error 1010) bloquea el User-Agent por defecto "Python-urllib"
        "User-Agent": "DashboardMails/1.0",
    }
    if prov == "gemini":
        headers["x-goog-api-key"] = clave  # lo pide la API propia de Google (lista de modelos)
    req = urllib.request.Request(url, data=datos, method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        crudo = e.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(crudo)
            detalle = (detalle[0] if isinstance(detalle, list) else detalle).get("error", {})
            mensaje = detalle.get("message") or str(detalle)
        except Exception:
            mensaje = crudo.strip()[:200] or e.reason
        if "error code: 10" in crudo:  # bloqueo del firewall de Cloudflare, no de la key
            mensaje = f"el firewall de {nombre} bloqueó el pedido ({crudo.strip()})"
        elif e.code in (401, 403) or "API key not valid" in crudo:
            mensaje = f"la API key de {nombre} no es válida o no tiene permiso"
        elif e.code == 429:
            mensaje = f"sin créditos o demasiados pedidos ({mensaje})"
        raise ErrorIA(f"{nombre} respondió {e.code}: {mensaje}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ErrorIA(f"No se pudo conectar con {nombre}: {getattr(e, 'reason', e)}") from None


def _clave(con, prov):
    clave = core.leer_config(con, incluir_secretos=True).get(PROVEEDORES[prov]["clave"])
    if not clave:
        raise ErrorIA(f"Falta la API key de {PROVEEDORES[prov]['nombre']} (pestaña Configuración).")
    return clave


def _modelos_gemini(clave):
    datos = _pedido("gemini", "GET", GEMINI_MODELOS, clave, timeout=20)
    ids = set()
    for m in datos.get("models", []):
        nombre = m.get("name", "").removeprefix("models/")
        # Solo modelos de texto que generan contenido (sin embeddings, imágenes, audio, etc.)
        if ("generateContent" in m.get("supportedGenerationMethods", []) and nombre.startswith("gemini")
                and not re.search(r"embedding|image|tts|audio|live|vision", nombre)):
            ids.add(nombre)
    return ids


def listar_modelos(con, prov=None):
    prov = prov if prov in PROVEEDORES else proveedor_activo(core.leer_config(con))
    p = PROVEEDORES[prov]
    base = {"proveedor": prov, "recomendado": p["recomendado"], "modelos": p["respaldo"], "origen": "respaldo"}
    try:
        clave = _clave(con, prov)
        if prov == "gemini":
            ids = _modelos_gemini(clave)
        else:
            datos = _pedido(prov, "GET", p["api"] + "/models", clave, timeout=20)
            lista = datos if isinstance(datos, list) else (datos.get("data") or datos.get("models") or [])
            ids = {m if isinstance(m, str) else (m.get("id") or m.get("model") or m.get("name") or "")
                   for m in lista} - {""}
    except ErrorIA as e:
        return base | {"error": str(e)}
    if not ids:
        return base
    recomendado = p["recomendado"]
    if recomendado not in ids:  # p. ej. si Google retiró ese modelo: el "flash" estable más nuevo
        flash = sorted(i for i in ids if re.fullmatch(r"gemini-[\d.]+-flash", i))
        recomendado = flash[-1] if flash else sorted(ids)[0]
    return base | {"modelos": sorted(ids), "origen": p["nombre"], "recomendado": recomendado}


SISTEMA = """Sos un asistente de la sección consular de una representación argentina en el exterior. Redactás respuestas a los correos de ciudadanos y usuarios usando una base de respuestas aprobadas (plantillas).

Tu tarea para cada correo:
1. Analizá el mensaje e identificá TODAS las consultas que hace la persona.
2. Elegí la plantilla que mejor responde la consulta principal. Si hay consultas secundarias cubiertas por otras plantillas, usalas también. Si ninguna plantilla aplica, usá faq_id null.
3. Redactá la respuesta ADAPTANDO la plantilla al caso concreto:
   - Saludá a la persona por su nombre si se puede deducir del mensaje o del remitente (ej. "Estimada Lucía:"). Si no, usá un saludo neutro.
   - Usá los detalles del mensaje original (fechas, lugares, documentos, situación familiar, urgencia) para que la respuesta sea específica y no genérica.
   - Quitá de la plantilla lo que no aplique al caso y reordená si hace falta. Respondé todas las preguntas.
   - NO inventes requisitos, costos, plazos, horarios, direcciones, teléfonos ni links que no estén en las plantillas. Si una pregunta no está cubierta, escribí un marcador visible [COMPLETAR: qué falta] en el lugar correspondiente.
4. IDIOMA: respondé en el mismo idioma en que está escrito el correo (por ejemplo, si escriben en inglés, respondé en inglés). Si la plantilla elegida está en otro idioma, traducila con cuidado, sin cambiar requisitos, montos, plazos ni links. Si no se puede determinar el idioma, usá español.
5. Usá el tono indicado y terminá con la firma si se te da una, tal cual (no la traduzcas ni agregues otra despedida duplicada).
6. Si se te dan PREFERENCIAS APRENDIDAS del operador, seguilas siempre: son la forma en que él corrige los borradores. Si se te dan EJEMPLOS de respuestas que ya envió, imitá su estilo, estructura y nivel de detalle, pero NUNCA copies de ellos nombres, fechas, números ni datos personales; los datos oficiales salen siempre de las plantillas.

Devolvé SOLO un objeto JSON con esta forma (los campos "motivo", "consultas" y "pendientes" siempre en español, porque los lee el operador):
{"faq_id": <id de la plantilla principal o null>, "faq_ids_secundarias": [<ids>], "idioma": "<idioma de la respuesta, ej. español o inglés>", "confianza": "alta" | "media" | "baja", "motivo": "<una oración: por qué elegiste esa plantilla>", "consultas": ["<cada consulta detectada>"], "pendientes": ["<lo que quedó como [COMPLETAR]>"], "respuesta": "<texto completo del correo de respuesta>"}"""


def _plantillas(con, mail, faq_forzada=None):
    todas = [dict(r) for r in con.execute("SELECT id, pregunta, respuesta, categoria FROM faq ORDER BY id")]
    if sum(len(f["pregunta"]) + len(f["respuesta"]) for f in todas) > MAX_CARACTERES_PLANTILLAS:
        # Base grande: se mandan solo las más parecidas para no pasarse del límite de la API
        ids = [f["id"] for f in core.sugerir_faq(con, (mail["asunto"] or "") + "\n" + (mail["cuerpo"] or ""), limite=15)]
        if faq_forzada and faq_forzada not in ids:
            ids.append(faq_forzada)
        todas = [f for f in todas if f["id"] in ids]
    return todas


def _extraer_json(texto):
    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def completar_json(con, sistema, usuario, max_tokens=8000):
    """Hace un pedido al proveedor elegido y devuelve (datos JSON, "Proveedor · modelo")."""
    cfg = core.leer_config(con)
    prov = proveedor_activo(cfg)
    p = PROVEEDORES[prov]
    clave = _clave(con, prov)
    modelo = cfg.get(p["modelo"]) or p["recomendado"]
    cuerpo = {
        "model": modelo,
        "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
        # incluye el razonamiento en los modelos que razonan; Gemini usa el nombre clásico del parámetro
        ("max_tokens" if prov == "gemini" else "max_completion_tokens"): max_tokens,
        "response_format": {"type": "json_object"},
    }
    url = p["api"] + "/chat/completions"
    try:
        resp = _pedido(prov, "POST", url, clave, cuerpo)
    except ErrorIA as e:
        if "400" not in str(e):
            raise
        cuerpo.pop("response_format")  # algunos modelos no aceptan el modo JSON
        resp = _pedido(prov, "POST", url, clave, cuerpo)
    try:
        return _extraer_json(resp["choices"][0]["message"]["content"] or ""), f"{p['nombre']} · {modelo}"
    except (KeyError, IndexError, TypeError, ValueError):
        raise ErrorIA("La IA devolvió una respuesta con un formato inesperado. Probá de nuevo o con otro modelo.")


def redactar(con, mail_id, instruccion=None, faq_forzada=None):
    """Genera y guarda el borrador de un mail. Devuelve el mail completo con el análisis."""
    import aprendizaje  # importación local: aprendizaje usa este módulo

    mail = con.execute("SELECT * FROM mails WHERE id=?", (mail_id,)).fetchone()
    if not mail:
        raise ErrorIA("Mail no encontrado")
    cfg = core.leer_config(con)
    _clave(con, proveedor_activo(cfg))  # falla antes de armar el pedido si no hay key
    plantillas = _plantillas(con, mail, faq_forzada)
    if not plantillas:
        raise ErrorIA("La base de preguntas frecuentes está vacía: importá tu CSV primero.")
    if instruccion is None:
        instruccion = mail["instruccion"]

    usuario = [
        f"TONO: {cfg['tono'] or 'Cordial y formal.'}",
        f"FIRMA (ponela al final, tal cual):\n{cfg['firma']}" if cfg["firma"].strip() else "FIRMA: (no hay; no firmes con un nombre)",
    ]
    if instruccion.strip():
        usuario.append(f"INDICACIÓN DEL OPERADOR PARA ESTE CASO: {instruccion.strip()}")
    if faq_forzada:
        usuario.append(f"El operador eligió la plantilla id {faq_forzada}: usala como principal.")
    preferencias = [l.strip(" -•\t") for l in (cfg.get("preferencias") or "").splitlines() if l.strip(" -•\t")]
    if preferencias:
        usuario.append("PREFERENCIAS APRENDIDAS DEL OPERADOR (seguilas siempre):\n" + "\n".join(f"- {l}" for l in preferencias))
    consulta = (mail["asunto"] or "") + "\n" + (mail["cuerpo"] or "")
    ejemplos = aprendizaje.ejemplos_parecidos(con, consulta, excluir_mail=mail_id)
    if ejemplos:
        usuario.append(
            "EJEMPLOS DE RESPUESTAS QUE EL OPERADOR YA ENVIÓ A CONSULTAS PARECIDAS (solo como guía de estilo; "
            "no copies sus nombres ni datos personales):\n\n" + "\n\n".join(
                f"--- Ejemplo {i}\nCONSULTA: {e['consulta'][:800]}\nRESPUESTA ENVIADA:\n{e['texto_final'][:1800]}"
                for i, e in enumerate(ejemplos, 1)))
    usuario.append("PLANTILLAS DISPONIBLES:\n" + "\n\n".join(
        f"[id {f['id']}] ({f['categoria'] or 'sin categoría'}) {f['pregunta']}\n{f['respuesta']}" for f in plantillas))
    usuario.append(
        "CORREO A RESPONDER:\n"
        f"De: {mail['remitente'] or ''} <{mail['remitente_email'] or ''}>\n"
        f"Asunto: {mail['asunto'] or ''}\n\n{(mail['cuerpo'] or '')[:12000]}"
    )

    datos, modelo = completar_json(con, SISTEMA, "\n\n".join(usuario))
    try:
        texto = str(datos["respuesta"]).strip()
    except (KeyError, TypeError):
        raise ErrorIA("La IA devolvió una respuesta con un formato inesperado. Probá de nuevo o con otro modelo.")

    validos = {f["id"] for f in plantillas}
    principal = datos.get("faq_id") if datos.get("faq_id") in validos else None
    ids = ([principal] if principal else []) + [i for i in datos.get("faq_ids_secundarias") or [] if i in validos and i != principal]
    elegida = next((f for f in plantillas if f["id"] == principal), None)
    analisis = {
        "modelo": modelo,
        "ejemplos_usados": len(ejemplos),
        "preferencias_usadas": len(preferencias),
        "faq_id": principal,
        "faq_pregunta": elegida["pregunta"] if elegida else None,
        "faq_ids": ids,
        "idioma": datos.get("idioma"),
        "confianza": datos.get("confianza"),
        "motivo": datos.get("motivo"),
        "consultas": datos.get("consultas") or [],
        "pendientes": datos.get("pendientes") or [],
    }
    core.guardar_borrador(con, mail_id, texto, ids, autor="ia", analisis=analisis)
    if mail["estado"] == "pedido":
        con.execute("UPDATE mails SET estado='borrador' WHERE id=?", (mail_id,))
        con.commit()
    return core.mail_completo(con, mail_id)
