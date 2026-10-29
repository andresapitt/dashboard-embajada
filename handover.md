# Handover: cómo se arregló la conexión con la API de 1min.ai

> Contexto para otra IA o desarrollador. Problema resuelto el 26/10/2026 en un dashboard local hecho en Python
> (solo biblioteca estándar, `urllib`) que llama a la API de 1min.ai. Si otro proyecto tiene "la API key de 1min.ai no funciona",
> empezá por acá.

## Resumen en una línea

**La key no tenía nada de malo: el firewall de Cloudflare que protege `api.1min.ai` bloquea el User-Agent por defecto de Python
(`Python-urllib/3.x`) y responde `403`. Se arregla mandando un header `User-Agent` propio.**

## Síntomas

- Todas las llamadas a 1min.ai fallan con **HTTP 403 Forbidden**, tanto `GET /openai/v1/models` como `POST /openai/v1/chat/completions`.
- Parece un problema de API key inválida o sin permisos, pero la misma key funciona en otros lados.
- El cuerpo de la respuesta **no es JSON**, sino texto plano:
  ```
  error code: 1010
  ```
- El header de la respuesta dice `Server: cloudflare`.

Detalle que confundió el diagnóstico: la app tenía una lista de modelos "de respaldo" para cuando la API fallaba. Por eso el desplegable de modelos
parecía funcionar aunque en realidad nunca se había conectado. **Si tu app tiene un respaldo así, verificá que la lista venga de verdad de la API.**

## Causa

El **error 1010 de Cloudflare** significa "el dueño del sitio bloqueó el acceso según la firma de tu navegador". Cloudflare está bloqueando el
**User-Agent** del cliente HTTP. `urllib` de Python manda `User-Agent: Python-urllib/3.12` y eso queda bloqueado. **La key no tiene nada que ver**:
el pedido ni siquiera llega a 1min.ai.

## Cómo se diagnosticó

Se mandó el mismo pedido dos veces, con la misma key, cambiando solo el User-Agent:

```python
import json, urllib.request, urllib.error

KEY = "..."  # tu key de 1min.ai
URL = "https://api.1min.ai/openai/v1/chat/completions"
cuerpo = json.dumps({"model": "gpt-4.1-mini",
                     "messages": [{"role": "user", "content": "Decí hola"}],
                     "max_completion_tokens": 20}).encode()

for ua in (None, "MiApp/1.0"):
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    req = urllib.request.Request(URL, data=cuerpo, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(ua, "-> OK", r.read()[:100])
    except urllib.error.HTTPError as e:
        print(ua, "->", e.code, e.headers.get("Server"), e.read()[:200])
```

Resultado:
```
None -> 403 cloudflare b'error code: 1010\n'
MiApp/1.0 -> OK b'{"id":"chatcmpl-...","model":"gpt-4.1-mini","choices":[...'
```

## La solución

Agregá un header `User-Agent` propio, con cualquier nombre descriptivo, a **todos** los pedidos a `api.1min.ai`.

### Python con `urllib` (lo que se usó y se verificó)

```python
headers = {
    "Authorization": f"Bearer {clave}",
    "Content-Type": "application/json",
    # Cloudflare (error 1010) bloquea el User-Agent por defecto "Python-urllib"
    "User-Agent": "MiApp/1.0",
}
req = urllib.request.Request(url, data=datos, method=metodo, headers=headers)
```

### Otras librerías (no verificado: aplicá la misma idea)

Estas variantes **no se probaron** en este proyecto. Si tu proyecto usa alguna de estas librerías y recibe el 403 con `error code: 1010`,
fijá el User-Agent igual:

```python
# requests (su User-Agent por defecto es "python-requests/x.y")
requests.post(url, json=cuerpo, headers={"Authorization": f"Bearer {KEY}", "User-Agent": "MiApp/1.0"})

# SDK oficial de OpenAI apuntando a 1min.ai
from openai import OpenAI
client = OpenAI(api_key=KEY, base_url="https://api.1min.ai/openai/v1",
                default_headers={"User-Agent": "MiApp/1.0"}, max_retries=0)
```

```js
// Node / fetch
await fetch(url, { method: "POST", headers: {
  "Authorization": `Bearer ${KEY}`, "Content-Type": "application/json", "User-Agent": "MiApp/1.0" },
  body: JSON.stringify(cuerpo) });
```

## Mejora en el manejo de errores (recomendada)

Antes, la app traducía cualquier 401/403 como "la API key no es válida", y eso escondió la causa real. Ahora detecta el bloqueo de Cloudflare
leyendo el cuerpo del error, que no es JSON:

```python
except urllib.error.HTTPError as e:
    crudo = e.read().decode("utf-8", errors="replace")
    try:
        mensaje = json.loads(crudo).get("error", {}).get("message")
    except Exception:
        mensaje = crudo.strip()[:200] or e.reason
    if "error code: 10" in crudo:          # bloqueo de Cloudflare, no de la key
        mensaje = f"el firewall de 1min.ai bloqueó el pedido ({crudo.strip()})"
    elif e.code in (401, 403):
        mensaje = "la API key de 1min.ai no es válida o no tiene permiso"
    elif e.code == 429:
        mensaje = f"sin créditos o demasiados pedidos ({mensaje})"
    raise ErrorIA(f"1min.ai respondió {e.code}: {mensaje}")
```

También conviene **registrar el error completo** (en consola o en un log) y no mostrarlo solo en un aviso que desaparece de la interfaz.

## Datos útiles de la API de 1min.ai (verificados en este proyecto)

| Qué | Valor |
|---|---|
| Endpoint compatible con OpenAI | `https://api.1min.ai/openai/v1` |
| Autenticación | `Authorization: Bearer <KEY>` (las rutas viejas `/api/*` usan otro header, `API-KEY: <KEY>`) |
| Chat | `POST /openai/v1/chat/completions` |
| Lista de modelos | `GET /openai/v1/models`, que devuelve `{"data": [{"id": "gpt-4.1-mini", ...}, ...]}` (en esta cuenta, 49 modelos) |
| Límite de salida | usar `max_completion_tokens` (no mandar `max_tokens` al mismo tiempo) |
| Modo JSON | `"response_format": {"type": "json_object"}` funciona con `gpt-4.1-mini` |
| Modelo que anduvo bien | `gpt-4.1-mini`: rápido (1–3 s), barato y bueno en español e inglés |

Otros detalles de la documentación (https://docs.1min.ai/docs/api/openai-compatible):
- Hasta 100.000 caracteres y 200 mensajes por pedido. La respuesta tiene que generarse en menos de 2 minutos.
- Los reintentos automáticos están desactivados: usá `max_retries=0` en los SDK.
- Un error `429 insufficient_quota` indica que faltan créditos, o que conviene bajar `max_completion_tokens`, porque los créditos se reservan antes de generar.
- No es igual a OpenAI en todo: no tiene embeddings, imágenes, audio ni la Responses API.

## Lista de control para el otro proyecto

1. Reproducí el error y **leé el cuerpo de la respuesta**. Si dice `error code: 1010` y el header `Server` es `cloudflare`, es este problema.
2. Agregá `User-Agent: <NombreDeTuApp>/1.0` a todos los pedidos a `api.1min.ai`.
3. Probá `GET /openai/v1/models`: si devuelve la lista de modelos de tu cuenta, ya está.
4. Si la app es un servidor que queda corriendo, **reinicialo**. Si queda abierta la versión vieja, el error sigue apareciendo. En este proyecto eso hizo pensar
   que el arreglo no había funcionado.
5. Si después de esto el error es otro (`401`, `429`), ahí sí revisá la key o los créditos.
