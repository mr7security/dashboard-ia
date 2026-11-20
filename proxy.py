#!/usr/bin/env python3
"""
Dashboard de ChatGPT Enterprise - SGP
-------------------------------------
Servidor ligero (solo librería estándar de Python 3.8+) que:

  1. Lee el workspace de ChatGPT Enterprise con una Admin key del workspace:
     miembros y uso diario por persona, modelo y aplicación.
  2. Acumula el uso diario en data/uso_diario.json, de modo que el histórico
     crece más allá de los ~30 días que devuelve la API.
  3. Detecta altas y bajas de miembros (data/eventos.csv) y guarda una foto
     diaria de puestos (data/historico.csv).
  4. Sirve el dashboard (index.html) y los datos en /api/datos.

Uso:
    python3 proxy.py                 # datos reales
    python3 proxy.py --demo          # datos simulados, no toca el disco
    python3 proxy.py --puerto 8091

Variables de entorno (ver dashboard-ia.env.ejemplo):
    CHATGPT_ADMIN_KEY      Admin key del workspace (Admin Console > Credentials)
    CHATGPT_WORKSPACE_ID   UUID del workspace de ChatGPT
    DASH_USUARIO           Usuario para proteger el dashboard (opcional)
    DASH_CLAVE             Contraseña para proteger el dashboard (opcional)
"""

import argparse
import base64
import csv
import gzip
import hmac
import json
import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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
ARCHIVO_HTML = BASE / "index.html"
ARCHIVO_USO = DATA / "uso_diario.json"
ARCHIVO_MIEMBROS = DATA / "miembros.json"
ARCHIVO_EVENTOS = DATA / "eventos.csv"
ARCHIVO_HISTORICO = DATA / "historico.csv"
ARCHIVO_RELLENO = DATA / "relleno.json"
ARCHIVO_LIMITES = DATA / "limites.json"

API_CHATGPT = "https://api.chatgpt.com/v1"
MESES_RELLENO = 24
# Al cambiar lo que se guarda de cada registro, se sube para volver a recuperar el histórico.
VERSION_ALMACEN = 2

CONFIG_POR_DEFECTO = {
    "puerto": 8091,
    "escucha": "0.0.0.0",
    "refresco_minutos": 60,
    "organizacion": "SGP",
    "moneda": "USD",
    # Precio mensual por tipo de puesto tal como lo da la API (chatgpt, codex...).
    "precio_puesto": {"chatgpt": 0},
    # Compromiso anual de gasto del contrato (puestos + consumo), en la moneda del
    # panel. El margen para consumo se calcula restando el coste anual de los puestos.
    "compromiso_anual": 0,
    # Alternativa si no hay compromiso: bolsa anual solo para consumo.
    "bolsa_anual": 0,
    # Día en que empieza el contrato (AAAA-MM-DD). Vacío: primer día con datos.
    "contrato_inicio": "",
    # Cambio para pasar a la moneda del panel los importes que la API da en USD.
    "usd_a_moneda": 1,
    # Importe de un crédito en la moneda del panel, si el contrato lo fija. Con 0 se
    # usa solo el importe que informe la API.
    "precio_credito": 0,
    "dias_activo": 7,        # activo = con uso en los últimos N días
    "dias_reasignar": 30,    # candidato a reasignar = sin uso en N días
    "retencion_dias": 730,   # cuánto histórico de uso se conserva
    # Nombre que se muestra bajo el reloj. Vacío: el nombre de la máquina.
    "nombre_servidor": "",
    # Nombre de cada empresa por dominio de correo: {"ejemplo.es": "Empresa"}
    "empresas": {},
}

CAMPOS_HISTORICO = ["fecha", "chatgpt_total", "chatgpt_activos_7d", "chatgpt_sin_uso"]
CAMPOS_EVENTOS = ["fecha", "tipo", "email", "nombre"]


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
            if "moneda" not in propio and propio.get("moneda_puestos"):
                cfg["moneda"] = propio["moneda_puestos"]
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


def a_entero(valor):
    return int(a_numero(valor))


def opcional(valor):
    """Valores que la API puede dar como null (créditos, coste)."""
    return None if valor is None else round(a_numero(valor), 4)


def hoy_utc():
    return datetime.now(timezone.utc).date()


