#!/usr/bin/env python3
"""
Dashboard de licencias OpenAI / ChatGPT - SGP
---------------------------------------------
Servidor ligero (solo librería estándar de Python 3.8+) que:

  1. Lee los puestos de ChatGPT Business desde un CSV (data/chatgpt_miembros.csv).
  2. Consulta la organización de OpenAI Platform con una Admin key de solo lectura
     (usuarios, proyectos, coste y uso de los últimos 30 días).
  3. Guarda una foto diaria en data/historico.csv.
  4. Sirve el dashboard (index.html) y los datos en /api/datos.

Uso:
    python3 proxy.py                 # datos reales (según lo que esté configurado)
    python3 proxy.py --demo          # datos simulados, para enseñar el diseño
    python3 proxy.py --puerto 8090

Variables de entorno (ver dashboard-ia.env.ejemplo):
    OPENAI_ADMIN_KEY   Admin key de solo lectura (sk-admin-...)
    DASH_USUARIO       Usuario para proteger el dashboard (opcional)
    DASH_CLAVE         Contraseña para proteger el dashboard (opcional)
"""

import argparse
import base64
import csv
import hmac
import json
import os
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
ARCHIVO_CONFIG = BASE / "config.json"
ARCHIVO_MIEMBROS = DATA / "chatgpt_miembros.csv"
ARCHIVO_HISTORICO = DATA / "historico.csv"
ARCHIVO_HTML = BASE / "index.html"

API_OPENAI = "https://api.openai.com/v1"
DIAS_VENTANA = 30
DIAS_SIN_USO = 30

CONFIG_POR_DEFECTO = {
    "puerto": 8090,
    "escucha": "0.0.0.0",
    "refresco_minutos": 60,
    "organizacion": "SGP",
    "moneda_puestos": "USD",
    # Precio mensual por puesto de ChatGPT. Con 0 el dashboard no calcula coste.
    "precio_puesto": {"standard": 0, "premium": 0},
}

CAMPOS_HISTORICO = [
    "fecha", "chatgpt_total", "chatgpt_standard", "chatgpt_premium",
    "api_usuarios", "api_coste_30d",
]


# ----------------------------------------------------------------- utilidades

def log(*partes):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *partes, flush=True)


def cargar_config():
    cfg = json.loads(json.dumps(CONFIG_POR_DEFECTO))
    if ARCHIVO_CONFIG.exists():
        try:
            propio = json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8"))
            for clave, valor in propio.items():
                if isinstance(valor, dict) and isinstance(cfg.get(clave), dict):
                    cfg[clave].update(valor)
                else:
                    cfg[clave] = valor
        except (ValueError, OSError) as e:
            log("AVISO: config.json no se pudo leer, uso valores por defecto:", e)
    return cfg


def normalizar(texto):
    """minúsculas, sin acentos, espacios y guiones a '_'"""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return t.strip().lower().replace(" ", "_").replace("-", "_")


