"""Pruebas previas sobre la misma historia (docs/07, 9.2): cuentan en el número de pruebas acumulado. La siembra se
hace una vez (tabla pruebas_previas) contando estos estudios (cifras del 29-10-2026):

| Estudio | Pruebas | Archivo | Mismo fuera de muestra |
|---|---|---|---|
| Backtest general | 690 | BACKTEST_DIR/resultados.json | en parte (otro diseño, 10 años) |
| Laboratorio de 2.000 | 2.000 (381 efectivas) | laboratorio.json (p_day de cada una en all[].oos) | sí |
| ¿6 diarias? | 432 | controles/seis_diarias.json | sí (corte 13-08-2025) |
| Salidas («estirar el chicle») | 378 | controles/salidas_motor.json | sí |
| Salidas 1:1 | 66 | controles/salidas_1a1.json | sí |
| Objetivos pequeños | 196 | controles/objetivos_pequenos.json | sí |
| Selectivas (Murphy, Fibonacci, Ruptura) | 23 | controles/selectiva/medir_selectiva.json | sí |
| Técnicas del curso | 52 | controles/curso/medir_curso.json | sí |
| Noticia confirmada | 80 | controles/noticia_confirmacion.json | en parte |

Para Benjamini-Hochberg: los 2.000 llevan su p_day (sin él, p = 1) y las demás previas p = 1, una por prueba (lo
prudente: suben m y endurecen el umbral)."""
import json

from .. import config
from . import almacen

PREVIAS = [
    ("backtest_general", 690, None, 0, "resultados.json", "otro diseño, 10 años"),
    ("laboratorio_2000", 2000, 381.0, 1, "laboratorio.json", "p_day de cada una en all[].oos"),
    ("seis_diarias", 432, None, 1, "controles/seis_diarias.json", "corte 13-08-2025"),
    ("salidas_motor", 378, None, 1, "controles/salidas_motor.json", "estirar el chicle"),
    ("salidas_1a1", 66, None, 1, "controles/salidas_1a1.json", ""),
    ("objetivos_pequenos", 196, None, 1, "controles/objetivos_pequenos.json", ""),
    ("selectivas", 23, None, 1, "controles/selectiva/medir_selectiva.json", "Murphy, Fibonacci, Ruptura"),
    ("curso", 52, None, 1, "controles/curso/medir_curso.json", "técnicas del curso del usuario"),
    ("noticia_confirmada", 80, None, 0, "controles/noticia_confirmacion.json", "en parte"),
]
TOTAL = sum(x[1] for x in PREVIAS)   # 3.917


def sembrar(con) -> int:
    """Siembra las previas si faltan (idempotente). Devuelve el N previo."""
    for fuente, n, nef, mismo, archivo, nota in PREVIAS:
        con.execute("INSERT OR IGNORE INTO pruebas_previas(fuente, n, n_efectivo, mismo_fuera, p_conocidos, archivo, nota) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)", (fuente, n, nef, mismo, archivo, nota))
    ya = con.execute("SELECT COUNT(*) FROM previas_p WHERE fuente = 'laboratorio_2000'").fetchone()[0]
    if not ya:
        try:
            d = json.loads((config.BACKTEST_DIR / "laboratorio.json").read_text(encoding="utf-8"))
            ps = [(r.get("oos") or {}).get("p_day") for r in d.get("all") or []]
        except (OSError, ValueError):
            ps = []
        filas = [("laboratorio_2000", k, float(p) if p is not None else 1.0) for k, p in enumerate(ps)]
        con.executemany("INSERT OR IGNORE INTO previas_p(fuente, k, p) VALUES (?, ?, ?)", filas)
        con.execute("UPDATE pruebas_previas SET p_conocidos = ? WHERE fuente = 'laboratorio_2000'",
                    (sum(1 for p in ps if p is not None),))
    return almacen.n_previo(con)


def ps_previas(con) -> list[float]:
    """Los p de todas las previas para BH: los conocidos y p = 1 para el resto."""
    out = [float(r[0]) for r in con.execute("SELECT p FROM previas_p")]
    conocidas = {r[0]: r[1] for r in con.execute("SELECT fuente, COUNT(*) FROM previas_p GROUP BY fuente")}
    for fuente, n in con.execute("SELECT fuente, n FROM pruebas_previas"):
        out += [1.0] * max(0, int(n) - int(conocidas.get(fuente, 0)))
    return out