def escribir_json(ruta, objeto):
    DATA.mkdir(exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(objeto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporal, ruta)


def leer_json(ruta, defecto):
    if not ruta.exists():
        return defecto
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log("AVISO: no se pudo leer %s: %s" % (ruta.name, e))
        return defecto


def empresa_de(email, cfg):
    dominio = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if not dominio:
        return "Sin dominio"
    propio = cfg.get("empresas") or {}
    if dominio in propio:
        return str(propio[dominio])
    etiqueta = dominio.split(".")[0]
    return etiqueta[:1].upper() + etiqueta[1:]


# ------------------------------------------ ChatGPT Enterprise (Admin API)

class ErrorAPI(Exception):
    codigo = None


def api_get(ruta, clave, parametros=None):
    url = API_CHATGPT + ruta
    if parametros:
        url += "?" + urllib.parse.urlencode(parametros, doseq=True)
    peticion = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + clave,
        "Content-Type": "application/json",
        "User-Agent": "dashboard-ia-sgp/3.0",
    })
    try:
        with urllib.request.urlopen(peticion, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = ""
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", "")
            detalle = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            pass
        error = ErrorAPI("HTTP %s en %s %s" % (e.code, ruta, detalle))
        error.codigo = e.code
        raise error from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ErrorAPI("Sin respuesta de %s: %s" % (ruta, e)) from None


def chatgpt_usuarios(clave, ws):
    """Miembros del workspace. Paginación por cursor (after / last_id)."""
    ruta = "/manage/workspaces/%s/users" % urllib.parse.quote(ws, safe="")
    elementos, parametros, vistos = [], {}, set()
    for _ in range(200):
        r = api_get(ruta, clave, parametros)
        elementos.extend(r.get("data") or [])
        cursor = r.get("last_id")
        if not r.get("has_more") or not cursor or cursor in vistos:
            break
        vistos.add(cursor)
        parametros = {"after": cursor}
    return elementos


def chatgpt_uso_ventana(clave, ws, parametros, avisos):
    """Una consulta de uso completa (todas sus páginas, por next_page)."""
    ruta = "/analytics/workspaces/%s/usage" % urllib.parse.quote(ws, safe="")
    r = api_get(ruta, clave, parametros)
    registros, vistos = list(r.get("data") or []), set()
    for _ in range(2000):
        cursor = r.get("next_page")
        if not r.get("has_more") or not cursor or cursor in vistos:
            break
        vistos.add(cursor)
        try:
            r = api_get(ruta, clave, dict(parametros, page=cursor))
        except ErrorAPI as e:
            avisos.append("Uso incompleto (falló una página): %s" % e)
            break
        registros.extend(r.get("data") or [])
    return registros


def chatgpt_uso(clave, ws, avisos, rellenar=False):
    """Uso diario por persona.

    Sin parámetros la API devuelve los últimos ~30 días. Con start_time devuelve
    los ~30 días que empiezan en esa fecha, así que el histórico anterior se
    recupera retrocediendo de ventana en ventana hasta encontrar dos vacías.
    Devuelve (registros, relleno_completo)."""
    registros = chatgpt_uso_ventana(clave, ws, {}, avisos)
    if not rellenar:
        return registros, False
    ahora, vacias = datetime.now(timezone.utc), 0
    for k in range(1, MESES_RELLENO + 1):
        inicio = int((ahora - timedelta(days=30 * k + 29)).timestamp())
        try:
            tramo = chatgpt_uso_ventana(clave, ws, {"start_time": inicio}, avisos)
        except ErrorAPI as e:
            avisos.append("Histórico anterior no recuperado: %s" % e)
            return registros, e.codigo in (400, 422)
        registros.extend(tramo)
        vacias = 0 if tramo else vacias + 1
        if vacias >= 2:
            break
    return registros, True


# -------------------------------------------------------- almacén de uso

def compactar(r):
    """Registro de la API -> (clave, registro compacto) o None si no es válido."""
    actor = r.get("actor") or {}
    email = (actor.get("email") or "").strip().lower()
    uid = r.get("user_id") or actor.get("user_id") or ""
    if not (email or uid):
        return None
    dia = datetime.fromtimestamp(a_numero(r.get("start_time")), timezone.utc).date().isoformat()
    t = r.get("totals") or {}
    compacto = {
        "e": email, "u": uid,
        "m": a_entero(t.get("messages")), "c": a_entero(t.get("chats")),
        "ti": a_entero(t.get("uncached_text_input_tokens")),
        "tc": a_entero(t.get("cached_text_input_tokens")),
        "to": a_entero(t.get("text_output_tokens")),
        "tt": a_entero(t.get("text_total_tokens")),
        "cr": opcional(t.get("credits")), "co": opcional(t.get("cost_usd")),
        "cl": [[str(c.get("client_id") or "otro"), a_entero(c.get("messages")), a_entero(c.get("chats")),
                opcional(c.get("credits")), opcional(c.get("cost_usd"))] for c in r.get("clients") or []],
        "mo": [[str(m.get("model") or "other"), str(m.get("speed") or ""),
                a_entero(m.get("text_total_tokens")), opcional(m.get("credits")), opcional(m.get("cost_usd")),
                a_entero(m.get("uncached_text_input_tokens")), a_entero(m.get("cached_text_input_tokens")),
                a_entero(m.get("text_output_tokens"))]
               for m in r.get("models") or []],
    }
    return "%s|%s" % (dia, email or uid), compacto


def fusionar(almacen, registros, retencion_dias):
    """Mete los registros nuevos en el almacén (los días devueltos se pisan)."""
    for r in registros:
        par = compactar(r)
        if par:
            almacen[par[0]] = par[1]
    limite = (hoy_utc() - timedelta(days=int(retencion_dias))).isoformat()
    for clave in [k for k in almacen if k[:10] < limite]:
        del almacen[clave]
    return almacen


# -------------------------------------------------------- miembros y eventos

def leer_eventos():
    if not ARCHIVO_EVENTOS.exists():
        return []
    try:
        with ARCHIVO_EVENTOS.open(encoding="utf-8", newline="") as f:
            return [fila for fila in csv.DictReader(f) if fila.get("fecha")]
    except OSError as e:
        log("AVISO: no se pudieron leer los eventos:", e)
        return []


def registrar_altas_bajas(usuarios):
    """Compara con la última lista guardada y anota altas y bajas."""
    actual = {}
    for u in usuarios:
        email = (u.get("email") or "").strip().lower()
        if email:
            actual[email] = u.get("name") or email.split("@")[0]
    previo = leer_json(ARCHIVO_MIEMBROS, None)
    if isinstance(previo, dict) and actual:
        hoy = date.today().isoformat()
        nuevos = [{"fecha": hoy, "tipo": "alta", "email": e, "nombre": n}
                  for e, n in actual.items() if e not in previo]
        nuevos += [{"fecha": hoy, "tipo": "baja", "email": e, "nombre": n}
                   for e, n in previo.items() if e not in actual]
        if nuevos:
            DATA.mkdir(exist_ok=True)
            existe = ARCHIVO_EVENTOS.exists()
            with ARCHIVO_EVENTOS.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CAMPOS_EVENTOS)
                if not existe:
                    w.writeheader()
                w.writerows(nuevos)
    if actual:
        escribir_json(ARCHIVO_MIEMBROS, actual)


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


