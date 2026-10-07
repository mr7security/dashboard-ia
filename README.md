# Dashboard de licencias OpenAI / ChatGPT

Panel web interno con los puestos de ChatGPT Business y los usuarios, coste y uso
de la API de OpenAI. Mismo planteamiento que `dashboard-sensores`: Python con
librería estándar (`proxy.py`), un `index.html`, histórico en CSV y servicio systemd.

## De dónde salen los datos

| Fuente | Cómo | Qué da |
|---|---|---|
| OpenAI Platform | Admin API con una Admin key de solo lectura | Usuarios, roles, proyectos, coste y uso de 30 días |
| ChatGPT Business | `data/chatgpt_miembros.csv` (no hay API en el plan Business) | Miembros, rol, tipo de puesto, área, última actividad |
| Histórico | `data/historico.csv`, una fila por día | Evolución de puestos y coste |

### CSV de miembros de ChatGPT

Sale de ChatGPT > Workspace settings > Members. Columnas reconocidas (en inglés o
español, separadas por coma o punto y coma); solo `email` es obligatoria:

    name,email,role,seat_type,department,last_active

También vale el formato de invitación masiva sin cabecera: `email,rol,puesto`.
Ver `chatgpt_miembros.ejemplo.csv`. Al reemplazar el archivo, el dashboard lo
recoge en el siguiente refresco o al pulsar **Actualizar**.

### Admin key de OpenAI

platform.openai.com > Organization settings > Admin keys > crear clave con
permiso **Read only**. Solo la puede crear un Organization Owner.
Va en `/etc/dashboard-ia.env`, nunca en el repositorio.

## Probar en local

    python3 proxy.py --demo        # datos simulados
    python3 proxy.py               # datos reales

Abre http://localhost:8090

## Instalar en Ubuntu

    sudo bash instalar.sh
    sudo nano /etc/dashboard-ia.env          # Admin key + usuario y contraseña
    sudo cp miembros.csv /opt/dashboard-ia/data/chatgpt_miembros.csv
    sudo chown dashboard-ia: /opt/dashboard-ia/data/chatgpt_miembros.csv
    sudo systemctl restart dashboard-ia

El instalador crea el usuario de sistema `dashboard-ia`, copia todo a
`/opt/dashboard-ia` y activa el servicio. Para actualizar, vuelve a ejecutarlo:
no pisa `config.json`, el `.env` ni los datos.

## Configuración (`config.json`)

- `puerto`, `escucha`: dónde escucha el servidor (por defecto `0.0.0.0:8090`).
- `refresco_minutos`: cada cuánto consulta las fuentes (mínimo 5).
- `precio_puesto`: precio mensual por puesto `standard` y `premium`. Con 0 no se muestra el coste.
- `moneda_puestos`: moneda de esos precios.

## Seguridad

- El panel muestra nombres y emails del personal: define `DASH_USUARIO` y
  `DASH_CLAVE` y limita el puerto a la red interna (`ufw allow from <red> to any port 8090`).
- La autenticación es HTTP Basic sin cifrar. Si sale de una red de confianza,
  ponlo detrás de nginx o Caddy con HTTPS y cambia `escucha` a `127.0.0.1`.
- La Admin key es de solo lectura y vive en `/etc/dashboard-ia.env` (root, 600).
- El servicio corre sin privilegios y solo puede escribir en `data/`.

## Rutas

- `/` dashboard · `/api/datos` JSON en caché · `/api/refrescar` fuerza lectura · `/api/salud` sin autenticación
