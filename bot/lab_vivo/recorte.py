"""La prueba del recorte (docs/07, 4.4, 6.4 y A8 de 8.1): ninguna familia puede mirar datos posteriores a su decisión.

Una señal con t (la decisión) solo puede usar lo que se conoce en t: las velas de 1 min que abren antes de t (cerradas
en t), las barras con t_cierre ≤ t y el funding, el miedo y codicia y los cierres de bolsa con sello ≤ t. Es justo lo
que ve Mercado.recortado(t + 1 ms): las velas y las barras, cerradas en t; lo demás, con sello ≤ t.

Dos comprobaciones, con la misma ventana de datos (el mismo calentamiento) que la pasada entera:
1. **Corte en cada señal** (todas o una muestra fija de POR_SENAL por tramo): las señales con t' ≤ tᵢ, calculadas solo
   con los datos hasta tᵢ, tienen que salir idénticas a las calculadas con todos. Caza CON CERTEZA cualquier mirada al
   futuro de 1 min o más en la señal cortada: una familia que decide en t con la vela que abre en t no puede dar esa
   señal sin esa vela. (La prueba de antes solo comparaba las señales con t' < T, y una mirada de 1 min no se veía nunca.)
2. **Cortes al azar** (semilla fija): en cada T, las señales con t' ≤ T, idénticas con los datos hasta T y con todos.
   Caza también lo que una mirada al futuro QUITA (una señal que no sale porque se ve lo que viene) cuando el corte
   cae en ella.
Si un tramo no tiene ninguna señal, la prueba no prueba nada: se dice («tramos sin señales»)."""
import time

import numpy as np

MIN, DAY = 60_000, 86_400_000
POR_SENAL = 60          # cortes por señal en cada tramo de la admisión (A8): todas las señales si hay menos
PRESUPUESTO_S = 240.0   # tope de tiempo de los cortes por señal de una variante (una familia lenta); como mínimo MINIMO
MINIMO = 10


def _corte(fam, mv, p, a: int, b: int, T: int):
    """Señales de [a, b) con los datos hasta T (T = t + 1 ms: lo que se conoce en t)."""
    return fam.senales(mv.recortado(T), p, a, b)


def comprobar(fam, tramos, p: dict, rng, cortes_azar: int, por_senal: int = POR_SENAL,
              presupuesto_s: float = PRESUPUESTO_S) -> dict:
    """tramos: [(mv, a, b)] = la ventana de datos (con su calentamiento) y el tramo de señales. Devuelve {malos_senal,
    cortes_senal, malos_azar, cortes_azar, ejemplos (hasta 5 t de señales que cambian), tramos_vacios, segundos}."""
    t0 = time.time()
    out = {"malos_senal": 0, "cortes_senal": 0, "malos_azar": 0, "cortes_azar": 0, "ejemplos": [], "tramos_vacios": 0,
           "senales": 0}
    for mv, a, b in tramos:
        base = fam.senales(mv, p, a, b)
        out["senales"] += len(base)
        if not len(base):
            out["tramos_vacios"] += 1
        ts = np.unique(base.t) if len(base) else np.zeros(0, np.int64)
        if len(ts) > por_senal:
            ts = rng.choice(ts, por_senal, replace=False)
        hechos = 0
        for t in ts:
            if hechos >= MINIMO and time.time() - t0 > presupuesto_s:
                break
            T = int(t) + 1
            hechos += 1
            out["cortes_senal"] += 1
            if not base.entre(a, T).iguales(_corte(fam, mv, p, a, b, T).entre(a, T)):
                out["malos_senal"] += 1
                if len(out["ejemplos"]) < 5:
                    out["ejemplos"].append(int(t))
        for T in rng.integers(a + DAY, b, cortes_azar):
            T = int(T) // MIN * MIN + 1
            out["cortes_azar"] += 1
            if not base.entre(a, T).iguales(_corte(fam, mv, p, a, b, T).entre(a, T)):
                out["malos_azar"] += 1
    out["segundos"] = round(time.time() - t0, 2)
    return out


def motivos(r: dict) -> list[str]:
    """Los motivos A8 en español (vacío si pasa)."""
    mot = []
    if r["malos_senal"]:
        ej = ", ".join(_hora(t) for t in r["ejemplos"][:3])
        mot.append(f"A8: prueba del recorte: {r['malos_senal']} de {r['cortes_senal']} señales cambian si se calculan "
                   f"solo con los datos hasta su decisión (mira datos posteriores a la decisión; p. ej., {ej} UTC)")
    if r["malos_azar"]:
        mot.append(f"A8: prueba del recorte: {r['malos_azar']} de {r['cortes_azar']} cortes al azar dan señales distintas "
                   "hasta el corte (mira datos posteriores a la decisión)")
    return mot


def _hora(t: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(t) / 1000, timezone.utc).strftime("%d-%m-%Y %H:%M")
