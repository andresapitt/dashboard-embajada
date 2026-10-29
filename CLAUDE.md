# Dashboard de respuestas de mails (embajada)

Una app local hecha solo con la biblioteca estándar de Python (sin pip). Para iniciarla: `iniciar.bat`, que levanta `server.py` en http://127.0.0.1:8765.
Los datos quedan en `datos/dashboard.db` (SQLite). La contraseña del correo está cifrada con DPAPI. **Nunca** leas ni muestres la contraseña.

Lectura de mails: el servidor del ministerio bloquea IMAP (993/143) desde afuera, así que por defecto se usa la API REST de Zimbra
por HTTPS (`correo_metodo = "rest"`): `GET /home/~/inbox?fmt=json` y `GET /home/~/?id=<id>` (MIME crudo), con Basic auth. Son solo GET, en modo lectura.

Modo de ejemplo: `iniciar-demo.bat` regenera `datos/demo.db` con `demo.py` (datos ficticios) y abre http://127.0.0.1:8766.
Para trabajar sobre la demo con `tools.py`, fijá `DASHBOARD_DB=datos/demo.db`; si no lo fijás, usa la base real.

Python: `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (no está en el PATH de las sesiones viejas).

## IA (1min.ai o Gemini)

`ia.py` redacta los borradores con una API compatible con OpenAI. Hay dos proveedores en `PROVEEDORES` y `ia_proveedor` elige cuál se usa:
1min.ai (`https://api.1min.ai/openai/v1`, key `ia_key`, modelo `ia_modelo`) y Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`,
key `gemini_key`, modelo `gemini_modelo`; la lista de modelos sale de la API propia de Google). Hay que mandar un User-Agent propio,
porque Cloudflare bloquea "Python-urllib" (error 1010).
El modelo elige la plantilla de FAQ, la adapta y devuelve JSON (`faq_id`, `confianza`, `motivo`, `pendientes`, `respuesta`). El resultado se guarda en `borradores.analisis`.
Las API keys se cargan en Configuración y quedan cifradas con DPAPI (`ia_key` y `gemini_key`, igual que la contraseña). Nunca las leas ni las muestres.
La IA responde en el idioma del mail, porque algunas plantillas de visas están en inglés.

## Aprendizaje (`aprendizaje.py`, pestaña Aprendizaje)

- Al marcar un mail como respondido, `registrar()` guarda en la tabla `ejemplos` la versión final enviada, más el borrador original de la IA
  (`borradores.texto_ia`) y la `similitud` entre los dos (difflib).
- `ia.redactar` le pasa a la IA hasta 3 respuestas enviadas a consultas parecidas (`ejemplos_parecidos`), como guía de estilo, y las
  `preferencias` (config, una por línea).
- Con 3 correcciones nuevas o más, `analizar_si_corresponde()` corre `analizar()` en segundo plano: la IA compara los borradores con las versiones finales
  y reescribe las preferencias. El usuario puede editarlas, apagar el aprendizaje automático y excluir ejemplos.

## Redactar borradores con Claude Code ("redactá los borradores pendientes")

El usuario marca los mails como "Pedido a Claude" desde el dashboard. Para responderlos:

1. `python tools.py pendientes`: devuelve los mails pedidos, la `instruccion` de cada uno, las `sugerencias` de FAQ, la `firma` y el `tono`.
   Si hace falta, usá `python tools.py faq` para ver toda la base.
2. Para cada mail escribí la respuesta en español: usá la respuesta base de la FAQ que corresponda, pero **adaptala** al caso concreto
   (nombre del cliente, lo que pregunta exactamente y los datos que da; sacá lo que no aplica y respondé todas las preguntas del mail).
   Seguí el `tono` y la `instruccion`, y terminá con la `firma` si hay una. No inventes requisitos, costos, plazos ni links que no estén en la FAQ:
   si falta información, dejá un marcador visible como `[COMPLETAR: ...]`.
3. Guardá el texto en un archivo temporal (en el scratchpad, UTF-8) y corré `python tools.py guardar <id> <archivo> --faq <ids usados>`.
4. Si un mail plantea una consulta nueva que no está en la FAQ, avisale al usuario y ofrecé agregarla (`tools.py nueva-faq`).

El dashboard nunca envía mails: el usuario copia la respuesta y la pega en Zimbra.
