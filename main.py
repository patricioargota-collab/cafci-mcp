"""
cafci-mcp — Conector MCP de rendimientos de FCI (familias Fima y Pionero)
Fuentes:
  - ArgentinaDatos (https://api.argentinadatos.com/v1/finanzas/fci/...) — datos diarios de CAFCI en JSON, sin auth. Fuente de ingesta.
  - CAFCI directo (https://api.cafci.org.ar) — ficha y rendimiento oficial por fondo/clase ID. Herramientas de contraste.
Patrón: FastMCP + SQLite + Railway (volumen persistente en /data).
"""

import os
import sqlite3
import datetime as dt
from contextlib import closing

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------- config

PORT = int(os.environ.get("PORT", 8000))
DB_PATH = os.environ.get("DB_PATH", "/data/fci.db")
# familias a indexar: match por substring (case-insensitive) sobre el nombre del fondo
FAMILIAS = [f.strip().lower() for f in os.environ.get("FCI_FAMILIAS", "fima,pionero").split(",") if f.strip()]

AD_BASE = "https://api.argentinadatos.com/v1/finanzas/fci"
CATEGORIAS = ["mercadoDinero", "rentaFija", "rentaVariable", "rentaMixta", "otros"]
CAFCI_BASE = "https://api.cafci.org.ar"

mcp = FastMCP("cafci", host="0.0.0.0", port=PORT)

# ---------------------------------------------------------------- db

