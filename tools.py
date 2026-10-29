"""Herramienta de línea de comandos para que Claude Code redacte los borradores.

  python tools.py pendientes            -> JSON con los mails pedidos a Claude, FAQ sugeridas, firma y tono
  python tools.py mail <id>             -> JSON con un mail completo
  python tools.py faq                   -> JSON con toda la base de preguntas frecuentes
  python tools.py guardar <id> <archivo.txt> [--faq 3,7]   -> guarda el borrador (autor: claude)
  python tools.py nueva-faq <archivo.json>                -> agrega FAQ desde {"pregunta","respuesta","categoria"} o una lista
  python tools.py importar <archivo.csv>                  -> importa un CSV de preguntas frecuentes
"""
import json
import sys

import core


def salida(datos):
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(datos, ensure_ascii=False, indent=2))


def main(args):
    if not args:
        print(__doc__)
        return 1
    con = core.conectar()
    cmd = args[0]

    if cmd == "pendientes":
        cfg = core.leer_config(con)
        ids = [r["id"] for r in con.execute("SELECT id FROM mails WHERE estado='pedido' ORDER BY fecha")]
        salida({
            "firma": cfg["firma"],
            "tono": cfg["tono"],
            "mails": [core.mail_completo(con, i) for i in ids],
        })
    elif cmd == "mail":
        salida(core.mail_completo(con, int(args[1])))
    elif cmd == "faq":
        salida([dict(r) for r in con.execute("SELECT id, pregunta, respuesta, categoria FROM faq ORDER BY id")])
    elif cmd == "guardar":
        mail_id, archivo = int(args[1]), args[2]
        faq_ids = []
        if "--faq" in args:
            faq_ids = [int(x) for x in args[args.index("--faq") + 1].split(",") if x.strip()]
        with open(archivo, encoding="utf-8") as f:
            texto = f.read().strip()
        if not con.execute("SELECT 1 FROM mails WHERE id=?", (mail_id,)).fetchone():
            print(f"No existe el mail {mail_id}")
            return 1
        core.guardar_borrador(con, mail_id, texto, faq_ids, autor="claude")
        print(f"Borrador guardado para el mail {mail_id}")
    elif cmd == "nueva-faq":
        with open(args[1], encoding="utf-8") as f:
            datos = json.load(f)
        for d in datos if isinstance(datos, list) else [datos]:
            con.execute(
                "INSERT INTO faq (pregunta, respuesta, categoria, creado, actualizado) VALUES (?, ?, ?, ?, ?)",
                (d["pregunta"].strip(), d["respuesta"].strip(), d.get("categoria", "").strip(), core.ahora(), core.ahora()),
            )
        con.commit()
        print("FAQ agregada(s)")
    elif cmd == "importar":
        with open(args[1], "rb") as f:
            salida(core.importar_csv(con, f.read()))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