def guardar_foto_diaria(resumen):
    """Una fila por día; si ya existe la de hoy, se actualiza."""
    filas = {f["fecha"]: f for f in leer_historico()}
    hoy = date.today().isoformat()
    filas[hoy] = {"fecha": hoy, "chatgpt_total": resumen["total"],
                  "chatgpt_activos_7d": resumen["activos"], "chatgpt_sin_uso": resumen["sin_uso"]}
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
        return a_entero(v) if str(v if v is not None else "").strip() != "" else None
    return [{"fecha": f["fecha"], "total": entero(f.get("chatgpt_total")),
             "activos": entero(f.get("chatgpt_activos_7d")), "sin_uso": entero(f.get("chatgpt_sin_uso"))}
            for f in filas[-730:]]


# ------------------------------------------------------------ datos al panel

def construir(cfg, usuarios, almacen):
    """Tablas compactas para el panel: el filtrado y los cálculos se hacen allí."""
    personas, indice = [], {}

    def persona(email, uid, nombre=None, miembro=False, rol="", puesto="", scim=False):
        clave = email or uid
        if clave in indice:
            return indice[clave]
        indice[clave] = len(personas)
        personas.append({
            "email": email, "nombre": nombre or (email.split("@")[0] if email else uid) or "(sin nombre)",
            "empresa": empresa_de(email, cfg), "miembro": miembro, "rol": rol, "puesto": puesto, "scim": scim,
        })
        return indice[clave]

    por_uid = {}
    for u in usuarios:
        email = (u.get("email") or "").strip().lower()
        i = persona(email, u.get("id") or "", u.get("name"), True, normalizar(u.get("role")) or "member",
                    normalizar(u.get("seat_type")) or "sin_dato", bool(u.get("is_scim_managed")))
        if u.get("id"):
            por_uid[u["id"]] = i

    dias_con_datos = sorted({k[:10] for k in almacen})
    dias, pos = [], {}
    if dias_con_datos:
        d, fin = date.fromisoformat(dias_con_datos[0]), date.fromisoformat(dias_con_datos[-1])
        while d <= fin:
            pos[d.isoformat()] = len(dias)
            dias.append(d.isoformat())
            d += timedelta(days=1)

    def catalogo():
        nombres, idx = [], {}

        def de(nombre):
            if nombre not in idx:
                idx[nombre] = len(nombres)
                nombres.append(nombre)
            return idx[nombre]
        return nombres, de

    modelos, i_modelo = catalogo()
    velocidades, i_velocidad = catalogo()
    clientes, i_cliente = catalogo()
    uso, uso_modelos, uso_clientes = [], [], []

    for clave in sorted(almacen):
        r = almacen[clave]
        if not (r["m"] or r["c"] or r["tt"] or r["cr"]):
            continue
        d = pos[clave[:10]]
        p = indice.get(r["e"]) if r["e"] else None
        if p is None:
            p = por_uid.get(r["u"])
        if p is None:
            p = persona(r["e"], r["u"])
        uso.append([d, p, r["m"], r["c"], r["ti"], r["tc"], r["to"], r["tt"], r["cr"], r["co"]])
        for m in r["mo"]:
            # Los registros guardados por versiones anteriores traen menos campos.
            nombre, velocidad, tokens, creditos, coste, t_in, t_cache, t_out = (list(m) + [None] * 4)[:8]
            if tokens or creditos or coste:
                uso_modelos.append([d, p, i_modelo(nombre), i_velocidad(velocidad or "sin dato"), tokens,
                                    creditos, coste, t_in or 0, t_cache or 0, t_out or 0])
        for c in r["cl"]:
            nombre, mensajes, chats, creditos, coste = (list(c) + [None])[:5]
            if mensajes or chats or creditos or coste:
                uso_clientes.append([d, p, i_cliente(nombre), mensajes, chats, creditos, coste])

    return {"personas": personas, "dias": dias, "uso": uso, "uso_modelos": uso_modelos,
            "uso_clientes": uso_clientes, "modelos": modelos, "velocidades": velocidades,
            "clientes": clientes}


def anadir_bajas(tablas, eventos, cfg):
    """Marca la fecha de baja de quien ya no es miembro y añade a quien se dio de
    baja sin uso guardado, para que el panel también lo muestre."""
    por_email = {p["email"]: p for p in tablas["personas"] if p["email"]}
    for e in sorted(eventos, key=lambda x: x.get("fecha") or ""):
        email = (e.get("email") or "").strip().lower()
        if e.get("tipo") != "baja" or not email:
            continue
        p = por_email.get(email)
        if p is None:
            p = {"email": email, "nombre": e.get("nombre") or email.split("@")[0],
                 "empresa": empresa_de(email, cfg), "miembro": False, "rol": "", "puesto": "", "scim": False}
            tablas["personas"].append(p)
            por_email[email] = p
        if not p["miembro"]:
            p["baja"] = e.get("fecha")


