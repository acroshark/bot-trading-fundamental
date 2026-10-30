"""Noticias de 3 años en el laboratorio vivo (bot/lab_vivo/noticias.py y datos.py): el Mercado solo da las que ya se
podían usar (recortado y ventana), la caché las guarda y las vuelve a abrir, la firma de la época cambia con ellas (y
no cambia sin ellas) y la admisión con datos (A6) no mide una familia de noticias sin el archivo compilado."""
import json

import numpy as np

from bot.lab_vivo import ajustes, datos, etapas, familias
from bot.lab_vivo import noticias as nl
from test_lab_vivo_comun import DAY, H, MIN, T0, lab, reglas  # noqa: F401


def _n(ts, mon=1, dir_=50):
    return nl.Noticias.de_filas([{"t": t, "t0": t - 5 * MIN, "n30": 3, "mon": mon, "dir": dir_, "imp": 5}
                                 for t in ts], datos.TIPOS_SINT, ["a.com"], {"firma": "x"})


def test_noticias_tramo_sentido_monedas_y_cobertura():
    n = nl.Noticias.de_filas([{"t": T0 + 2 * H, "mon": 0, "dir": -40}, {"t": T0 + H, "mon": 0b0101, "dir": 10},
                              {"t": T0 + 3 * DAY, "mon": 0b0010, "dir": 90}], datos.TIPOS_SINT)
    assert list(n.t) == [T0 + H, T0 + 2 * H, T0 + 3 * DAY]                       # ordenadas por t
    assert list(n.sentido()) == [0, -1, 1]
    assert list(n.de_moneda(0)) == [True, False, False] and list(n.de_moneda(2, generales=True)) == [True, True, False]
    assert len(n.tramo(T0 + H, T0 + 2 * H)) == 1 and len(n.tramo(None, None)) == 3
    assert n.cobertura(T0, T0 + 4 * DAY) == 0.5                                # 2 de 4 días con alguna
    assert len(nl.Noticias.vacias()) == 0


def test_el_mercado_solo_da_las_que_ya_se_podian_usar():
    fu = datos.fuente_sintetica(dias=10, semilla=3)
    fu.noticias = _n([T0 + H, T0 + 2 * H, T0 + 3 * H])
    m = datos.Mercado(fu)
    assert len(m.noticias) == 3
    assert list(m.recortado(T0 + 2 * H).noticias.t) == [T0 + H]                  # t < hasta (como las velas)
    assert list(m.recortado(T0 + 2 * H + 1).noticias.t) == [T0 + H, T0 + 2 * H]  # la decisión en t la ve
    assert list(m.ventana(T0 + 90 * MIN, None).noticias.t) == [T0 + 2 * H, T0 + 3 * H]
    assert datos.Mercado(datos.fuente_sintetica(dias=10, semilla=3)).noticias is None


def test_la_cache_guarda_y_abre_las_noticias(tmp_path):
    fu = datos.fuente_sintetica(dias=12, semilla=4, monedas=("BTC", "ETH"), noticias=True)
    assert len(fu.noticias) > 20
    abierta = datos.guardar_fuente(fu, tmp_path / "cache", tf_cache=())
    assert abierta.meta["noticias"] == {"firma": "sint4", "n": len(fu.noticias)}
    for k in nl.COLUMNAS:
        assert np.array_equal(getattr(abierta.noticias, k), getattr(fu.noticias, k)), k
    assert abierta.noticias.tipos == fu.noticias.tipos
    sin = datos.guardar_fuente(datos.fuente_sintetica(dias=12, semilla=4, monedas=("BTC",)), tmp_path / "c2",
                               tf_cache=())
    assert sin.noticias is None and sin.meta["noticias"] is None


def test_construir_fuente_coge_el_archivo_compilado(tmp_path, monkeypatch):
    nl.guardar(_n([T0 - DAY, T0 + H, T0 + 30 * DAY]), tmp_path / "lab", {"firma": "abc123"})
    monkeypatch.setattr(nl, "DIR", tmp_path / "lab")
    monkeypatch.setattr(datos, "archivos_moneda", lambda c, meses: [])
    monkeypatch.setattr(datos, "cargar_velas", lambda paths, a, b: datos.Velas(*(np.zeros(0) for _ in range(7))))
    fu = datos.construir_fuente(T0, T0 + 10 * DAY, {"inicio": T0, "fin": T0 + 10 * DAY, "corte": T0 + 5 * DAY},
                                monedas=("BTC",), aperturas=False)
    assert list(fu.noticias.t) == [T0 + H] and fu.meta["noticias"]["firma"] == "abc123"


def test_la_firma_de_la_epoca_cambia_con_las_noticias(monkeypatch):
    monkeypatch.setattr(datos, "archivos_moneda", lambda c, meses: [])
    monkeypatch.setattr(datos.noticias_mod, "firma", lambda carpeta=None: None)
    sin = datos.firma_epoca("2026-10")
    assert sin == datos.firma_archivos([])                    # sin noticias, la de siempre
    monkeypatch.setattr(datos.noticias_mod, "firma", lambda carpeta=None: "f1")
    f1 = datos.firma_epoca("2026-10")
    monkeypatch.setattr(datos.noticias_mod, "firma", lambda carpeta=None: "f2")
    assert f1 != sin and datos.firma_epoca("2026-10") not in (sin, f1)


def test_a6_con_datos_no_mide_una_familia_de_noticias_sin_noticias(lab, reglas):  # noqa: F811
    t0 = 1_740_960_000_000
    per = {"epoca": "sint", "inicio": t0 + 40 * DAY, "fin": t0 + 140 * DAY, "corte": t0 + 100 * DAY}
    sal = {"tipo": "A", "sl": 2.0}
    tr = {"id": "adm-noticias", "variantes": [{"vid": "V1", "familia": "noticia_racimo", "params": {}, "salida": sal},
                                              {"vid": "V2", "familia": "ruptura_canal", "params": {}, "salida": sal}]}
    fu = datos.fuente_sintetica(dias=142, semilla=5, t0=t0, monedas=("BTC", "ETH"), periodo=per)
    car = ajustes.CACHE / "sint-sin-noticias"
    datos.guardar_fuente(fu, car, tf_cache=())
    familias.cargar()
    res = etapas.admision({**tr, "cache": str(car)}, None)["variantes"]
    assert not res["V1"]["ok"] and any(m.startswith("A6: las noticias de 3 años cubren el 0 %")
                                       for m in res["V1"]["motivos"])
    assert not any(m.startswith("A6") for m in res["V2"]["motivos"])
    fu2 = datos.fuente_sintetica(dias=142, semilla=5, t0=t0, monedas=("BTC", "ETH"), periodo=per, noticias=True)
    car2 = ajustes.CACHE / "sint-con-noticias"
    datos.guardar_fuente(fu2, car2, tf_cache=())
    res2 = etapas.admision({**tr, "id": "adm-noticias-2", "cache": str(car2)}, None)["variantes"]
    assert res2["V1"]["ok"], res2["V1"]["motivos"]
    assert res2["V1"]["recorte"]["cortes_senal"] > 0 and res2["V1"]["recorte"]["malos_senal"] == 0
    json.dumps(res2)