def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS serie (
            fondo      TEXT NOT NULL,
            categoria  TEXT NOT NULL,
            fecha      TEXT NOT NULL,      -- YYYY-MM-DD
            vcp        REAL,
            ccp        REAL,
            patrimonio REAL,
            horizonte  TEXT,
            PRIMARY KEY (fondo, fecha)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cafci_ids (
            fondo    TEXT PRIMARY KEY,     -- nombre tal como figura en serie.fondo
            fondo_id INTEGER NOT NULL,
            clase_id INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_serie_fecha ON serie(fecha)")
    return conn


def familia_de(nombre: str) -> str | None:
    n = nombre.lower()
    for fam in FAMILIAS:
        if fam in n:
            return fam
    return None


def norm_fecha(fecha: str) -> str:
    return fecha.replace("/", "-")[:10]


# ---------------------------------------------------------------- ingesta

def _fetch_categoria(cliente: httpx.Client, categoria: str, fecha: str) -> list[dict]:
    """fecha: 'ultimo', 'penultimo' o YYYY/MM/DD"""
    r = cliente.get(f"{AD_BASE}/{categoria}/{fecha}", timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json() or []


def _insertar_dia(conn: sqlite3.Connection, categoria: str, filas: list[dict]) -> int:
    nuevos = 0
    for f in filas:
        nombre = (f.get("fondo") or "").strip()
        if not nombre or familia_de(nombre) is None:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO serie (fondo, categoria, fecha, vcp, ccp, patrimonio, horizonte)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (nombre, categoria, norm_fecha(f.get("fecha") or ""),
             f.get("vcp"), f.get("ccp"), f.get("patrimonio"), f.get("horizonte")),
        )
        nuevos += cur.rowcount
    conn.commit()  # commit por categoría: un timeout no pierde lo ya cargado
    return nuevos


@mcp.tool()
def fci_actualizar() -> str:
    """Descarga el último dato diario disponible (fuente CAFCI vía ArgentinaDatos) para las
    cinco categorías de FCI y agrega a la serie local los fondos de las familias configuradas
    (por defecto Fima y Pionero). Idempotente: no duplica días ya cargados. Ejecutar antes de
    cualquier análisis para asegurar datos frescos."""
    resumen = []
    with closing(db()) as conn, httpx.Client() as cliente:
        for cat in CATEGORIAS:
            try:
                filas = _fetch_categoria(cliente, cat, "ultimo")
                n = _insertar_dia(conn, cat, filas)
                resumen.append(f"{cat}: +{n} registros nuevos")
            except Exception as e:  # una categoría caída no frena las demás
                resumen.append(f"{cat}: ERROR {e}")
    return "Actualización completada.\n" + "\n".join(resumen)


@mcp.tool()
def fci_backfill(desde: str, hasta: str, max_dias: int = 10) -> str:
    """Carga histórica de la serie diaria entre dos fechas (YYYY-MM-DD), en tramos.
    Procesa como máximo max_dias días hábiles por llamada (default 10) con commit por día,
    para que un gateway timeout de Railway no pierda el progreso (lección comarb/tfaba).
    Si el rango quedó incompleto, la respuesta indica desde dónde relanzar. Sábados y
    domingos se saltean (no hay datos)."""
    d0 = dt.date.fromisoformat(norm_fecha(desde))
    d1 = dt.date.fromisoformat(norm_fecha(hasta))
    if d0 > d1:
        return "Rango inválido: 'desde' es posterior a 'hasta'."
    procesados, total_nuevos = 0, 0
    dia = d0
    with closing(db()) as conn, httpx.Client() as cliente:
        while dia <= d1 and procesados < max_dias:
            if dia.weekday() < 5:  # lunes a viernes
                fecha_api = dia.strftime("%Y/%m/%d")
                for cat in CATEGORIAS:
                    try:
                        total_nuevos += _insertar_dia(conn, cat, _fetch_categoria(cliente, cat, fecha_api))
                    except Exception:
                        pass  # día/categoría sin datos: seguir
                procesados += 1
            dia += dt.timedelta(days=1)
    if dia <= d1:
        return (f"Tramo procesado: {procesados} días hábiles, +{total_nuevos} registros. "
                f"INCOMPLETO: relanzar con desde={dia.isoformat()} hasta={d1.isoformat()}.")
    return f"Backfill completo: {procesados} días hábiles, +{total_nuevos} registros."


# ---------------------------------------------------------------- consulta

def _vcp_en(conn: sqlite3.Connection, fondo: str, fecha: str, hacia_atras: bool = True) -> sqlite3.Row | None:
    """VCP en la fecha dada o el día con dato más cercano (hacia atrás por default)."""
    op, orden = ("<=", "DESC") if hacia_atras else (">=", "ASC")
    return conn.execute(
        f"SELECT fecha, vcp FROM serie WHERE fondo = ? AND vcp IS NOT NULL AND fecha {op} ? ORDER BY fecha {orden} LIMIT 1",
        (fondo, fecha),
    ).fetchone()


def _resolver_fondo(conn: sqlite3.Connection, nombre: str) -> list[str]:
    return [r["fondo"] for r in conn.execute(
        "SELECT DISTINCT fondo FROM serie WHERE lower(fondo) LIKE ? ORDER BY fondo",
        (f"%{nombre.lower()}%",),
    ).fetchall()]


@mcp.tool()
def fci_listar_fondos(familia: str = "") -> str:
    """Lista los fondos indexados con su categoría, familia, cantidad de días cargados y
    fecha del último dato. Filtro opcional por familia ('fima' o 'pionero')."""
    with closing(db()) as conn:
        filas = conn.execute(
            """SELECT fondo, categoria, COUNT(*) dias, MIN(fecha) primera, MAX(fecha) ultima
               FROM serie GROUP BY fondo ORDER BY categoria, fondo"""
        ).fetchall()
    out = []
    for r in filas:
        fam = familia_de(r["fondo"]) or "?"
        if familia and fam != familia.lower():
            continue
        out.append(f"[{fam}] {r['fondo']} | {r['categoria']} | {r['dias']} días ({r['primera']} → {r['ultima']})")
    return "\n".join(out) if out else "Sin fondos indexados. Ejecutar fci_actualizar o fci_backfill."


@mcp.tool()
def fci_rendimiento(fondo: str, desde: str = "", hasta: str = "", dias: int = 0) -> str:
    """Rendimiento de un fondo entre dos fechas (YYYY-MM-DD), calculado sobre la serie local
    de VCP (dato oficial CAFCI). Alternativa: pasar dias=30/90/365 para la ventana hacia atrás
    desde el último dato. El nombre del fondo admite coincidencia parcial (ej. 'Fima Premium').
    Devuelve rendimiento directo del período y TNA/TEA equivalentes."""
    with closing(db()) as conn:
        candidatos = _resolver_fondo(conn, fondo)
        if not candidatos:
            return f"No hay fondos que coincidan con '{fondo}'. Ver fci_listar_fondos."
        if len(candidatos) > 1 and fondo.lower() not in [c.lower() for c in candidatos]:
            return "Coincidencias múltiples, precisar:\n" + "\n".join(candidatos)
        nombre = candidatos[0] if len(candidatos) == 1 else fondo

        if dias:
            ult = conn.execute("SELECT MAX(fecha) f FROM serie WHERE fondo = ?", (nombre,)).fetchone()["f"]
            hasta = ult
            desde = (dt.date.fromisoformat(ult) - dt.timedelta(days=dias)).isoformat()
        if not desde or not hasta:
            return "Indicar desde/hasta (YYYY-MM-DD) o dias=N."

        vi = _vcp_en(conn, nombre, norm_fecha(desde), hacia_atras=False)
        vf = _vcp_en(conn, nombre, norm_fecha(hasta), hacia_atras=True)
    if not vi or not vf or vi["fecha"] >= vf["fecha"]:
        return f"Serie insuficiente para {nombre} en ese rango. Ver fci_estado_indice / correr fci_backfill."
    n = (dt.date.fromisoformat(vf["fecha"]) - dt.date.fromisoformat(vi["fecha"])).days
    r = vf["vcp"] / vi["vcp"] - 1
    tna = r * 365 / n * 100
    tea = ((1 + r) ** (365 / n) - 1) * 100
    return (f"{nombre}\nPeríodo real con datos: {vi['fecha']} → {vf['fecha']} ({n} días)\n"
            f"Rendimiento directo: {r*100:.2f}%\nTNA equivalente: {tna:.2f}% | TEA equivalente: {tea:.2f}%\n"
            f"Fuente: serie VCP oficial CAFCI (vía ArgentinaDatos), base local.")


@mcp.tool()
def fci_comparar(dias: int = 30, familia: str = "", categoria: str = "") -> str:
    """Ranking de todos los fondos indexados por rendimiento en una ventana de N días
    (default 30), de mayor a menor. Filtros opcionales: familia ('fima'/'pionero') y
    categoria ('mercadoDinero','rentaFija','rentaVariable','rentaMixta','otros').
    Es la herramienta central para responder 'qué fondo conviene'."""
    resultados = []
    with closing(db()) as conn:
        fondos = conn.execute("SELECT DISTINCT fondo, categoria FROM serie").fetchall()
        for f in fondos:
            fam = familia_de(f["fondo"]) or "?"
            if familia and fam != familia.lower():
                continue
            if categoria and f["categoria"] != categoria:
                continue
            ult = conn.execute("SELECT MAX(fecha) f FROM serie WHERE fondo = ?", (f["fondo"],)).fetchone()["f"]
            d0 = (dt.date.fromisoformat(ult) - dt.timedelta(days=dias)).isoformat()
            vi = _vcp_en(conn, f["fondo"], d0, hacia_atras=False)
            vf = _vcp_en(conn, f["fondo"], ult, hacia_atras=True)
            if vi and vf and vi["fecha"] < vf["fecha"] and vi["vcp"]:
                n = (dt.date.fromisoformat(vf["fecha"]) - dt.date.fromisoformat(vi["fecha"])).days
                r = (vf["vcp"] / vi["vcp"] - 1) * 100
                resultados.append((r, f"[{fam}] {f['fondo']} ({f['categoria']}): {r:.2f}% en {n} días (al {vf['fecha']})"))
    if not resultados:
        return "Sin datos suficientes. Correr fci_actualizar/fci_backfill primero."
    resultados.sort(reverse=True)
    return f"Ranking por rendimiento a {dias} días:\n" + "\n".join(x[1] for x in resultados)


@mcp.tool()
def fci_serie(fondo: str, desde: str, hasta: str) -> str:
    """Serie diaria de VCP y patrimonio de un fondo entre dos fechas (YYYY-MM-DD),
    para análisis de evolución o gráficos. Nombre con coincidencia parcial."""
    with closing(db()) as conn:
        candidatos = _resolver_fondo(conn, fondo)
        if len(candidatos) != 1:
            return ("Sin coincidencias." if not candidatos else "Precisar entre:\n" + "\n".join(candidatos))
        filas = conn.execute(
            "SELECT fecha, vcp, patrimonio FROM serie WHERE fondo = ? AND fecha BETWEEN ? AND ? ORDER BY fecha",
            (candidatos[0], norm_fecha(desde), norm_fecha(hasta)),
        ).fetchall()
    if not filas:
        return "Sin datos en ese rango."
    lineas = [f"{r['fecha']}  vcp={r['vcp']}  patrimonio={r['patrimonio']}" for r in filas]
    return f"{candidatos[0]} ({len(filas)} días):\n" + "\n".join(lineas)


@mcp.tool()
def fci_estado_indice() -> str:
    """Estadísticas del índice local: total de registros, fondos por familia y categoría,
    rango de fechas cubierto y fecha del último dato. Usar para diagnosticar huecos."""
    with closing(db()) as conn:
        tot = conn.execute("SELECT COUNT(*) c FROM serie").fetchone()["c"]
        rango = conn.execute("SELECT MIN(fecha) a, MAX(fecha) b FROM serie").fetchone()
        por_cat = conn.execute(
            "SELECT categoria, COUNT(DISTINCT fondo) f, COUNT(*) r FROM serie GROUP BY categoria"
        ).fetchall()
        ids = conn.execute("SELECT COUNT(*) c FROM cafci_ids").fetchone()["c"]
    if not tot:
        return "Índice vacío. Correr fci_actualizar."
    cats = "\n".join(f"  {r['categoria']}: {r['f']} fondos, {r['r']} registros" for r in por_cat)
    return (f"Registros: {tot}\nRango: {rango['a']} → {rango['b']}\nFamilias configuradas: {', '.join(FAMILIAS)}\n"
            f"Por categoría:\n{cats}\nMapeos CAFCI directos cargados: {ids}")


# ------------------------------------------------- CAFCI directo (contraste)

@mcp.tool()
def fci_registrar_cafci_id(fondo: str, fondo_id: int, clase_id: int) -> str:
    """Mapea un fondo local a sus IDs oficiales de CAFCI (fondo_id;clase_id, visibles en la URL
    de la ficha: cafci.org.ar/ficha-fondo.html?q=FONDO;CLASE). Habilita las herramientas de
    contraste directo contra la API oficial."""
    with closing(db()) as conn:
        conn.execute("INSERT OR REPLACE INTO cafci_ids (fondo, fondo_id, clase_id) VALUES (?, ?, ?)",
                     (fondo, fondo_id, clase_id))
        conn.commit()
    return f"Mapeado: {fondo} → CAFCI {fondo_id};{clase_id}"


@mcp.tool()
def fci_cafci_rendimiento(fondo: str, desde: str, hasta: str) -> str:
    """Rendimiento OFICIAL entre dos fechas (YYYY-MM-DD) consultado en vivo a la API de CAFCI
    (api.cafci.org.ar). Requiere que el fondo tenga IDs mapeados con fci_registrar_cafci_id.
    Usar para validar cifras de la serie local o cuando se necesita el dato canónico."""
    with closing(db()) as conn:
        row = conn.execute("SELECT fondo_id, clase_id FROM cafci_ids WHERE lower(fondo) LIKE ?",
                           (f"%{fondo.lower()}%",)).fetchone()
    if not row:
        return f"'{fondo}' no tiene IDs CAFCI mapeados. Usar fci_registrar_cafci_id."
    url = f"{CAFCI_BASE}/fondo/{row['fondo_id']}/clase/{row['clase_id']}/rendimiento/{norm_fecha(desde)}/{norm_fecha(hasta)}"
    with httpx.Client() as cliente:
        r = cliente.get(url, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", {})
    return f"Rendimiento oficial CAFCI {desde} → {hasta}: {data.get('rendimiento')}%\n(fondo {row['fondo_id']};{row['clase_id']})"


# ---------------------------------------------------------------- main

def main():
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    mcp.settings.streamable_http_path = "/mcp"
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