def resumen_puestos(tablas, dias_activo):
    """Totales para el registro y la foto diaria."""
    hoy, ultima = hoy_utc(), {}
    for fila in tablas["uso"]:
        d = tablas["dias"][fila[0]]
        if fila[1] not in ultima or d > ultima[fila[1]]:
            ultima[fila[1]] = d
    total = activos = sin_uso = 0
    for i, p in enumerate(tablas["personas"]):
        if not p["miembro"]:
            continue
        total += 1
        if i not in ultima:
            sin_uso += 1
        elif (hoy - date.fromisoformat(ultima[i])).days <= dias_activo:
            activos += 1
    return {"total": total, "activos": activos, "sin_uso": sin_uso}


def publico(cfg):
    return {"moneda": cfg.get("moneda", "USD"), "precio_puesto": cfg.get("precio_puesto", {}),
            "compromiso_anual": a_numero(cfg.get("compromiso_anual")),
            "bolsa_anual": a_numero(cfg.get("bolsa_anual")),
            "contrato_inicio": str(cfg.get("contrato_inicio") or cfg.get("bolsa_inicio") or ""),
            "usd_a_moneda": a_numero(cfg.get("usd_a_moneda"), 1.0) or 1.0,
            "precio_credito": a_numero(cfg.get("precio_credito")),
            "dias_laborables_mes": int(a_numero(cfg.get("dias_laborables_mes"), 21) or 21),
            "dias_activo": int(cfg.get("dias_activo", 7)), "dias_reasignar": int(cfg.get("dias_reasignar", 30))}


# ------------------------------------------------------- límites de uso

def regla_limite(r):
    """Regla de límite de la API -> {tipo, importe, unidad, expira[, origen]} o None.

    La API da el límite propio como lista de una regla, y el efectivo y el
    heredado como {limit: regla, source: {kind}}."""
    if isinstance(r, list):
        r = r[0] if r else None
    if not isinstance(r, dict):
        return None
    if isinstance(r.get("limit"), dict):
        regla = regla_limite(r["limit"]) or {"tipo": "", "importe": None, "unidad": None, "expira": None}
        regla["origen"] = str((r.get("source") or {}).get("kind") or "")
        return regla
    # La API da el mismo límite en créditos ("limit") y en dinero ("limit_amount").
    importe = unidad = None
    creditos = a_numero(r.get("limit"), None) if r.get("limit") is not None else None
    cantidad = r.get("limit_amount")
    if isinstance(cantidad, dict):
        importe, unidad = a_numero(cantidad.get("amount"), None), cantidad.get("unit")
    elif creditos is not None:
        importe, unidad = creditos, "credit"
    regla = {"tipo": str(r.get("type") or ""), "importe": importe, "unidad": unidad,
             "expira": r.get("limit_expires_at")}
    if creditos is not None and unidad != "credit":
        regla["creditos"] = creditos
    return regla


def gasto_del_periodo(d):
    """Gasto del periodo -> (importe, unidad, créditos). Prefiere el importe en dinero."""
    creditos = None
    if d.get("current_month_usage_unit") == "credit" and d.get("current_month_usage") is not None:
        creditos = a_numero(d.get("current_month_usage"), None)
    cantidad = d.get("current_month_usage_amount")
    if isinstance(cantidad, dict) and cantidad.get("amount") is not None:
        return a_numero(cantidad.get("amount"), None), cantidad.get("unit"), creditos
    if d.get("current_month_usage") is None:
        return None, None, None
    return a_numero(d.get("current_month_usage"), None), d.get("current_month_usage_unit"), creditos


def leer_limites(clave, ws, usuarios):
    """Límite mensual de cada persona y uso del periodo, con la API de límites de uso."""
    base = "/manage/workspaces/%s/usage_limits" % urllib.parse.quote(ws, safe="")
    res = {"estado": "ok", "mensaje": "", "unidad": None, "general": None, "personas": [], "fallos": 0}
    try:
        w = api_get(base + "/workspace", clave)
    except ErrorAPI as e:
        res.update(estado="sin_permiso" if e.codigo in (401, 403) else "error", mensaje=str(e))
        return res
    for campo in ("default_user_monthly_usage_limit", "default_monthly_usage_limit", "monthly_usage_limit"):
        if isinstance(w, dict) and w.get(campo) is not None:
            res["general"] = regla_limite(w[campo])
            break

    def una(u):
        ruta = "%s/users/%s" % (base, urllib.parse.quote(str(u.get("id")), safe=""))
        d = api_get(ruta, clave)
        # El gasto del periodo suele venir ya en esta respuesta; si no, se pide aparte.
        if d.get("current_month_usage") is None and d.get("current_month_usage_amount") is None:
            try:
                d = dict(d, **{k: v for k, v in api_get(ruta + "/monthly-usage", clave).items()
                               if k.startswith("current_month_usage")})
            except ErrorAPI:
                pass
        usado, unidad, creditos = gasto_del_periodo(d)
        return {"email": (u.get("email") or "").strip().lower(), "id": u.get("id"),
                "propio": regla_limite(d.get("override_monthly_usage_limit")),
                "efectivo": regla_limite(d.get("effective_monthly_usage_limit")),
                "heredado": regla_limite(d.get("inherited_monthly_usage_limit")),
                "usado": usado, "unidad": unidad, "usado_creditos": creditos}

    def segura(u):
        try:
            return una(u)
        except ErrorAPI as e:
            return e

    con_id = [u for u in usuarios if u.get("id")]
    with ThreadPoolExecutor(max_workers=4) as hilos:
        resultados = list(hilos.map(segura, con_id))
    errores = [r for r in resultados if isinstance(r, ErrorAPI)]
    res["personas"] = [r for r in resultados if not isinstance(r, ErrorAPI)]
    res["fallos"] = len(errores)
    if errores and not res["personas"]:
        e = errores[0]
        res.update(estado="sin_permiso" if e.codigo in (401, 403) else "error", mensaje=str(e))
        return res
    unidades = [p["unidad"] for p in res["personas"] if p["unidad"]]
    res["unidad"] = max(set(unidades), key=unidades.count) if unidades else None
    # Sin límite general explícito, se deduce del que heredan las personas sin límite propio.
    if res["general"] is None:
        for p in res["personas"]:
            ref = p["heredado"] or (p["efectivo"] if not p["propio"] else None)
            if ref and ref.get("origen") in ("workspace_default", ""):
                res["general"] = {k: ref.get(k) for k in ("tipo", "importe", "unidad", "expira", "creditos")
                                  if k in ref}
                break
    return res


