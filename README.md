# Dashboard de ChatGPT Enterprise

Panel web interno con los puestos, la adopción y el consumo del workspace de
ChatGPT Enterprise. Mismo planteamiento que `dashboard-sensores`: Python con
librería estándar (`proxy.py`), un `index.html`, datos en ficheros y servicio systemd.

## Pestañas

| Pestaña | Qué muestra |
|---|---|
| Resumen | Cifras clave con comparación frente al periodo anterior, adopción, avisos y actividad diaria |
| Empresas | Puestos, adopción, mensajes y coste por sociedad, agrupando por el dominio del correo |
| Personas | Tabla filtrable y ordenable; al pulsar una fila, ficha con su actividad, modelos y aplicaciones |
| Modelos | Tokens por modelo y velocidad, evolución y quién usa cada modelo |
| Aplicaciones | Mensajes y personas por aplicación (web, escritorio, móvil) |
| Consumo y coste | Tokens de entrada, caché y salida; coste de los puestos; margen para consumo y compromiso del contrato; mayores consumidores |
| Facturación | Reparto de puestos y consumo por empresa en cada periodo de facturación, para refacturar y conciliar con la factura |
| Estimador | Coste de una carga de trabajo (modelo, tipo de consulta, personas) con los precios observados en el uso real |
| Tendencias | Activos y mensajes por semana, mapa por día de la semana, evolución de puestos, activos por mes |
| Licencias | Candidatos a reasignar, owners y admins, SCIM, altas y bajas, cuentas con uso que ya no son miembros |
| Límites | Límite mensual de cada persona, quién lo tiene subido, desde y hasta cuándo, y cuánto lleva gastado |

Filtros comunes: periodo (7, 30, 90 días, todo o fechas a mano) y empresa. Todas las
tablas se exportan a CSV. Tema claro u oscuro.

## De dónde salen los datos

Admin API del workspace (`api.chatgpt.com`) con una Admin key de solo lectura:

    GET /v1/manage/workspaces/{id}/users
    GET /v1/analytics/workspaces/{id}/usage
    GET /v1/manage/workspaces/{id}/usage_limits/workspace
    GET /v1/manage/workspaces/{id}/usage_limits/users/{user_id}
    GET /v1/manage/workspaces/{id}/usage_limits/users/{user_id}/monthly-usage

La API devuelve unos 30 días de uso. El servidor guarda cada día en
`data/uso_diario.json`, así que el histórico crece con el tiempo (dos años por defecto).
Los días vienen en UTC y el último puede estar incompleto. `credits` y `cost_usd`
pueden llegar vacíos; el panel solo muestra créditos cuando la API los informa.

| Fichero en `data/` | Contenido |
|---|---|
| `uso_diario.json` | Uso diario por persona, modelo y aplicación |
| `miembros.json` | Última lista de miembros, para detectar altas y bajas |
| `eventos.csv` | Altas y bajas detectadas |
| `historico.csv` | Foto diaria: puestos, activos y sin uso |
| `limites.json` | Límites propios vistos en la última lectura y cambios detectados |

### Admin key del workspace

Admin Console > workspace > **Credentials > Admin keys > Create new admin key**.
Permisos **Custom**, en **Read**: Enterprise analytics API, Users y Usage limits (este último solo lo usa la pestaña Límites). Hace falta rol
Owner o Admin del workspace. El UUID del workspace va en `CHATGPT_WORKSPACE_ID`.

## Configuración (`config.json`)

- `puerto`, `escucha`: dónde escucha el servidor (por defecto `0.0.0.0:8091`).
- `refresco_minutos`: cada cuánto consulta la API (mínimo 5).
- `precio_puesto`: precio mensual por tipo de puesto (`chatgpt`, `codex`...). Con 0 no se muestran costes.
- `moneda`: moneda en la que se muestra todo el panel (`EUR`, `USD`...).
- `compromiso_anual`: gasto mínimo comprometido en el contrato (puestos + consumo), en esa moneda.
  El margen para consumo se calcula restando el coste anual de los puestos. Con 0 no se muestra.
- `contrato_inicio`: día en que empieza el contrato (`AAAA-MM-DD`). Marca también el inicio de
  cada periodo de facturación mensual. Vacío: primer día con datos.
- `bolsa_anual`: alternativa si no hay compromiso: bolsa anual solo para consumo.
- `usd_a_moneda`: cambio para pasar a la moneda del panel los importes que la API da en USD.
- `dias_laborables_mes`: días laborables que usa por defecto el estimador (21).
- `precio_credito`: importe de un crédito en la moneda del panel, si el contrato lo fija. Se usa
  cuando la API informa créditos pero no importe.
- `dias_activo`: días sin uso a partir de los que un puesto pasa de activo a parado (7).
- `dias_reasignar`: días sin uso a partir de los que un puesto es candidato a reasignar (30).
- `nombre_servidor`: nombre que se muestra bajo el reloj. Vacío: el nombre de la máquina.
- `empresas`: nombre de cada sociedad por dominio de correo. Sin esto se usa el propio dominio.
- `retencion_dias`: cuánto histórico de uso se conserva (730).

Tras cambiar `config.json`: `sudo systemctl restart dashboard-ia`.

## Probar en local

    python3 proxy.py --demo        # datos simulados, no toca el disco
    python3 proxy.py               # datos reales

Abre http://localhost:8091

## Instalar o actualizar en Ubuntu

    sudo bash instalar.sh
    sudo nano /etc/dashboard-ia.env          # clave, UUID, usuario y contraseña (solo la primera vez)

El instalador crea el usuario de sistema `dashboard-ia`, copia todo a
`/opt/dashboard-ia` y reinicia el servicio. No pisa `config.json`, el `.env` ni los datos.

## Seguridad y privacidad

- El panel muestra nombres, emails y actividad individual del personal: define
  `DASH_USUARIO` y `DASH_CLAVE`, limita el puerto a la red interna
  (`ufw allow from <red> to any port 8091`) y da acceso solo a quien deba verlo.
- No se lee el contenido de las conversaciones, solo volúmenes de uso.
- La autenticación es HTTP Basic sin cifrar. Si sale de una red de confianza,
  ponlo detrás de nginx o Caddy con HTTPS y cambia `escucha` a `127.0.0.1`.
- La Admin key es de solo lectura y vive en `/etc/dashboard-ia.env` (root, 600),
  nunca en el repositorio. Si se expone, se revoca y se crea otra.
- El servicio corre sin privilegios y solo puede escribir en `data/`.

## Rutas

- `/` dashboard · `/api/datos` JSON en caché · `/api/refrescar` fuerza lectura · `/api/salud` sin autenticación
