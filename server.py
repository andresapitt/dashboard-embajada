"""Servidor local del dashboard. Solo escucha en 127.0.0.1 (no es accesible desde otras PCs).

Uso:  python server.py   y abrir http://127.0.0.1:8765
"""
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import aprendizaje
import core
import ia

PUERTO = int(os.environ.get("DASHBOARD_PUERTO") or 8765)
DEMO = os.environ.get("DASHBOARD_DEMO") == "1"
HOSTS_PERMITIDOS = {f"127.0.0.1:{PUERTO}", f"localhost:{PUERTO}"}
ESTATICOS = os.path.join(core.BASE, "static")


class ErrorApi(Exception):
    def __init__(self, mensaje, codigo=400):
        super().__init__(mensaje)
        self.codigo = codigo


RUTAS = []


def ruta(metodo, patron):
    def deco(fn):
        RUTAS.append((metodo, re.compile(f"^{patron}$"), fn))
        return fn
    return deco


# ---------------------------------------------------------------- estado / config

@ruta("GET", "/api/estado")
def estado(con, q, cuerpo):
    conteo = {e: 0 for e in core.ESTADOS}
    for r in con.execute("SELECT estado, COUNT(*) c FROM mails GROUP BY estado"):
        conteo[r["estado"]] = r["c"]
    faqs = con.execute("SELECT COUNT(*) FROM faq").fetchone()[0]
    return {"conteo": conteo, "faqs": faqs, "demo": DEMO}


@ruta("GET", "/api/config")
def ver_config(con, q, cuerpo):
    return core.leer_config(con)


@ruta("PUT", "/api/config")
def editar_config(con, q, cuerpo):
    core.guardar_config(con, cuerpo)
    return core.leer_config(con)


@ruta("POST", "/api/sincronizar")
def sincronizar(con, q, cuerpo):
    if DEMO:
        raise ErrorApi("En el modo de ejemplo no se conecta al correo. Abrí iniciar.bat para usar tus mails reales.")
    try:
        return core.sincronizar(con)
    except ValueError as e:
        raise ErrorApi(str(e))
    except Exception as e:  # errores de red, login, certificado...
        raise ErrorApi(f"No se pudo conectar al correo: {e}", 502)


# ---------------------------------------------------------------- IA (1min.ai)

@ruta("GET", "/api/ia/modelos")
def modelos_ia(con, q, cuerpo):
    return ia.listar_modelos(con, q.get("proveedor", [""])[0])


@ruta("POST", r"/api/mails/(\d+)/ia")
def redactar_ia(con, q, cuerpo, mail_id):
    if "instruccion" in cuerpo:
        con.execute("UPDATE mails SET instruccion=? WHERE id=?", (cuerpo["instruccion"], int(mail_id)))
        con.commit()
    try:
        return ia.redactar(con, int(mail_id), faq_forzada=cuerpo.get("faq_id"))
    except ia.ErrorIA as e:
        # Queda en la ventana negra para poder diagnosticarlo después
        print(f"[{core.ahora()}] Error de IA en el mail {mail_id}: {e}", flush=True)
        raise ErrorApi(str(e), 502)


# ---------------------------------------------------------------- aprendizaje

@ruta("GET", "/api/aprendizaje")
def ver_aprendizaje(con, q, cuerpo):
    return aprendizaje.estadisticas(con)


@ruta("PUT", "/api/aprendizaje")
def editar_aprendizaje(con, q, cuerpo):
    datos = {}
    if "preferencias" in cuerpo:
        datos["preferencias"] = "\n".join(l.strip() for l in cuerpo["preferencias"].splitlines() if l.strip())
    if "auto" in cuerpo:
        datos["aprendizaje_auto"] = "1" if cuerpo["auto"] else "0"
    core.guardar_config(con, datos)
    return aprendizaje.estadisticas(con)


@ruta("POST", "/api/aprendizaje/analizar")
def analizar_aprendizaje(con, q, cuerpo):
    try:
        aprendizaje.analizar(con)
    except ia.ErrorIA as e:
        print(f"[{core.ahora()}] Aprendizaje: {e}", flush=True)
        raise ErrorApi(str(e), 502)
    return aprendizaje.estadisticas(con)


