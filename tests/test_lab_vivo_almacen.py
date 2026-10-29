"""lab_vivo.db (docs/07, 12): el esquema, los disparadores de solo añadir en pruebas, series y eventos, y el número
de pruebas acumulado (una vez por variante)."""
import sqlite3

import numpy as np
import pytest

from bot.lab_vivo import almacen, previas
from test_lab_vivo_comun import lab  # noqa: F401


def test_esquema_y_tablas(lab):  # noqa: F811
    con = almacen.conectar()
    tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("hipotesis", "variantes", "pruebas", "series", "operaciones", "avance", "trabajos", "consolidaciones",
              "pruebas_previas", "previas_p", "familias", "noticias_vivas", "eventos", "estado_kv"):
        assert t in tablas
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_pruebas_series_y_eventos_no_se_borran_ni_cambian(lab):  # noqa: F811
    con = almacen.conectar()
    with con:
        pid = almacen.insertar_prueba(con, {"variante": "h-20261029-0000-x:V1", "etapa": "rapida", "cuenta_en_n": 1,
                                            "n_dentro": 10, "R_dentro": 0.1}, ("2025-01-01", np.array([0.0, 1.0, -1.0])))
        almacen.evento(con, "nota", "algo")
    for sql in ("DELETE FROM pruebas", "UPDATE pruebas SET R_dentro = 9", "DELETE FROM series",
                "UPDATE series SET dias = 1", "DELETE FROM eventos", "UPDATE eventos SET texto = 'x'"):
        with pytest.raises(sqlite3.DatabaseError) as e:
            with con:
                con.execute(sql)
        assert "no se" in str(e.value)
    s = almacen.blob_a_serie(con.execute("SELECT r_dia FROM series WHERE prueba = ?", (pid,)).fetchone()[0])
    assert s.tolist() == [0.0, 1.0, -1.0]


def test_n_total_cuenta_una_vez_por_variante(lab):  # noqa: F811
    con = almacen.conectar()
    with con:
        for et in ("rapida", "completa", "robustez", "estres"):
            almacen.insertar_prueba(con, {"variante": "h-20261029-0000-x:V1", "etapa": et, "cuenta_en_n": 1})
        almacen.insertar_prueba(con, {"variante": "h-20261029-0000-x:V2", "etapa": "rapida", "cuenta_en_n": 1})
        almacen.insertar_prueba(con, {"variante": "canario:libro", "etapa": "canario", "cuenta_en_n": 0})
    assert almacen.n_total(con) == 2
    assert con.execute("SELECT SUM(cuenta_en_n) FROM pruebas").fetchone()[0] == 2


def test_previas_siembra_idempotente(lab):  # noqa: F811
    con = almacen.conectar()
    with con:
        n1 = previas.sembrar(con)
        n2 = previas.sembrar(con)
    assert n1 == n2 == previas.TOTAL == 3917
    ps = previas.ps_previas(con)
    assert len(ps) == 3917 and all(0 <= p <= 1 for p in ps)


def test_kv(lab):  # noqa: F811
    con = almacen.conectar()
    with con:
        almacen.kv_set(con, "x", {"a": 1})
        almacen.kv_set(con, "x", {"a": 2})
    assert almacen.kv_get(con, "x") == {"a": 2}
    assert almacen.kv_get(con, "no", 5) == 5