def paginas(ruta, clave, tope=200):
    """Todas las páginas de una lista. Admite los dos cursores que usa la API: cursor y last_id."""
    elementos, parametros, vistos = [], {"limit": 100}, set()
    for _ in range(tope):
        r = api_get(ruta, clave, parametros)
        if isinstance(r, list):
            return elementos + r
        elementos.extend(r.get("data") or [])
        if not r.get("has_more"):
            break
        if r.get("cursor"):
            parametros = {"limit": 100, "cursor": r["cursor"]}
        elif r.get("last_id"):
            parametros = {"limit": 100, "after": r["last_id"]}
        else:
            break
        marca = parametros.get("cursor") or parametros.get("after")
        if marca in vistos:
            break
        vistos.add(marca)
    return elementos


def leer_grupos(clave, ws, usuarios):
    """Grupos del workspace y quién pertenece a cada uno."""
    base = "/manage/workspaces/%s/groups" % urllib.parse.quote(ws, safe="")
    res = {"estado": "ok", "mensaje": "", "grupos": [], "fallos": 0}
    try:
        lista = paginas(base, clave)
    except ErrorAPI as e:
        codigo = getattr(e, "codigo", None)
        res.update(estado="sin_permiso" if codigo in (401, 403) else "error", mensaje=str(e))
        return res
    email_de = {str(u.get("id")): (u.get("email") or "").strip().lower() for u in usuarios if u.get("id")}

    def miembros(g):
        try:
            filas = paginas("%s/%s/users" % (base, urllib.parse.quote(str(g.get("id")), safe="")), clave)
        except ErrorAPI as e:
            return e
        emails = set()
        for f in filas:
            if not isinstance(f, dict) or f.get("status") == "deactivated":
                continue
            email = email_de.get(str(f.get("id"))) or (f.get("email") or "").strip().lower()
            if email:
                emails.add(email)
        return sorted(emails)

    grupos = [g for g in lista if isinstance(g, dict) and g.get("id")]
    with ThreadPoolExecutor(max_workers=4) as hilos:
        resultados = list(hilos.map(miembros, grupos))
    for g, m in zip(grupos, resultados):
        if isinstance(m, ErrorAPI):
            res["fallos"] += 1
            res["mensaje"] = str(m)
            m = []
        res["grupos"].append({"nombre": g.get("name") or g.get("display_name") or str(g.get("id")),
                              "descripcion": g.get("description") or "", "miembros": m})
    if grupos and res["fallos"] == len(grupos):
        res["estado"] = "error"
    res["grupos"].sort(key=lambda g: g["nombre"].lower())
    return res


def seguir_limites(limites):
    """Anota desde cuándo tiene cada persona su límite propio y guarda el historial."""
    previo = leer_json(ARCHIVO_LIMITES, None)
    primera_vez = not isinstance(previo, dict)
    guardado = {} if primera_vez else previo.get("actual", {})
    historial = [] if primera_vez else list(previo.get("historial", []))
    hoy = date.today().isoformat()
    inicio = hoy if primera_vez else previo.get("inicio", hoy)
    actual = {}
    for p in limites["personas"]:
        uid = str(p["id"])
        if not p["propio"]:
            continue
        firma = json.dumps({k: p["propio"].get(k) for k in ("tipo", "importe", "unidad", "expira")}, sort_keys=True)
        antes = guardado.get(uid)
        if antes and antes.get("firma") == firma:
            desde = antes.get("desde")
        else:
            desde = None if primera_vez else hoy
            if not primera_vez:
                evento = {"fecha": hoy, "cambio": "cambio" if antes else "alta", "email": p["email"],
                          "limite": p["propio"], "general": limites.get("general")}
                if antes:
                    evento["anterior"], evento["desde"] = antes.get("limite"), antes.get("desde")
                historial.append(evento)
        # Se guarda también el límite para poder decir de cuánto era cuando termine.
        actual[uid] = {"firma": firma, "desde": desde, "email": p["email"], "limite": p["propio"],
                       "general": limites.get("general")}
        p["desde"] = desde
    for uid, antes in guardado.items():
        if uid not in actual:
            historial.append({"fecha": hoy, "cambio": "fin", "email": antes.get("email", ""), "limite": None,
                              "anterior": antes.get("limite"), "desde": antes.get("desde"),
                              "general": antes.get("general")})
    historial = historial[-5000:]
    escribir_json(ARCHIVO_LIMITES, {"inicio": inicio, "actual": actual, "historial": historial})
    limites["inicio"] = inicio
    limites["historial"] = historial


