"""La prueba del recorte (bot/lab_vivo/recorte.py; docs/07, 4.4, 6.4 y A8) caza las familias trampa que miran al
futuro, también 1 min, que la prueba de antes no veía nunca (solo comparaba las señales con t < T y T iba redondeado al
minuto: una familia que decide en t con la vela que abre en t salía siempre igual). Con velas sintéticas y sin red."""
import json

import numpy as np
import pytest

from bot.lab_vivo import ajustes, datos, etapas, familias, recorte
from bot.lab_vivo.familias import base
from test_lab_vivo_comun import DAY, MIN, fuente, lab, reglas  # noqa: F401


class Trampa(base.Familia):
    """Decide cada `cada_min` minutos con el movimiento de los 30 min anteriores... medido hasta el cierre de dentro de
    `futuro_min` minutos (0 = causal: hasta el cierre de la última vela cerrada en t). quita=True: la señal sale salvo
    que lo que viene vaya en contra (sin ese dato, sale siempre): la mirada al futuro QUITA señales en vez de ponerlas."""
    titulo = "Trampa"
    descripcion = "Familia de prueba que mira al futuro"
    calentamiento_d = 2
    max_senales_dia = 2000.0

    def __init__(self, futuro_min: int = 1, cada_min: int = 30, quita: bool = False, nombre: str = "trampa"):
        self.futuro_min, self.cada_min, self.quita, self.nombre = futuro_min, cada_min, quita, nombre

    def senales_crudas(self, m, p, desde_ms, hasta_ms):
        partes = []
        paso = self.cada_min * MIN
        t = np.arange(-(-desde_ms // paso) * paso, hasta_ms, paso, dtype=np.int64)
        for i, c in self.monedas_de(m, p):
            antes = base.cierre_en(m, c, t - 30 * MIN)
            luego = base.cierre_en(m, c, t + self.futuro_min * MIN)
            with np.errstate(invalid="ignore"):
                if self.quita:
                    ok = np.isfinite(antes) & ~(luego < antes * 0.9995)
                    d = np.ones(int(ok.sum()), np.int8)
                else:
                    ok = np.isfinite(antes) & (np.abs(luego / antes - 1) > 0.0004)
                    d = np.where(luego[ok] > antes[ok], 1, -1)
            partes.append(base.emitir(t[ok], i, d))
        return base.Senales.juntar(partes)


@pytest.fixture(scope="module")
def m90():
    return datos.Mercado(fuente(dias=90, semilla=23))


def _tramos(m):
    per = m.periodo
    a1 = per["inicio"] + 10 * DAY
    return [(m, a1, a1 + 30 * DAY), (m, per["inicio"] + 50 * DAY, per["inicio"] + 80 * DAY)]


def _antigua(fam, m, tramos, rng, n=10) -> int:
    """La prueba de antes: cortes al azar redondeados al minuto y solo las señales con t < T."""
    malos = 0
    for mv, a, b in tramos:
        base_ = fam.senales(mv, {}, a, b)
        for T in rng.integers(a + DAY, b, n):
            T = int(T) // MIN * MIN
            if not base_.entre(a, T).iguales(fam.senales(mv.recortado(T), {}, a, b).entre(a, T)):
                malos += 1
    return malos


@pytest.mark.parametrize("futuro_min,cada_min", [(1, 30), (1, 240), (15, 60), (60, 120)])
def test_caza_con_certeza_lo_que_mira_al_futuro(m90, futuro_min, cada_min):
    fam = Trampa(futuro_min, cada_min)
    r = recorte.comprobar(fam, _tramos(m90), {}, np.random.default_rng(1), 10, por_senal=12)
    assert r["senales"] > 20 and r["cortes_senal"] == 24
    assert r["malos_senal"] == r["cortes_senal"]          # TODAS las señales cortadas cambian: con certeza
    mot = recorte.motivos(r)
    assert mot and mot[0].startswith("A8: prueba del recorte") and "posteriores a la decisión" in mot[0]


def test_la_prueba_de_antes_no_veia_1_min(m90):
    fam = Trampa(1, 30)
    assert _antigua(fam, m90, _tramos(m90), np.random.default_rng(1)) == 0   # el fallo que tenía A8


def test_no_da_falsos_positivos_con_una_familia_causal(m90):
    fam = Trampa(0, 30)                                    # mira el cierre de ahora: causal
    r = recorte.comprobar(fam, _tramos(m90), {}, np.random.default_rng(1), 10, por_senal=40)
    assert r["senales"] > 100 and r["cortes_senal"] == 80 and r["malos_senal"] == 0 and r["malos_azar"] == 0
    assert not recorte.motivos(r)


def test_caza_lo_que_quita_una_mirada_al_futuro(m90):
    """La mirada que QUITA señales (sale salvo que lo que viene vaya en contra) no cambia las señales que hay: la
    cazan los cortes al azar cuando caen en una que se quitó (con decisiones cada minuto, casi siempre)."""
    fam = Trampa(1, 1, quita=True)
    r = recorte.comprobar(fam, _tramos(m90), {}, np.random.default_rng(1), 10, por_senal=5)
    assert r["malos_azar"] > 0 and recorte.motivos(r)


def test_tramo_sin_senales_se_dice(m90):
    fam = Trampa(1, 30)
    per = m90.periodo
    r = recorte.comprobar(fam, [(m90, per["fin"] + DAY, per["fin"] + 3 * DAY)], {}, np.random.default_rng(1), 2)
    assert r["tramos_vacios"] == 1 and r["cortes_senal"] == 0


def test_la_admision_rechaza_la_trampa_y_admite_la_causal(lab, reglas, monkeypatch):  # noqa: F811
    """A8 de verdad (etapas.admision) sobre una caché sintética: la trampa de 1 min sale rechazada; su gemela causal,
    no."""
    t0 = 1_740_960_000_000                                   # 03-03-2025, lunes
    per = {"epoca": "sint", "inicio": t0 + 3 * DAY, "fin": t0 + 140 * DAY, "corte": t0 + 75 * DAY}
    fu = datos.fuente_sintetica(dias=142, semilla=5, t0=t0, monedas=("BTC", "ETH"), periodo=per)
    car = ajustes.CACHE / "sint-recorte"
    datos.guardar_fuente(fu, car, tf_cache=())
    reg = familias.cargar()
    monkeypatch.setitem(reg, "trampa_1min", Trampa(1, 30, nombre="trampa_1min"))
    monkeypatch.setitem(reg, "trampa_causal", Trampa(0, 30, nombre="trampa_causal"))
    sal = {"tipo": "A", "sl": 2.0}
    tr = {"id": "adm-trampa", "cache": str(car),
          "variantes": [{"vid": "V1", "familia": "trampa_1min", "params": {}, "salida": sal},
                        {"vid": "V2", "familia": "trampa_causal", "params": {}, "salida": sal}]}
    res = etapas.admision(tr, None)["variantes"]
    assert not res["V1"]["ok"] and any(m.startswith("A8: prueba del recorte") for m in res["V1"]["motivos"])
    assert res["V1"]["recorte"]["malos_senal"] == res["V1"]["recorte"]["cortes_senal"] > 0
    assert res["V2"]["ok"], res["V2"]["motivos"]
    assert res["V2"]["recorte"]["cortes_senal"] == 2 * recorte.POR_SENAL and res["V2"]["recorte"]["malos_senal"] == 0
    json.dumps(res)                                          # el resultado va a un .json


def test_el_calentamiento_incluye_el_de_los_filtros():
    v = {"familia": "ruptura_canal", "params": {"regimen_cripto": "no_en_contra"}}
    assert etapas.calentamiento_d(v) == ajustes.CALENTAMIENTO_MAX_D
    assert etapas.calentamiento_d({"familia": "ruptura_canal", "params": {}}) == familias.obtener("ruptura_canal").calentamiento_d
    assert etapas.calentamiento_d({"familia": "calendario", "params": {"regimen_btc": "a_favor"}}) >= 55