def a_numero(valor, defecto=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


FORMATOS_FECHA = (
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y",
    "%d/%m/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y",
)


def a_fecha(texto):
    t = str(texto or "").strip()
    if not t:
        return None
    t = t.replace("Z", "").split(".")[0].split("+")[0].strip()
    for formato in FORMATOS_FECHA:
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            continue
    return None


# ----------------------------------------------- ChatGPT Business (desde CSV)

ALIAS = {
    "nombre": ("name", "nombre", "user", "usuario", "full_name", "member", "miembro"),
    "email": ("email", "correo", "mail", "e_mail", "email_address", "correo_electronico"),
    "rol": ("role", "rol"),
    "puesto": ("seat_type", "seat", "puesto", "tipo", "tipo_de_puesto", "tipo_puesto", "licencia", "license"),
    "departamento": ("department", "departamento", "area", "dept"),
    "ultima_actividad": ("last_active", "last_activity", "ultima_actividad", "last_seen",
                         "last_day_active", "ultimo_uso", "ultimo_acceso"),
}


def tipo_puesto(valor):
    v = normalizar(valor)
    if not v:
        return "sin_dato"
    if "prem" in v:
        return "premium"
    if "stand" in v or "estandar" in v or v in ("chatgpt", "default", "normal"):
        return "standard"
    return v


def leer_miembros_chatgpt(cfg):
    """Devuelve el bloque 'chatgpt' del JSON a partir del CSV de miembros."""
    bloque = {"estado": "sin_configurar", "mensaje": "", "total": 0, "miembros": [],
              "por_tipo": {}, "por_rol": {}, "por_departamento": [],
              "sin_uso": None, "coste_mensual": None,
              "moneda": cfg.get("moneda_puestos", "USD"), "archivo_fecha": None}

    if not ARCHIVO_MIEMBROS.exists():
        bloque["mensaje"] = "Falta data/chatgpt_miembros.csv con la lista de miembros del workspace."
        return bloque

    try:
        crudo = ARCHIVO_MIEMBROS.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        bloque.update(estado="error", mensaje="No se pudo leer el CSV de miembros: %s" % e)
        return bloque

    lineas = [l for l in crudo.splitlines() if l.strip()]
    if not lineas:
        bloque.update(estado="error", mensaje="El CSV de miembros está vacío.")
        return bloque

    primera = lineas[0]
    delim = max((",", ";", "\t"), key=primera.count)
    filas = list(csv.reader(lineas, delimiter=delim))

    cabecera = [normalizar(c) for c in filas[0]]
    columnas = {}
    for campo, nombres in ALIAS.items():
        for i, c in enumerate(cabecera):
            if c in nombres:
                columnas[campo] = i
                break

    if "email" in columnas:
        filas = filas[1:]
    else:
        # Sin cabecera: formato de invitación masiva de ChatGPT -> email,rol,puesto
        columnas = {"email": 0, "rol": 1, "puesto": 2}

    def celda(fila, campo):
        i = columnas.get(campo)
        return fila[i].strip() if i is not None and i < len(fila) else ""

    hoy = date.today()
    miembros, vistos = [], set()
    for fila in filas:
        email = celda(fila, "email").lower()
        if "@" not in email or email in vistos:
            continue
        vistos.add(email)
        ultima = a_fecha(celda(fila, "ultima_actividad"))
        miembros.append({
            "nombre": celda(fila, "nombre") or email.split("@")[0],
            "email": email,
            "rol": normalizar(celda(fila, "rol")) or "member",
            "puesto": tipo_puesto(celda(fila, "puesto")),
            "departamento": celda(fila, "departamento"),
            "ultima_actividad": ultima.isoformat() if ultima else None,
            "dias_inactivo": (hoy - ultima).days if ultima else None,
        })

    if not miembros:
        bloque.update(estado="error",
                      mensaje="El CSV no tiene ninguna fila con email válido. Revisa la cabecera.")
        return bloque

    por_tipo, por_rol, por_dep = {}, {}, {}
    for m in miembros:
        por_tipo[m["puesto"]] = por_tipo.get(m["puesto"], 0) + 1
        por_rol[m["rol"]] = por_rol.get(m["rol"], 0) + 1
        if m["departamento"]:
            por_dep[m["departamento"]] = por_dep.get(m["departamento"], 0) + 1

    precios = cfg.get("precio_puesto", {})
    coste = sum(n * a_numero(precios.get(tipo)) for tipo, n in por_tipo.items())

    sin_uso = None
    if "ultima_actividad" in columnas:
        sin_uso = sum(1 for m in miembros
                      if m["dias_inactivo"] is None or m["dias_inactivo"] > DIAS_SIN_USO)

    miembros.sort(key=lambda m: m["nombre"].lower())
    bloque.update(
        estado="ok", total=len(miembros), miembros=miembros,
        por_tipo=por_tipo, por_rol=por_rol,
        por_departamento=sorted(({"nombre": k, "n": v} for k, v in por_dep.items()),
                                key=lambda d: -d["n"]),
        sin_uso=sin_uso,
        coste_mensual=round(coste, 2) if coste > 0 else None,
        archivo_fecha=datetime.fromtimestamp(ARCHIVO_MIEMBROS.stat().st_mtime).isoformat(timespec="minutes"),
    )
    return bloque


# ------------------------------------------------ OpenAI Platform (Admin API)

class ErrorOpenAI(Exception):
    pass


def openai_get(ruta, clave, parametros=None):
    url = API_OPENAI + ruta
    if parametros:
        url += "?" + urllib.parse.urlencode(parametros, doseq=True)
    peticion = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + clave,
        "Content-Type": "application/json",
        "User-Agent": "dashboard-ia-sgp/1.0",
    })
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = ""
        try:
            detalle = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise ErrorOpenAI("HTTP %s en %s %s" % (e.code, ruta, detalle)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ErrorOpenAI("Sin respuesta de %s: %s" % (ruta, e)) from None


def openai_lista(ruta, clave):
    """Listados con paginación por cursor (after / last_id)."""
    elementos, parametros = [], {"limit": 100}
    for _ in range(50):
        r = openai_get(ruta, clave, parametros)
        elementos.extend(r.get("data", []))
        if not r.get("has_more") or not r.get("last_id"):
            break
        parametros["after"] = r["last_id"]
    return elementos


def openai_cubos(ruta, clave, inicio, extra=None):
    """Endpoints de uso y coste: cubos diarios con paginación por next_page."""
    cubos, parametros = [], {"start_time": inicio, "bucket_width": "1d", "limit": 31}
    parametros.update(extra or {})
    for _ in range(20):
        r = openai_get(ruta, clave, parametros)
        cubos.extend(r.get("data", []))
        if not r.get("has_more") or not r.get("next_page"):
            break
        parametros["page"] = r["next_page"]
    return cubos


def leer_openai_api(clave):
    bloque = {"estado": "sin_configurar", "mensaje": "", "total_usuarios": 0, "usuarios": [],
              "proyectos": None, "coste_30d": None, "moneda": "USD", "coste_diario": [],
              "peticiones_30d": None, "tokens_30d": None, "avisos": []}
    if not clave:
        bloque["mensaje"] = "Falta la variable OPENAI_ADMIN_KEY (Admin key de solo lectura)."
        return bloque

    try:
        usuarios = openai_lista("/organization/users", clave)
    except ErrorOpenAI as e:
        bloque.update(estado="error", mensaje=str(e))
        return bloque

    inicio = int((datetime.now(timezone.utc) - timedelta(days=DIAS_VENTANA)).timestamp())

    try:
        proyectos = openai_lista("/organization/projects", clave)
        bloque["proyectos"] = sum(1 for p in proyectos if p.get("status", "active") == "active")
    except ErrorOpenAI as e:
        bloque["avisos"].append("Proyectos: %s" % e)

    try:
        diario, total = [], 0.0
        for cubo in openai_cubos("/organization/costs", clave, inicio):
            valor = sum(a_numero((res.get("amount") or {}).get("value"))
                        for res in cubo.get("results", []))
            for res in cubo.get("results", []):
                moneda = (res.get("amount") or {}).get("currency")
                if moneda:
                    bloque["moneda"] = moneda.upper()
            dia = datetime.fromtimestamp(cubo.get("start_time", 0), timezone.utc).date().isoformat()
            diario.append({"fecha": dia, "valor": round(valor, 4)})
            total += valor
        bloque["coste_diario"] = diario
        bloque["coste_30d"] = round(total, 2)
    except ErrorOpenAI as e:
        bloque["avisos"].append("Coste: %s" % e)

    uso = {}
    try:
        peticiones = tokens = 0
        for cubo in openai_cubos("/organization/usage/completions", clave, inicio,
                                 {"group_by": "user_id"}):
            for res in cubo.get("results", []):
                n = int(a_numero(res.get("num_model_requests")))
                t = int(a_numero(res.get("input_tokens")) + a_numero(res.get("output_tokens")))
                peticiones += n
                tokens += t
                u = uso.setdefault(res.get("user_id") or "", {"peticiones": 0, "tokens": 0})
                u["peticiones"] += n
                u["tokens"] += t
        bloque["peticiones_30d"] = peticiones
        bloque["tokens_30d"] = tokens
    except ErrorOpenAI as e:
        bloque["avisos"].append("Uso: %s" % e)

    lista = []
    for u in usuarios:
        alta = u.get("added_at")
        propio = uso.get(u.get("id"), {})
        lista.append({
            "nombre": u.get("name") or (u.get("email") or "").split("@")[0],
            "email": (u.get("email") or "").lower(),
            "rol": u.get("role") or "",
            "alta": datetime.fromtimestamp(alta, timezone.utc).date().isoformat() if alta else None,
            "peticiones": propio.get("peticiones", 0),
            "tokens": propio.get("tokens", 0),
        })
    lista.sort(key=lambda x: (-x["peticiones"], x["nombre"].lower()))
    bloque.update(estado="ok", usuarios=lista, total_usuarios=len(lista))
    return bloque


# ------------------------------------------------------------------ histórico

def leer_historico():
    if not ARCHIVO_HISTORICO.exists():
        return []
    try:
        with ARCHIVO_HISTORICO.open(encoding="utf-8", newline="") as f:
            return [fila for fila in csv.DictReader(f) if fila.get("fecha")]
    except OSError as e:
        log("AVISO: no se pudo leer el histórico:", e)
        return []


def guardar_foto_diaria(chatgpt, api):
    """Una fila por día; si ya existe la de hoy, se actualiza."""
    if chatgpt["estado"] != "ok" and api["estado"] != "ok":
        return
    filas = {f["fecha"]: f for f in leer_historico()}
    hoy = date.today().isoformat()
    previa = filas.get(hoy, {})
    fila = {"fecha": hoy}
    if chatgpt["estado"] == "ok":
        fila["chatgpt_total"] = chatgpt["total"]
        fila["chatgpt_standard"] = chatgpt["por_tipo"].get("standard", 0)
        fila["chatgpt_premium"] = chatgpt["por_tipo"].get("premium", 0)
    if api["estado"] == "ok":
        fila["api_usuarios"] = api["total_usuarios"]
        fila["api_coste_30d"] = api["coste_30d"] if api["coste_30d"] is not None else ""
    filas[hoy] = {c: fila.get(c, previa.get(c, "")) for c in CAMPOS_HISTORICO}

    DATA.mkdir(exist_ok=True)
    temporal = ARCHIVO_HISTORICO.with_suffix(".tmp")
    with temporal.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_HISTORICO, extrasaction="ignore")
        w.writeheader()
        for clave in sorted(filas):
            w.writerow(filas[clave])
    os.replace(temporal, ARCHIVO_HISTORICO)


