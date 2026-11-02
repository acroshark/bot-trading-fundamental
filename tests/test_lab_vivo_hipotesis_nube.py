"""Las hipótesis que el coordinador deja en docs/hipotesis_nube/ (se copian a mano a lab_vivo\\cola\\entrada en el PC):
pasan la admisión sin datos (A1-A7 y A10), su id es el nombre del archivo, el autor es «coordinador», y cada variante
cambia una sola cosa respecto a V1 (un parámetro o la salida), como pide el protocolo (docs/07, 15.2), para que la
estabilidad (B6) mida la idea. Sin red ni datos."""
import json
from pathlib import Path

import pytest

from bot.lab_vivo import hipotesis, salidas
from bot.lab_vivo.familias import obtener
from test_lab_vivo_comun import reglas  # noqa: F401

CARPETA = Path(__file__).resolve().parents[1] / "docs" / "hipotesis_nube"
ARCHIVOS = sorted(CARPETA.glob("h-*.json"))


def test_hay_entre_4_y_6():
    assert 4 <= len(ARCHIVOS) <= 6


@pytest.mark.parametrize("ruta", ARCHIVOS, ids=lambda p: p.stem)
def test_pasa_la_admision_sin_datos(ruta, reglas):  # noqa: F811
    h = json.loads(ruta.read_text(encoding="utf-8"))
    assert h["id"] == ruta.stem and h["autor"] == "coordinador"
    r = hipotesis.validar(h)
    assert r["ok"], r["motivos"]
    assert all(v["cumple_reglas"] and not v["fuera_de_reglas"] for v in r["variantes"])
    assert all(len(v["filtros"]) <= 3 for v in r["variantes"])
    assert h["reglas"] == hipotesis.REGLAS_USUARIO and not h.get("exploratoria")
    for k in ("mecanismo", "prediccion", "falsacion", "que_aporta", "por_que_vale_con_costes"):
        assert len(h["logica"][k]) >= 40, k


@pytest.mark.parametrize("ruta", ARCHIVOS, ids=lambda p: p.stem)
def test_cada_variante_cambia_una_sola_cosa(ruta):
    h = json.loads(ruta.read_text(encoding="utf-8"))
    fam = obtener(h["familia"])
    vs = h["variantes"]
    assert 1 <= len(vs) <= 6 and vs[0]["id"] == "V1"
    p1, _ = fam.completar(vs[0]["params"])
    s1, _ = salidas.normalizar(vs[0]["salida"])
    for v in vs[1:]:
        p, _ = fam.completar(v["params"])
        s, _ = salidas.normalizar(v["salida"])
        cambios = [k for k in p if p[k] != p1[k]] + (["salida"] if s != s1 else [])
        assert len(cambios) == 1, (v["id"], cambios)