@ruta("PATCH", r"/api/aprendizaje/ejemplos/(\d+)")
def editar_ejemplo(con, q, cuerpo, ejemplo_id):
    con.execute("UPDATE ejemplos SET usar=? WHERE id=?", (1 if cuerpo.get("usar") else 0, int(ejemplo_id)))
    con.commit()
    return {"ok": True}


@ruta("DELETE", r"/api/aprendizaje/ejemplos/(\d+)")
def borrar_ejemplo(con, q, cuerpo, ejemplo_id):
    con.execute("DELETE FROM ejemplos WHERE id=?", (int(ejemplo_id),))
    con.commit()
    return {"ok": True}


# ---------------------------------------------------------------- mails

@ruta("GET", "/api/mails")
def listar_mails(con, q, cuerpo):
    sql = """SELECT m.id, m.remitente, m.remitente_email, m.asunto, m.fecha, m.estado, m.origen,
                    substr(m.cuerpo, 1, 160) AS extracto,
                    (SELECT COUNT(*) FROM notas n WHERE n.mail_id = m.id) AS notas
             FROM mails m WHERE 1=1"""
    args = []
    est = q.get("estado", [""])[0]
    if est and est != "todos":
        sql += " AND m.estado = ?"
        args.append(est)
    elif not est:
        sql += " AND m.estado IN ('nuevo', 'pedido', 'borrador')"
    texto = q.get("q", [""])[0].strip()
    if texto:
        sql += " AND (m.asunto LIKE ? OR m.remitente LIKE ? OR m.remitente_email LIKE ? OR m.cuerpo LIKE ?)"
        args += [f"%{texto}%"] * 4
    sql += " ORDER BY m.fecha DESC LIMIT 500"
    return [dict(r) for r in con.execute(sql, args)]