def leer_real(cfg):
    clave = os.environ.get("CHATGPT_ADMIN_KEY", "").strip()
    ws = (os.environ.get("CHATGPT_WORKSPACE_ID", "") or str(cfg.get("workspace_id", ""))).strip()
    fuente = {"estado": "ok", "mensaje": "", "avisos": []}
    almacen = leer_json(ARCHIVO_USO, {})
    if not isinstance(almacen, dict):
        almacen = {}
    usuarios = []

    if not clave or not ws:
        fuente.update(estado="sin_configurar",
                      mensaje="Faltan CHATGPT_ADMIN_KEY y CHATGPT_WORKSPACE_ID en /etc/dashboard-ia.env.")
    else:
        try:
            usuarios = chatgpt_usuarios(clave, ws)
            registrar_altas_bajas(usuarios)
        except ErrorAPI as e:
            fuente.update(estado="error", mensaje="No se pudo leer la lista de miembros: %s" % e)
            # Se usa la última lista guardada para no dar a todo el mundo por baja.
            previa = leer_json(ARCHIVO_MIEMBROS, {})
            if isinstance(previa, dict):
                usuarios = [{"email": e2, "name": n, "role": "member", "seat_type": ""}
                            for e2, n in previa.items()]
        except OSError as e:
            fuente["avisos"].append("No se pudieron guardar las altas y bajas: %s" % e)
        if fuente["estado"] == "ok":
            try:
                pendiente = leer_json(ARCHIVO_RELLENO, {}).get("v") != VERSION_ALMACEN
                registros, completo = chatgpt_uso(clave, ws, fuente["avisos"], pendiente)
                fusionar(almacen, registros, cfg.get("retencion_dias", 730))
                escribir_json(ARCHIVO_USO, almacen)
                if pendiente and completo:
                    escribir_json(ARCHIVO_RELLENO, {"hecho": date.today().isoformat(), "v": VERSION_ALMACEN})
            except ErrorAPI as e:
                fuente["avisos"].append("Uso no actualizado: %s" % e)
            except OSError as e:
                fuente["avisos"].append("No se pudo guardar el uso en disco: %s" % e)

    tablas = construir(cfg, usuarios, almacen)
    tablas["limites"] = {"estado": "sin_datos", "mensaje": "", "personas": [], "historial": []}
    if fuente["estado"] == "ok":
        try:
            limites = leer_limites(clave, ws, usuarios)
            if limites["estado"] == "ok":
                seguir_limites(limites)
            tablas["limites"] = limites
        except OSError as e:
            fuente["avisos"].append("No se pudo guardar el historial de límites: %s" % e)
        except Exception as e:  # la pestaña de límites no debe tumbar el resto del panel
            tablas["limites"] = {"estado": "error", "mensaje": repr(e), "personas": [], "historial": []}
    tablas["grupos"] = {"estado": "sin_datos", "mensaje": "", "grupos": [], "fallos": 0}
    if fuente["estado"] == "ok":
        try:
            tablas["grupos"] = leer_grupos(clave, ws, usuarios)
        except Exception as e:  # tampoco la de grupos
            tablas["grupos"] = {"estado": "error", "mensaje": repr(e), "grupos": [], "fallos": 0}
    if fuente["estado"] == "ok":
        try:
            guardar_foto_diaria(resumen_puestos(tablas, int(cfg.get("dias_activo", 7))))
        except OSError as e:
            fuente["avisos"].append("No se pudo guardar la foto diaria: %s" % e)
    eventos = leer_eventos()
    anadir_bajas(tablas, eventos, cfg)
    return fuente, tablas, historico_para_json(leer_historico()), eventos[-500:]


# ------------------------------------------------------------------------ demo

