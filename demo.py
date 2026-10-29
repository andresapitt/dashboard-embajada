"""Crea una base de ejemplo (datos/demo.db) con preguntas y mails ficticios, para probar el dashboard
sin tocar los datos reales. Se usa desde iniciar-demo.bat.
"""
import os

os.environ.setdefault("DASHBOARD_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "datos", "demo.db"))

import core  # noqa: E402  (tiene que importarse después de fijar DASHBOARD_DB)

FAQS = [
    ("¿Cómo renuevo mi pasaporte?", "Pasaportes",
     "Para renovar el pasaporte debe solicitar un turno a través del sistema de turnos online del consulado. "
     "El día del turno debe presentarse personalmente con:\n- DNI argentino vigente\n- Pasaporte anterior (si lo tuviera)\n"
     "- Comprobante de domicilio en la jurisdicción\n\nEl arancel se abona el mismo día. El pasaporte se envía al consulado "
     "y se le avisará por correo cuando esté listo para retirar."),
    ("¿Cómo saco o renuevo el DNI desde el exterior?", "DNI",
     "El trámite de DNI se realiza con turno previo y es presencial, ya que se toman huellas y foto. "
     "Debe presentar su DNI anterior o partida de nacimiento, y comprobante de domicilio. "
     "Si el domicilio cambia al exterior, el nuevo DNI indicará la dirección en este país."),
    ("¿Cómo legalizo un documento argentino?", "Legalizaciones",
     "Los documentos públicos argentinos deben estar apostillados en Argentina para tener validez en este país. "
     "El consulado no apostilla documentos emitidos en Argentina. Puede tramitar la apostilla online en el sitio de la Cancillería."),
    ("¿Cómo inscribo el nacimiento de mi hijo nacido en el exterior?", "Registro civil",
     "Para inscribir el nacimiento debe solicitar un turno. Deben presentarse ambos padres con:\n"
     "- Partida de nacimiento local legalizada/apostillada\n- DNI o pasaporte de los padres\n- El niño o niña\n\n"
     "Luego de la inscripción podrá tramitar su DNI y pasaporte argentino."),
    ("¿Cómo pido un turno?", "Turnos",
     "Los turnos se solicitan únicamente a través del sistema de turnos online del consulado. "
     "Se habilitan nuevos turnos todos los lunes. No se otorgan turnos por teléfono ni por correo."),
    ("¿Cómo me inscribo para votar en el exterior?", "Electoral",
     "Para votar desde el exterior debe tener domicilio actualizado en este país en su DNI. "
     "El padrón de electores en el exterior cierra 180 días antes de cada elección."),
    ("¿Puedo hacer un poder desde el exterior?", "Poderes",
     "Sí, el consulado otorga poderes. Debe solicitar un turno y enviar previamente por correo el texto del poder "
     "o los datos del apoderado y el motivo, para que podamos preparar el documento."),
]

MAILS = [
    # (remitente, email, asunto, cuerpo, fecha, estado, instruccion)
    ("Lucía Fernández", "lucia.fernandez@ejemplo.com", "Pasaporte vencido, viajo en diciembre",
     "Buenas tardes,\n\nMi pasaporte argentino venció en agosto y viajo a Buenos Aires el 15 de diciembre. "
     "¿Llego a renovarlo antes? ¿Qué tengo que llevar? Vivo en el país hace 3 años.\n\nGracias,\nLucía",
     "2026-10-23T09:12", "borrador", ""),
    ("Martín Gómez", "mgomez@ejemplo.com", "Consulta poder y DNI",
     "Hola, necesito hacer un poder para que mi hermano venda un auto en Córdoba. Además mi DNI está vencido, "
     "¿puedo hacer las dos cosas el mismo día?\n\nSaludos",
     "2026-10-23T08:40", "pedido", "Aclarar que son dos turnos distintos"),
    ("Sofía Ruiz", "sofia.ruiz@ejemplo.com", "Inscripción de nacimiento",
     "Estimados, mi hija nació el mes pasado acá. Quisiera inscribirla como argentina. Mi pareja no es argentino, ¿tiene que venir igual?",
     "2026-10-22T17:05", "nuevo", ""),
    ("Diego Paz", "diego.paz@ejemplo.com", "Turnos",
     "Hola, entro a la página de turnos y no hay disponibles. ¿Me pueden dar uno por mail? Es urgente.",
     "2026-10-22T11:30", "respondido", ""),
    ("Ana Torres", "ana.torres@ejemplo.com", "Apostilla título universitario",
     "Buen día, me pidieron apostillar mi título de la UBA para trabajar acá. ¿Lo hacen en el consulado?",
     "2026-10-21T15:48", "nuevo", ""),
]

BORRADORES = {
    "Pasaporte vencido, viajo en diciembre": (
        "Estimada Lucía:\n\nGracias por su consulta. Para renovar su pasaporte debe solicitar un turno a través del sistema "
        "de turnos online del consulado. El día del turno deberá presentarse personalmente con:\n"
        "- DNI argentino vigente\n- Su pasaporte vencido\n- Comprobante de domicilio en esta jurisdicción\n\n"
        "El arancel se abona el mismo día. Considerando que viaja el 15 de diciembre, le recomendamos pedir el turno cuanto antes, "
        "ya que el pasaporte se envía al consulado y le avisaremos por correo cuando esté listo para retirar. "
        "[COMPLETAR: plazo estimado de entrega]\n\nLe recordamos que, como ciudadana argentina, puede ingresar a Argentina con su DNI vigente.\n\n"
        "Saludos cordiales,\nSección Consular\nEmbajada (ejemplo)",
        "claude", [1]),
    "Turnos": (
        "Estimado Diego:\n\nLos turnos se solicitan únicamente a través del sistema de turnos online; no podemos otorgarlos por correo. "
        "Se habilitan nuevos turnos todos los lunes, por lo que le sugerimos ingresar ese día.\n\n"
        "Si su caso es una urgencia (por ejemplo, un viaje inminente por motivos de salud), escríbanos indicando el motivo y la fecha del viaje.\n\n"
        "Saludos cordiales,\nSección Consular\nEmbajada (ejemplo)",
        "manual", [5]),
}

NOTAS = {
    "Turnos": ["Le respondí el 22/10. Si insiste con la urgencia, consultar con el cónsul."],
    "Pasaporte vencido, viajo en diciembre": ["Ojo: viaja el 15/12, revisar si llega."],
}


def crear():
    if os.path.exists(core.DB_PATH):
        os.remove(core.DB_PATH)
    con = core.conectar()
    for preg, cat, resp in FAQS:
        con.execute("INSERT INTO faq (pregunta, respuesta, categoria, creado, actualizado) VALUES (?, ?, ?, ?, ?)",
                    (preg, resp, cat, core.ahora(), core.ahora()))
    con.execute("UPDATE faq SET usos = 3 WHERE id IN (1, 5)")
    for rem, mail, asunto, cuerpo, fecha, estado, instr in MAILS:
        cur = con.execute(
            """INSERT INTO mails (remitente, remitente_email, asunto, fecha, cuerpo, estado, instruccion, origen, importado)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'demo', ?)""",
            (rem, mail, asunto, fecha, cuerpo, estado, instr, core.ahora()))
        if asunto in BORRADORES:
            texto, autor, ids = BORRADORES[asunto]
            core.guardar_borrador(con, cur.lastrowid, texto, ids, autor)
            con.execute("UPDATE mails SET estado=? WHERE id=?", (estado, cur.lastrowid))
        for n in NOTAS.get(asunto, []):
            con.execute("INSERT INTO notas (mail_id, texto, creado) VALUES (?, ?, ?)", (cur.lastrowid, n, core.ahora()))
    core.guardar_config(con, {"firma": "Saludos cordiales,\nSección Consular\nEmbajada (ejemplo)"})
    con.commit()
    con.close()
    print(f"Base de ejemplo creada en {core.DB_PATH}")


if __name__ == "__main__":
    crear()