@ruta("POST", "/api/mails")
def crear_mail(con, q, cuerpo):
    texto = (cuerpo.get("cuerpo") or "").strip()
    if not texto:
        raise ErrorApi("Pegá el texto del mail.")
    cur = con.execute(
        """INSERT INTO mails (remitente, remitente_email, asunto, fecha, cuerpo, origen, importado)
           VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
        (cuerpo.get("remitente", "").strip(), cuerpo.get("remitente_email", "").strip(),
         cuerpo.get("asunto", "").strip() or "(sin asunto)", core.ahora(), texto, core.ahora()),
    )
    con.commit()
    return core.mail_completo(con, cur.lastrowid)


@ruta("GET", r"/api/mails/(\d+)")
def ver_mail(con, q, cuerpo, mail_id):
    datos = core.mail_completo(con, int(mail_id))
    if not datos:
        raise ErrorApi("Mail no encontrado", 404)
    return datos


@ruta("PATCH", r"/api/mails/(\d+)")
def editar_mail(con, q, cuerpo, mail_id):
    mail_id = int(mail_id)
    if "estado" in cuerpo:
        if cuerpo["estado"] not in core.ESTADOS:
            raise ErrorApi("Estado inválido")
        anterior = con.execute("SELECT estado FROM mails WHERE id=?", (mail_id,)).fetchone()
        con.execute("UPDATE mails SET estado=? WHERE id=?", (cuerpo["estado"], mail_id))
        # Al marcar como respondido se cuenta el uso de las FAQ que se usaron en el borrador
        if cuerpo["estado"] == "respondido" and anterior and anterior["estado"] != "respondido":
            b = con.execute("SELECT faq_ids FROM borradores WHERE mail_id=?", (mail_id,)).fetchone()
            for fid in json.loads(b["faq_ids"]) if b else []:
                con.execute("UPDATE faq SET usos = usos + 1 WHERE id=?", (fid,))
    if "instruccion" in cuerpo:
        con.execute("UPDATE mails SET instruccion=? WHERE id=?", (cuerpo["instruccion"], mail_id))
    con.commit()
    if cuerpo.get("estado") == "respondido":
        # La respuesta enviada pasa a ser un ejemplo; con varias correcciones nuevas se reanalizan las preferencias
        aprendizaje.registrar(con, mail_id)
        aprendizaje.analizar_si_corresponde()
    return core.mail_completo(con, mail_id)


@ruta("DELETE", r"/api/mails/(\d+)")
def borrar_mail(con, q, cuerpo, mail_id):
    con.execute("DELETE FROM mails WHERE id=?", (int(mail_id),))
    con.commit()
    return {"ok": True}


@ruta("PUT", r"/api/mails/(\d+)/borrador")
def editar_borrador(con, q, cuerpo, mail_id):
    core.guardar_borrador(con, int(mail_id), cuerpo.get("texto", ""), cuerpo.get("faq_ids", []),
                          cuerpo.get("autor", "manual"))
    return core.mail_completo(con, int(mail_id))


@ruta("POST", r"/api/mails/(\d+)/notas")
def crear_nota(con, q, cuerpo, mail_id):
    texto = (cuerpo.get("texto") or "").strip()
    if not texto:
        raise ErrorApi("La nota está vacía.")
    con.execute("INSERT INTO notas (mail_id, texto, creado) VALUES (?, ?, ?)", (int(mail_id), texto, core.ahora()))
    con.commit()
    return core.mail_completo(con, int(mail_id))


@ruta("DELETE", r"/api/notas/(\d+)")
def borrar_nota(con, q, cuerpo, nota_id):
    con.execute("DELETE FROM notas WHERE id=?", (int(nota_id),))
    con.commit()
    return {"ok": True}


# ---------------------------------------------------------------- FAQ

@ruta("GET", "/api/faq")
def listar_faq(con, q, cuerpo):
    texto = q.get("q", [""])[0].strip()
    if texto:
        return core.sugerir_faq(con, texto, limite=100)
    return [dict(r) for r in con.execute("SELECT * FROM faq ORDER BY categoria, pregunta")]


def _validar_faq(cuerpo):
    preg, resp = (cuerpo.get("pregunta") or "").strip(), (cuerpo.get("respuesta") or "").strip()
    if not preg or not resp:
        raise ErrorApi("La pregunta y la respuesta son obligatorias.")
    return preg, resp, (cuerpo.get("categoria") or "").strip()


@ruta("POST", "/api/faq")
def crear_faq(con, q, cuerpo):
    preg, resp, cat = _validar_faq(cuerpo)
    cur = con.execute(
        "INSERT INTO faq (pregunta, respuesta, categoria, creado, actualizado) VALUES (?, ?, ?, ?, ?)",
        (preg, resp, cat, core.ahora(), core.ahora()),
    )
    con.commit()
    return dict(con.execute("SELECT * FROM faq WHERE id=?", (cur.lastrowid,)).fetchone())


@ruta("PUT", r"/api/faq/(\d+)")
def editar_faq(con, q, cuerpo, faq_id):
    preg, resp, cat = _validar_faq(cuerpo)
    con.execute("UPDATE faq SET pregunta=?, respuesta=?, categoria=?, actualizado=? WHERE id=?",
                (preg, resp, cat, core.ahora(), int(faq_id)))
    con.commit()
    return dict(con.execute("SELECT * FROM faq WHERE id=?", (int(faq_id),)).fetchone())


@ruta("DELETE", r"/api/faq/(\d+)")
def borrar_faq(con, q, cuerpo, faq_id):
    con.execute("DELETE FROM faq WHERE id=?", (int(faq_id),))
    con.commit()
    return {"ok": True}


@ruta("POST", "/api/faq/importar")
def importar_faq(con, q, cuerpo):
    return core.importar_csv(con, cuerpo)


# ---------------------------------------------------------------- servidor HTTP

class Manejador(BaseHTTPRequestHandler):
    server_version = "DashboardMails"

    def log_message(self, fmt, *args):
        pass  # silencioso

    def _responder(self, codigo, datos, tipo="application/json; charset=utf-8", extra=None):
        cuerpo = datos if isinstance(datos, bytes) else json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _permitido(self):
        # Evita que una página web externa use este servidor local (DNS rebinding / CSRF)
        if self.headers.get("Host") not in HOSTS_PERMITIDOS:
            return False
        origen = self.headers.get("Origin")
        return origen is None or origen.split("//", 1)[-1] in HOSTS_PERMITIDOS

    def _manejar(self, metodo):
        if not self._permitido():
            return self._responder(403, {"error": "Acceso no permitido"})
        url = urlparse(self.path)
        if metodo == "GET" and not url.path.startswith("/api/"):
            return self._estatico(url.path)
        if url.path == "/api/faq/exportar":
            con = core.conectar()
            try:
                csv_txt = core.exportar_csv(con)
            finally:
                con.close()
            return self._responder(200, csv_txt.encode("utf-8"), "text/csv; charset=utf-8",
                                   {"Content-Disposition": 'attachment; filename="preguntas_frecuentes.csv"'})

        largo = int(self.headers.get("Content-Length") or 0)
        crudo = self.rfile.read(largo) if largo else b""
        for m, patron, fn in RUTAS:
            coincide = patron.match(url.path)
            if m == metodo and coincide:
                try:
                    if url.path == "/api/faq/importar":
                        cuerpo = crudo
                    else:
                        cuerpo = json.loads(crudo or b"{}")
                    con = core.conectar()
                    try:
                        resultado = fn(con, parse_qs(url.query), cuerpo, *coincide.groups())
                    finally:
                        con.close()
                    return self._responder(200, resultado)
                except ErrorApi as e:
                    return self._responder(e.codigo, {"error": str(e)})
                except Exception as e:
                    return self._responder(500, {"error": f"Error interno: {e}"})
        self._responder(404, {"error": "Ruta no encontrada"})

    def _estatico(self, ruta_url):
        nombre = "index.html" if ruta_url in ("/", "") else ruta_url.lstrip("/")
        ruta_archivo = os.path.normpath(os.path.join(ESTATICOS, nombre))
        if not ruta_archivo.startswith(ESTATICOS) or not os.path.isfile(ruta_archivo):
            return self._responder(404, {"error": "No encontrado"})
        tipos = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
        with open(ruta_archivo, "rb") as f:
            self._responder(200, f.read(), tipos.get(os.path.splitext(nombre)[1], "application/octet-stream"))

    def do_GET(self):
        self._manejar("GET")

    def do_POST(self):
        self._manejar("POST")

    def do_PUT(self):
        self._manejar("PUT")

    def do_PATCH(self):
        self._manejar("PATCH")

    def do_DELETE(self):
        self._manejar("DELETE")


class Servidor(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja que varios procesos usen el mismo puerto sin error y el viejo sigue
    # atendiendo. Con SO_EXCLUSIVEADDRUSE el segundo falla y podemos pedirle al anterior que se cierre.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


@ruta("POST", "/api/apagar")
def apagar(con, q, cuerpo):
    # Lo usa una versión nueva que se está abriendo para reemplazar a esta
    print("Se abrió otra ventana del dashboard; esta se cierra. Podés cerrar esta ventana.")
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


def _cerrar_anterior():
    req = urllib.request.Request(f"http://127.0.0.1:{PUERTO}/api/apagar", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3)
        return True
    except urllib.error.HTTPError:
        return False  # versión vieja, sin /api/apagar
    except OSError:
        return True  # no había nada escuchando


def main():
    con = core.conectar()
    aprendizaje.rellenar(con)  # suma como ejemplos los mails que ya estaban respondidos
    con.close()
    servidor = None
    for intento in range(10):
        try:
            servidor = Servidor(("127.0.0.1", PUERTO), Manejador)
            break
        except OSError:
            if intento == 0 and not _cerrar_anterior():
                print(f"Ya hay una versión anterior del dashboard abierta en el puerto {PUERTO}.\n"
                      "Cerrá todas las ventanas negras del dashboard y volvé a abrir iniciar.bat.")
                return
            time.sleep(0.5)
    if servidor is None:
        print(f"No se pudo abrir el puerto {PUERTO}. Cerrá las otras ventanas del dashboard y probá de nuevo.")
        return
    url = f"http://127.0.0.1:{PUERTO}"
    print(f"Dashboard de mails corriendo en {url}  (cerrá esta ventana para apagarlo)")
    if "--sin-navegador" not in sys.argv:
        webbrowser.open(url)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