def historico_para_json(filas):
    def entero(v):
        return int(a_numero(v)) if str(v).strip() != "" else None

    def decimal(v):
        return round(a_numero(v), 2) if str(v).strip() != "" else None

    return [{
        "fecha": f["fecha"],
        "chatgpt_total": entero(f.get("chatgpt_total", "")),
        "chatgpt_standard": entero(f.get("chatgpt_standard", "")),
        "chatgpt_premium": entero(f.get("chatgpt_premium", "")),
        "api_usuarios": entero(f.get("api_usuarios", "")),
        "api_coste_30d": decimal(f.get("api_coste_30d", "")),
    } for f in filas[-180:]]


# ----------------------------------------------------------------------- cruce

def cruzar(chatgpt, api):
    """Quién tiene puesto de ChatGPT, acceso a la API, o ambos (por email)."""
    if chatgpt["estado"] != "ok" or api["estado"] != "ok":
        return None
    a = {m["email"] for m in chatgpt["miembros"]}
    b = {u["email"] for u in api["usuarios"] if u["email"]}
    return {"solo_chatgpt": len(a - b), "ambos": len(a & b), "solo_api": len(b - a),
            "emails_solo_api": sorted(b - a)}


# ------------------------------------------------------------------------ demo

def datos_demo(cfg):
    """Datos simulados y deterministas para enseñar el diseño. No toca el disco."""
    deps = ["Administración", "Comercial", "Sistemas", "Producción", "Calidad", "RR. HH.", "Logística"]
    hoy = date.today()
    miembros = []
    for i in range(1, 49):
        inactivo = (i * 7) % 53
        miembros.append({
            "nombre": "Usuario %02d" % i, "email": "usuario%02d@demo.local" % i,
            "rol": "owner" if i == 1 else ("admin" if i in (2, 3) else "member"),
            "puesto": "premium" if i % 8 == 0 else "standard",
            "departamento": deps[i % len(deps)],
            "ultima_actividad": (hoy - timedelta(days=inactivo)).isoformat(),
            "dias_inactivo": inactivo,
        })
    por_tipo = {"standard": sum(m["puesto"] == "standard" for m in miembros),
                "premium": sum(m["puesto"] == "premium" for m in miembros)}
    por_dep = {}
    for m in miembros:
        por_dep[m["departamento"]] = por_dep.get(m["departamento"], 0) + 1
    chatgpt = {
        "estado": "ok", "mensaje": "", "total": len(miembros), "miembros": miembros,
        "por_tipo": por_tipo, "por_rol": {"owner": 1, "admin": 2, "member": len(miembros) - 3},
        "por_departamento": sorted(({"nombre": k, "n": v} for k, v in por_dep.items()),
                                   key=lambda d: -d["n"]),
        "sin_uso": sum(m["dias_inactivo"] > DIAS_SIN_USO for m in miembros),
        "coste_mensual": por_tipo["standard"] * 25 + por_tipo["premium"] * 60,
        "moneda": "USD", "archivo_fecha": datetime.now().isoformat(timespec="minutes"),
    }
    usuarios = [{
        "nombre": "Usuario %02d" % i, "email": "usuario%02d@demo.local" % i,
        "rol": "owner" if i == 1 else "reader",
        "alta": (hoy - timedelta(days=40 * i)).isoformat(),
        "peticiones": max(0, 4200 - i * 530), "tokens": max(0, 4200 - i * 530) * 1850,
    } for i in (1, 2, 3, 5, 8, 13, 21)]
    usuarios.append({"nombre": "Integración ERP", "email": "integracion@demo.local", "rol": "reader",
                     "alta": (hoy - timedelta(days=90)).isoformat(), "peticiones": 960, "tokens": 1520000})
    diario = [{"fecha": (hoy - timedelta(days=d)).isoformat(),
               "valor": round(2.2 + ((d * 37) % 11) * 0.45 + (0 if (hoy - timedelta(days=d)).weekday() < 5 else -1.8), 2)}
              for d in range(DIAS_VENTANA - 1, -1, -1)]
    api = {
        "estado": "ok", "mensaje": "", "total_usuarios": len(usuarios), "usuarios": usuarios,
        "proyectos": 4, "coste_30d": round(sum(d["valor"] for d in diario), 2), "moneda": "USD",
        "coste_diario": diario, "peticiones_30d": sum(u["peticiones"] for u in usuarios),
        "tokens_30d": sum(u["tokens"] for u in usuarios), "avisos": [],
    }
    historico = []
    for d in range(59, -1, -1):
        total = len(miembros) - (d // 6)
        premium = max(2, por_tipo["premium"] - d // 20)
        historico.append({"fecha": (hoy - timedelta(days=d)).isoformat(), "chatgpt_total": total,
                          "chatgpt_standard": total - premium, "chatgpt_premium": premium,
                          "api_usuarios": len(usuarios) - (1 if d > 25 else 0),
                          "api_coste_30d": round(api["coste_30d"] * (1 - d / 150), 2)})
    return chatgpt, api, historico


# ---------------------------------------------------------------------- estado

class Estado:
    def __init__(self, cfg, demo):
        self.cfg = cfg
        self.demo = demo
        self.datos = None
        self.cerrojo = threading.Lock()

    def refrescar(self):
        if self.demo:
            chatgpt, api, historico = datos_demo(self.cfg)
        else:
            chatgpt = leer_miembros_chatgpt(self.cfg)
            api = leer_openai_api(os.environ.get("OPENAI_ADMIN_KEY", "").strip())
            try:
                guardar_foto_diaria(chatgpt, api)
            except OSError as e:
                log("AVISO: no se pudo guardar el histórico:", e)
            historico = historico_para_json(leer_historico())
        datos = {
            "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
            "demo": self.demo,
            "organizacion": self.cfg.get("organizacion", ""),
            "refresco_minutos": self.cfg.get("refresco_minutos", 60),
            "dias_ventana": DIAS_VENTANA,
            "dias_sin_uso": DIAS_SIN_USO,
            "chatgpt": chatgpt,
            "api": api,
            "cruce": cruzar(chatgpt, api),
            "historico": historico,
        }
        with self.cerrojo:
            self.datos = datos
        log("Datos actualizados - ChatGPT: %s (%s puestos) - API: %s (%s usuarios)" % (
            chatgpt["estado"], chatgpt["total"], api["estado"], api["total_usuarios"]))

    def bucle(self):
        while True:
            time.sleep(max(5, int(self.cfg.get("refresco_minutos", 60))) * 60)
            try:
                self.refrescar()
            except Exception as e:  # el servicio no debe caerse por un fallo de lectura
                log("ERROR al refrescar:", repr(e))

    def json(self):
        with self.cerrojo:
            return json.dumps(self.datos, ensure_ascii=False).encode("utf-8")


# -------------------------------------------------------------------- servidor

def crear_manejador(estado):
    usuario = os.environ.get("DASH_USUARIO", "")
    clave = os.environ.get("DASH_CLAVE", "")
    esperado = base64.b64encode(("%s:%s" % (usuario, clave)).encode()).decode() if usuario and clave else None

    class Manejador(BaseHTTPRequestHandler):
        server_version = "dashboard-ia"

        def log_message(self, formato, *args):
            pass

        def autorizado(self):
            if not esperado:
                return True
            cabecera = self.headers.get("Authorization", "")
            return cabecera.startswith("Basic ") and hmac.compare_digest(cabecera[6:].strip(), esperado)

        def responder(self, codigo, cuerpo, tipo, extra=None):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cuerpo)

        def do_GET(self):
            ruta = urllib.parse.urlparse(self.path).path
            if ruta == "/api/salud":
                return self.responder(200, b'{"ok":true}', "application/json")
            if not self.autorizado():
                return self.responder(401, b"Acceso restringido", "text/plain; charset=utf-8",
                                      {"WWW-Authenticate": 'Basic realm="Dashboard IA"'})
            if ruta in ("/", "/index.html"):
                try:
                    return self.responder(200, ARCHIVO_HTML.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.responder(500, b"Falta index.html", "text/plain; charset=utf-8")
            if ruta == "/api/datos":
                return self.responder(200, estado.json(), "application/json; charset=utf-8")
            if ruta == "/api/refrescar":
                try:
                    estado.refrescar()
                except Exception as e:
                    log("ERROR al refrescar:", repr(e))
                return self.responder(200, estado.json(), "application/json; charset=utf-8")
            return self.responder(404, b"No encontrado", "text/plain; charset=utf-8")

        do_HEAD = do_GET

    return Manejador


def main():
    cfg = cargar_config()
    parser = argparse.ArgumentParser(description="Dashboard de licencias OpenAI / ChatGPT")
    parser.add_argument("--puerto", type=int, default=int(cfg.get("puerto", 8090)))
    parser.add_argument("--escucha", default=cfg.get("escucha", "0.0.0.0"))
    parser.add_argument("--demo", action="store_true", help="datos simulados")
    args = parser.parse_args()

    DATA.mkdir(exist_ok=True)
    estado = Estado(cfg, args.demo or bool(cfg.get("demo")))
    estado.refrescar()
    threading.Thread(target=estado.bucle, daemon=True).start()

    servidor = ThreadingHTTPServer((args.escucha, args.puerto), crear_manejador(estado))
    log("Dashboard en http://%s:%s %s" % (args.escucha, args.puerto, "(MODO DEMO)" if estado.demo else ""))
    if not (os.environ.get("DASH_USUARIO") and os.environ.get("DASH_CLAVE")):
        log("AVISO: sin DASH_USUARIO/DASH_CLAVE el dashboard no pide contraseña.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    sys.exit(main())
