"""El N acumulado FUERA de lab_vivo.db (docs/07, 9.1): lab_vivo\\n_acumulado.jsonl, de solo añadir.

Los disparadores de lab_vivo.db impiden borrar pruebas, pero no borrar, mover o rehacer el archivo de la base. Con este
registro aparte, un corredor que arranca con una base que tiene MENOS pruebas que las ya apuntadas (se borró, se cambió
por una copia vieja o BOT_LAB_VIVO_DIR apunta a otra base dentro de la misma carpeta) no mide nada: el número de
pruebas no puede volver a cero a escondidas. Solo una orden explícita del usuario
(python -X utf8 -m bot.lab_vivo heredar-n --nota "…") lo desbloquea, y deja anotado:
- en pruebas_previas, una fila «lab_vivo_anterior:<fecha>» con las pruebas que faltan (cuentan en Benjamini-Hochberg con
  p = 1) y su N efectivo (suma en N_dsr): las previas suman también las del laboratorio vivo anterior;
- en eventos y en este registro.

Cada línea: {ts, base (id de la base), n_total (variantes medidas en esta base), heredado (de bases anteriores),
n_acumulado (= n_total + heredado), n_lab (N efectivo de la parte viva), bh_m, n_dsr, motivo}. Se apunta al arrancar, cada
vez que crece el N y en cada consolidación. Si se borra la carpeta lab_vivo entera, se pierde también: el N de la primera
tanda queda además en docs/07 (20.7)."""
import json
import logging
import os
import uuid

from . import ajustes, almacen

log = logging.getLogger("lab_vivo.registro_n")
FUENTE = "lab_vivo_anterior"


def ruta():
    return ajustes.DIR / "n_acumulado.jsonl"


def leer() -> list[dict]:
    try:
        lineas = ruta().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for x in lineas:
        try:
            d = json.loads(x)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("n_acumulado"), (int, float)):
            out.append(d)
    return out


def maximo() -> dict | None:
    """La línea con más pruebas acumuladas (y, a igualdad, la más reciente)."""
    filas = leer()
    return max(filas, key=lambda d: (d["n_acumulado"], d.get("n_lab") or 0, d.get("ts") or 0)) if filas else None


def id_base(con) -> str:
    """Id de esta base (se crea con ella): distingue en el registro una base rehecha de la de antes."""
    b = almacen.kv_get(con, "id_base")
    if not b:
        b = uuid.uuid4().hex[:12]
        almacen.kv_set(con, "id_base", b)
    return b


def heredado(con) -> dict:
    r = con.execute("SELECT COALESCE(SUM(n), 0), COALESCE(SUM(n_efectivo), 0) FROM pruebas_previas WHERE fuente LIKE ?",
                    (FUENTE + "%",)).fetchone()
    return {"n": int(r[0]), "n_efectivo": float(r[1])}


def acumulado(con) -> dict:
    h = heredado(con)
    n = almacen.n_total(con)
    return {"n_total": n, "heredado": h["n"], "n_acumulado": n + h["n"], "n_efectivo_heredado": h["n_efectivo"]}


def comprobar(con) -> str | None:
    """El motivo para no medir (la base tiene menos pruebas que las ya apuntadas) o None."""
    mx = maximo()
    ac = acumulado(con)
    if mx and ac["n_acumulado"] < int(mx["n_acumulado"]):
        return (f"lab_vivo.db tiene {ac['n_acumulado']} variantes medidas y el registro de solo añadir "
                f"(n_acumulado.jsonl) llegó a {int(mx['n_acumulado'])} el {ajustes.fecha_mad(int(mx.get('ts') or 0))}: "
                "¿se ha borrado o cambiado la base? No se mide nada (el número de pruebas no puede volver a cero). "
                "Si es a propósito, el usuario lo acepta con «python -X utf8 -m bot.lab_vivo heredar-n --nota "
                "\"por qué\"»: lo que falta pasa a las previas.")
    return None


def apuntar(con, motivo: str, extra: dict | None = None, forzar: bool = False) -> dict | None:
    """Añade una línea si el N acumulado cambió desde la última de esta base (o si forzar). Nunca con la base por
    debajo del registro (bloqueada: lo que dijera no vale)."""
    if comprobar(con):
        return None
    ac = acumulado(con)
    base = id_base(con)
    ult = next((d for d in reversed(leer()) if d.get("base") == base), None)
    if not forzar and ult and ult.get("n_acumulado") == ac["n_acumulado"]:
        return None
    d = {"ts": ajustes.ahora_ms(), "base": base, **ac, "motivo": motivo, **(extra or {})}
    p = ruta()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:   # solo se añade (una línea, con su salto)
        f.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return d


def heredar(con, nota: str, por: str = "usuario", n_lab_actual: float = 0.0) -> dict | None:
    """La orden del usuario: lo que falta respecto al registro pasa a pruebas_previas («lab_vivo_anterior:<fecha>»),
    con el N efectivo que falta: el de la base de antes (su parte viva y lo que ya heredaba; si no llegó a apuntarse,
    una prueba efectiva por variante, lo prudente) menos lo que esta base ya cuenta (n_lab_actual y su herencia).
    Devuelve la fila o None si no falta nada."""
    mx = maximo()
    ac = acumulado(con)
    if not mx or ac["n_acumulado"] >= int(mx["n_acumulado"]):
        return None
    falta = int(mx["n_acumulado"]) - ac["n_acumulado"]
    previo = float(mx["n_lab"]) if mx.get("n_lab") is not None else float(mx["n_acumulado"])
    nef = max(float(falta) if mx.get("n_lab") is None else 0.0,
              previo + float(mx.get("n_efectivo_heredado") or 0.0) - float(n_lab_actual) - ac["n_efectivo_heredado"])
    fuente = f"{FUENTE}:{ajustes.dia_mad(ajustes.ahora_ms())}:{uuid.uuid4().hex[:6]}"
    nota_txt = (f"Heredado por orden del {por} ({ajustes.fecha_mad(ajustes.ahora_ms())}): {nota.strip() or 'sin nota'}. "
                f"La base tenía {ac['n_acumulado']} y el registro, {int(mx['n_acumulado'])}.")
    con.execute("INSERT INTO pruebas_previas(fuente, n, n_efectivo, mismo_fuera, p_conocidos, archivo, nota) "
                "VALUES (?, ?, ?, 1, 0, ?, ?)", (fuente, falta, round(nef, 3), "n_acumulado.jsonl", nota_txt))
    almacen.evento(con, "herencia_n", nota_txt, None, {"fuente": fuente, "n": falta, "n_efectivo": nef, "nota": nota,
                                                       "por": por})
    apuntar(con, "herencia por orden del usuario", {"nota": nota}, forzar=True)
    log.warning(nota_txt)
    return {"fuente": fuente, "n": falta, "n_efectivo": round(nef, 3), "nota": nota_txt}