def datos_demo(cfg):
    """Datos simulados con el mismo formato que devuelve la API. No toca el disco."""
    hoy = hoy_utc()
    dominios = ["alfa.demo", "alfa.demo", "alfa.demo", "beta.demo", "beta.demo", "gamma.demo", "delta.demo"]
    usuarios = [{
        "object": "workspace.user", "id": "user-demo%02d" % i,
        "email": "usuario%02d@%s" % (i, dominios[i % len(dominios)]), "name": "Usuario %02d" % i,
        "role": "owner" if i <= 2 else "member", "seat_type": "chatgpt", "is_scim_managed": i % 4 == 0,
    } for i in range(1, 33)]
    antiguos = [{"id": "user-baja%d" % i, "email": "antiguo%d@beta.demo" % i} for i in (1, 2)]

    modelos = [("gpt-6-sol", "standard"), ("gpt-6-luna", "fast"), ("gpt-5.6-terra", "standard"),
               ("gpt-image-2", "standard"), ("codex-auto-review", "standard"), ("other", "")]
    clientes = ["CODEX_WORK_WEB", "CHAT_WEB", "CODEX_DESKTOP_APP", "CHAT_IOS", "CODEX_WORK_DESKTOP", "DOTS"]
    registros = []
    for i, u in enumerate(usuarios + antiguos, start=1):
        baja = u["id"].startswith("user-baja")
        intensidad = 0 if i % 9 == 0 else (3 if i % 3 == 0 else (2 if i % 4 == 0 else 1))
        for d in range(119, -1, -1):
            dia = hoy - timedelta(days=d)
            semilla = (i * 31 + d * 17) % 23
            mensajes = 0
            if intensidad and dia.weekday() < 5 and semilla % (5 - intensidad) == 0:
                mensajes = int((4 + semilla * intensidad) * (1.5 - d / 180))
            if (baja and d < 40) or (i % 7 == 0 and d < 14) or (i > 26 and d > 60):
                mensajes = 0
            if not mensajes:
                continue
            chats = (mensajes + 5) // 6
            entrada, cache, salida = mensajes * 700, mensajes * 260, mensajes * 420
            total = entrada + cache + salida
            inicio = int(datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc).timestamp())
            c1, c2 = clientes[i % 2], clientes[(i + d) % len(clientes)]
            (m1, v1), (m2, v2) = modelos[(i + d // 9) % 3], modelos[(i * 3 + d) % len(modelos)]
            precio = {"gpt-6-sol": 9.0, "gpt-6-luna": 2.5, "gpt-5.6-terra": 5.0, "gpt-image-2": 14.0,
                      "codex-auto-review": 6.0, "other": 1.5}   # USD por millón de tokens, inventado
            coste1 = round(total * 7 // 10 * precio[m1] / 1e6, 4)
            coste2 = round((total - total * 7 // 10) * precio[m2] / 1e6, 4)
            parte = mensajes * 2 // 3
            registros.append({
                "start_time": inicio, "end_time": inicio + 86400, "user_id": u["id"],
                "actor": {"type": "ACCOUNT_USER", "user_id": u["id"], "email": u["email"]},
                "totals": {"chats": chats, "messages": mensajes, "credits": None, "cost_usd": coste1 + coste2,
                           "uncached_text_input_tokens": entrada, "cached_text_input_tokens": cache,
                           "text_output_tokens": salida, "text_total_tokens": total},
                "clients": [{"client_id": c1, "chats": chats, "messages": parte, "credits": None,
                             "cost_usd": coste1},
                            {"client_id": c2, "chats": 1, "messages": mensajes - parte, "credits": None,
                             "cost_usd": coste2}],
                "models": [{"model": m1, "speed": v1, "credits": None, "cost_usd": coste1,
                            "uncached_text_input_tokens": entrada * 7 // 10, "cached_text_input_tokens": cache * 7 // 10,
                            "text_output_tokens": salida * 7 // 10, "text_total_tokens": total * 7 // 10},
                           {"model": m2, "speed": v2, "credits": None, "cost_usd": coste2,
                            "uncached_text_input_tokens": entrada - entrada * 7 // 10,
                            "cached_text_input_tokens": cache - cache * 7 // 10,
                            "text_output_tokens": salida - salida * 7 // 10,
                            "text_total_tokens": total - total * 7 // 10}],
            })
    tablas_cfg = dict(cfg, empresas={"alfa.demo": "Alfa Cerámica", "beta.demo": "Beta Logística",
                                     "gamma.demo": "Gamma Servicios", "delta.demo": "Delta"})
    tablas = construir(tablas_cfg, usuarios, fusionar({}, registros, 730))
    def regla(importe, expira=None, origen=None):
        r = {"tipo": "limited", "importe": importe, "unidad": "usd", "expira": expira, "creditos": importe * 25}
        if origen:
            r["origen"] = origen
        return r
    fin_periodo = (hoy + timedelta(days=21)).isoformat() + "T00:00:00Z"
    fin_pasado = (hoy - timedelta(days=24)).isoformat() + "T00:00:00Z"
    personas_lim = []
    for i, u in enumerate(usuarios, start=1):
        propio = regla(150, fin_periodo) if i in (3, 12, 24) else regla(300) if i in (6, 30) else None
        usado = round(((i * 37) % 53) * (2.6 if propio else 0.9), 2)
        personas_lim.append({"email": u["email"], "id": u["id"], "propio": propio,
                             "efectivo": dict(propio, origen="individual_override") if propio else regla(50, None, "workspace_default"),
                             "heredado": regla(50, None, "workspace_default") if propio else None,
                             "usado": usado, "unidad": "usd", "usado_creditos": round(usado * 25),
                             "desde": (date.today() - timedelta(days=(i * 3) % 19)).isoformat() if propio and i != 6 else None})
    tablas["limites"] = {
        "estado": "ok", "mensaje": "", "unidad": "usd", "general": regla(50), "personas": personas_lim, "fallos": 0,
        "inicio": (date.today() - timedelta(days=40)).isoformat(),
        "historial": [dict({"fecha": (date.today() - timedelta(days=d)).isoformat(), "cambio": t, "email": e,
                            "limite": l, "general": regla(50)}, **({"anterior": a} if a else {}))
                      for d, t, e, l, a in ((38, "alta", usuarios[11]["email"], regla(100, fin_pasado), None),
                                            (36, "alta", usuarios[7]["email"], regla(200), None),
                                            (30, "alta", usuarios[4]["email"], regla(150, fin_pasado), None),
                                            (24, "fin", usuarios[11]["email"], None, regla(100, fin_pasado)),
                                            (24, "fin", usuarios[4]["email"], None, regla(150, fin_pasado)),
                                            (15, "alta", usuarios[11]["email"], regla(150, fin_periodo), None),
                                            (9, "alta", usuarios[23]["email"], regla(150, fin_periodo), None),
                                            (6, "cambio", usuarios[29]["email"], regla(300), regla(120)),
                                            (2, "fin", usuarios[7]["email"], None, regla(200)))]}
    correos = [u["email"] for u in usuarios]
    tablas["grupos"] = {"estado": "ok", "mensaje": "", "fallos": 0, "grupos": [
        {"nombre": "Comercial", "descripcion": "Equipo de ventas", "miembros": correos[0:9]},
        {"nombre": "Dirección", "descripcion": "", "miembros": correos[7:11] + correos[29:30]},
        {"nombre": "Diseño", "descripcion": "", "miembros": correos[11:17]},
        {"nombre": "Informática", "descripcion": "Sistemas y desarrollo", "miembros": correos[17:25] + correos[2:3]},
        {"nombre": "Pruebas", "descripcion": "Grupo vacío", "miembros": []}]}
    actual = resumen_puestos(tablas, int(cfg.get("dias_activo", 7)))
    historico = [{"fecha": (date.today() - timedelta(days=d)).isoformat(),
                  "total": actual["total"] - d // 14, "activos": max(0, actual["activos"] - d // 10),
                  "sin_uso": actual["sin_uso"] + d // 25} for d in range(89, -1, -1)]
    eventos = [{"fecha": (date.today() - timedelta(days=d)).isoformat(), "tipo": t, "email": e, "nombre": n}
               for d, t, e, n in ((41, "baja", "antiguo1@beta.demo", "Antiguo 1"),
                                  (40, "baja", "antiguo2@beta.demo", "Antiguo 2"),
                                  (33, "baja", "antiguo3@alfa.demo", "Antiguo 3"),
                                  (27, "alta", "usuario31@beta.demo", "Usuario 31"),
                                  (12, "alta", "usuario32@gamma.demo", "Usuario 32"))]
    anadir_bajas(tablas, eventos, tablas_cfg)
    return {"estado": "ok", "mensaje": "", "avisos": []}, tablas, historico, eventos


# ---------------------------------------------------------------------- estado

class Estado:
    def __init__(self, cfg, demo):
        self.cfg = cfg
        self.demo = demo
        self.cuerpo = b"{}"
        self.comprimido = gzip.compress(self.cuerpo)
        self.cerrojo = threading.Lock()
        self.en_curso = threading.Lock()

    def refrescar(self):
        with self.en_curso:
            cfg = self.cfg
            if self.demo:
                cfg = dict(cfg, moneda="EUR", precio_puesto={"chatgpt": 20}, compromiso_anual=30000,
                           contrato_inicio=(hoy_utc() - timedelta(days=70)).isoformat(),
                           usd_a_moneda=0.9, precio_credito=0)
                fuente, tablas, historico, eventos = datos_demo(cfg)
            else:
                fuente, tablas, historico, eventos = leer_real(cfg)
            datos = {
                "generado": datetime.now().astimezone().isoformat(timespec="seconds"),
                "hoy": hoy_utc().isoformat(),
                "demo": self.demo,
                "organizacion": cfg.get("organizacion", ""),
                "servidor": cfg.get("nombre_servidor") or socket.gethostname(),
                "refresco_minutos": cfg.get("refresco_minutos", 60),
                "cfg": publico(cfg),
                "fuente": fuente,
                "historico": historico,
                "eventos": eventos,
            }
            datos.update(tablas)
            cuerpo = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            comprimido = gzip.compress(cuerpo, 6)
            with self.cerrojo:
                self.cuerpo, self.comprimido = cuerpo, comprimido
            r = resumen_puestos(tablas, int(cfg.get("dias_activo", 7)))
            con_creditos = sum(1 for f in tablas["uso"] if f[8] is not None)
            con_importe = sum(1 for f in tablas["uso"] if f[9] is not None)
            lim = tablas.get("limites") or {}
            txt_lim = "%s (%s con límite propio, %s fallos)" % (
                lim.get("estado"), sum(1 for p in lim.get("personas", []) if p.get("propio")), lim.get("fallos", 0))
            gr = tablas.get("grupos") or {}
            txt_gr = "%s (%s grupos, %s fallos)%s" % (gr.get("estado"), len(gr.get("grupos", [])), gr.get("fallos", 0),
                                                    " " + gr["mensaje"] if gr.get("mensaje") else "")
            log("Datos actualizados - ChatGPT: %s (%s puestos, %s activos, %s sin uso) - %s días de uso, %s filas (%s con créditos, %s con importe) - límites: %s - grupos: %s%s" % (
                fuente["estado"], r["total"], r["activos"], r["sin_uso"], len(tablas["dias"]),
                len(tablas["uso"]), con_creditos, con_importe, txt_lim, txt_gr, " - AVISOS: " + " | ".join(fuente["avisos"]) if fuente["avisos"] else ""))
            if fuente["estado"] != "ok":
                log("AVISO:", fuente["mensaje"])

    def bucle(self):
        while True:
            time.sleep(max(5, int(self.cfg.get("refresco_minutos", 60))) * 60)
            try:
                self.refrescar()
            except Exception as e:  # el servicio no debe caerse por un fallo de lectura
                log("ERROR al refrescar:", repr(e))

    def json(self, gz):
        with self.cerrojo:
            return self.comprimido if gz else self.cuerpo


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

        def datos(self):
            gz = "gzip" in self.headers.get("Accept-Encoding", "")
            self.responder(200, estado.json(gz), "application/json; charset=utf-8",
                           {"Content-Encoding": "gzip"} if gz else None)

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
                return self.datos()
            if ruta == "/api/refrescar":
                try:
                    estado.refrescar()
                except Exception as e:
                    log("ERROR al refrescar:", repr(e))
                return self.datos()
            return self.responder(404, b"No encontrado", "text/plain; charset=utf-8")

        do_HEAD = do_GET

    return Manejador


def main():
    cfg = cargar_config()
    parser = argparse.ArgumentParser(description="Dashboard de ChatGPT Enterprise")
    parser.add_argument("--puerto", type=int, default=int(cfg.get("puerto", 8091)))
    parser.add_argument("--escucha", default=cfg.get("escucha", "0.0.0.0"))
    parser.add_argument("--demo", action="store_true", help="datos simulados")
    args = parser.parse_args()

    estado = Estado(cfg, args.demo or bool(cfg.get("demo")))
    if not estado.demo:
        DATA.mkdir(exist_ok=True)
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
