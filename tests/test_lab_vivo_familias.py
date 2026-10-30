"""Conformidad de TODAS las familias registradas (docs/07, 6.4), con velas sintéticas, sin red ni disco:
1. Esquema: arrays de la misma longitud; t múltiplo de 60.000 y ordenado; dir ∈ {−1, +1}; moneda en rango; stop_pct NaN
   o > 0.
2. Recorte (recorte.py, la misma prueba que A8): sobre dos tramos de 60 días, un corte en cada señal (una muestra fija)
   y 20 cortes T al azar (semilla fija); las señales con t ≤ T, idénticas con los datos hasta T y con todos (nada mira
   datos posteriores a la decisión, ni 1 min). Que la prueba caza las trampas está en test_lab_vivo_recorte.py.
3. Determinismo: dos pasadas iguales.
4. Por flanco: menos de max_senales_dia señales por día y como mucho 1 por moneda y minuto.
5. Velocidad: ≤ 2 s con 90 días sintéticos.
6. Sin red ni disco: socket bloqueado y escritura de archivos prohibida durante el cálculo.
Una familia nueva del generador pasa esta prueba automáticamente (el registro escanea la carpeta)."""
import builtins
import socket
import time

import numpy as np
import pytest

from bot.lab_vivo import catalogo, datos, familias, recorte
from test_lab_vivo_comun import DAY, MIN, fuente, reglas  # noqa: F401

REG = familias.cargar(forzar=True)
POR_SENAL = 5     # cortes por señal en cada tramo de la prueba (la admisión con datos reales hace recorte.POR_SENAL)


def variantes_de(nombre: str, maximo: int = 3) -> list[dict]:
    """Los parámetros por defecto y hasta `maximo` variantes distintas de la siembra y del catálogo."""
    out = [{}]
    hs = catalogo.siembra()
    try:
        from bot import config
        hs += catalogo.catalogo(config.DATA_DIR / "lab_vivo" / "cola")[0]
    except Exception:
        pass
    vistos = set()
    for h in hs:
        if h["familia"] != nombre:
            continue
        for v in h["variantes"]:
            k = repr(sorted(v["params"].items()))
            if k not in vistos and len(out) <= maximo:
                vistos.add(k)
                out.append(dict(v["params"]))
    return out


@pytest.fixture(scope="module")
def m180():   # con noticias sintéticas, para que las familias de noticias también den señales (y se prueben)
    return datos.Mercado(fuente(dias=180, semilla=21, noticias=True))


@pytest.fixture(scope="module")
def m90():
    return datos.Mercado(fuente(dias=90, semilla=22, noticias=True))


def test_todas_las_familias_cargan():
    assert not familias.errores(), familias.errores()
    for n in ("apertura_impulso", "ruptura_canal", "compresion_ruptura", "tendencia_diaria"):
        assert n in REG
    for n, f in REG.items():
        assert f.nombre == n and f.version >= 1 and f.titulo and f.descripcion
        assert f.momento in ("aperturas", "continuo")
        assert f.datos and "velas_1m" in f.datos
        for k, p in f.esquema().items():
            v, e = p.validar(p.defecto)
            assert e is None, (n, k, e)


class _SinRed(socket.socket):
    def __init__(self, *a, **k):
        raise OSError("red prohibida en las familias")


def _sin_disco(monkeypatch):
    real = builtins.open

    def solo_lectura(file, mode="r", *a, **k):
        if any(x in mode for x in "wax+"):
            raise PermissionError(f"las familias no escriben archivos ({file})")
        return real(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", solo_lectura)
    monkeypatch.setattr(socket, "socket", _SinRed)


def _esquema(s, n_monedas: int):
    n = len(s)
    for k, _ in s.CAMPOS:
        assert len(getattr(s, k)) == n
    if not n:
        return
    assert np.all(s.t % MIN == 0)
    assert np.all(np.diff(s.t) >= 0)
    assert np.all(np.isin(s.dir, (-1, 1)))
    assert s.moneda.min() >= 0 and s.moneda.max() < n_monedas
    sp = s.stop_pct[np.isfinite(s.stop_pct)]
    assert np.all(sp > 0)


@pytest.mark.parametrize("nombre", sorted(REG))
def test_conformidad(nombre, m180, m90, monkeypatch, reglas):  # noqa: F811
    fam = REG[nombre]
    rng = np.random.default_rng(20261029)
    per = m180.periodo
    a1 = per["inicio"] + 100 * DAY
    tramos = [(a1, a1 + 60 * DAY), (per["inicio"] + 120 * DAY, per["inicio"] + 180 * DAY - MIN)]
    _sin_disco(monkeypatch)
    for p in variantes_de(nombre):
        pc, err = fam.completar(p)
        assert not err, err
        # 3. determinismo y 1. esquema
        s = fam.senales(m180, p, per["inicio"], per["fin"])
        assert s.iguales(fam.senales(m180, p, per["inicio"], per["fin"]))
        _esquema(s, len(m180.monedas))
        # 4. por flanco
        if len(s):
            _, cuenta = np.unique(s.t // DAY, return_counts=True)
            assert cuenta.max() < fam.max_senales_dia
            assert len(np.unique(s.t * 16 + s.moneda.astype(np.int64))) == len(s)
        # 2. recorte: un corte en cada señal (muestra fija) y 10 al azar por tramo, con t ≤ T
        r = recorte.comprobar(fam, [(m180, a, b) for a, b in tramos], p, rng, 10, por_senal=POR_SENAL)
        assert not recorte.motivos(r), (nombre, p, r)
        assert r["cortes_azar"] == 20 and (r["cortes_senal"] > 0) == (r["senales"] > 0)
        # 5. velocidad con 90 días
        t0 = time.time()
        fam.senales(m90, p, m90.periodo["inicio"], m90.periodo["fin"])
        assert time.time() - t0 <= 2.0, (nombre, p, time.time() - t0)


def test_alguna_senal_en_datos_sinteticos(m180, reglas):  # noqa: F811
    """Para que la prueba del recorte mida algo: la mayoría de las familias dan señales con las velas sintéticas."""
    con = [n for n, f in REG.items() if len(f.senales(m180, {}, m180.periodo["inicio"], m180.periodo["fin"]))]
    assert len(con) >= len(REG) - 4, sorted(set(REG) - set(con))


def test_la_tarea_bloquea_la_red():
    import subprocess
    import sys
    code = ("from bot.lab_vivo import recursos; recursos.bloquear_red(); import socket\n"
            "try:\n    socket.socket()\nexcept OSError as e:\n    print('bloqueada', e)\n")
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=120)
    assert "bloqueada" in r.stdout
