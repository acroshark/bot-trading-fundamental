"""El corredor del laboratorio vivo (docs/07, 11): prioridades, turno entre familias y cupos; pausas con
openings_on (se juntan; 5 min antes y 30 después; fines de semana; modo prudente sin agenda); ningún trabajo empieza en
pausa; parada cooperativa y forzada; reanudación (en_curso vuelve a la cola; ingesta idempotente; .tmp a medias); tres
fallos; guarda de memoria; freno por CPU; parar y pausa_manual; canario fallido que para la medida; familia cambiada sin
subir versión; código del bot que no se importa. Todo con un lanzador falso (sin subprocesos) y un reloj de prueba."""
import json
from datetime import date, datetime

import pytest

from bot.lab_vivo import agenda, ajustes, almacen, catalogo
from bot.lab_vivo.corredor import Corredor
from test_lab_vivo_comun import MIN, lab, reglas  # noqa: F401

MAD = ajustes.MAD


def ms(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=MAD).timestamp() * 1000)


class FakePopen:
    def __init__(self):
        self.code = None
        self.killed = False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9


class Lanzador:
    def __init__(self):
        self.lanzados = {}

    def __call__(self, tid):
        p = FakePopen()
        self.lanzados[tid] = p
        return {"popen": p, "pid": 999999}


def corredor(now, lanzador=None, memoria=lambda: 8000.0, cpu=lambda: 10.0, sin_pausas=True, openings_on=None):
    c = Corredor(procesos=1, sin_pausas=sin_pausas, reloj=lambda: now[0], lanzador=lanzador or Lanzador(),
                 memoria=memoria, cpu=cpu, openings_on=openings_on or (lambda d: []))
    assert c.arrancar()
    return c


def cache_falsa(c):
    ep = c.epoca()
    car = ajustes.CACHE / f"{ep}-x"
    car.mkdir(parents=True, exist_ok=True)
    (car / "meta.json").write_text(json.dumps({"firma": "x", "hasta": 0, "periodo": {"desde": "2023-10-01",
                                                                                    "hasta": "2026-09-30"}}),
                                   encoding="utf-8")
    return car


def canario_ok(c):
    with c.con:
        almacen.kv_set(c.con, "canario", {"estado": "ok", "firma_codigo": c.firma_cod})


# ---------------------------------------------------------------- agenda y pausas

def test_agenda_real_se_juntan_y_5_antes_30_despues():
    tr = agenda.calcular(ms(2026, 10, 29), dias=1)
    hoy = [x for x in tr if ms(2026, 10, 29) <= x["desde"] < ms(2026, 10, 30)]
    eu = next(x for x in hoy if "europa" in x["ventanas"])
    assert (eu["desde"], eu["hasta"]) == (ms(2026, 10, 29, 8, 55), ms(2026, 10, 29, 9, 30))
    # São Paulo (14:00 de Madrid) y Nueva York (14:30, semana del desfase del cambio de hora) se juntan
    am = next(x for x in hoy if "nuevayork" in x["ventanas"])
    assert "saopaulo" in am["ventanas"] and (am["desde"], am["hasta"]) == (ms(2026, 10, 29, 13, 55), ms(2026, 10, 29, 15, 0))
    sab = [x for x in agenda.calcular(ms(2026, 10, 31), dias=1) if ms(2026, 10, 31) <= x["desde"] < ms(2026, 11, 1)]
    assert sab == []                                       # fin de semana: sin pausas


def test_puede_empezar_y_3_min_antes():
    tr = [{"desde": 1000 * MIN, "hasta": 1035 * MIN, "nombre": "Europa", "ventana": "europa"}]
    assert agenda.puede_empezar(990 * MIN, tr)[0]
    assert not agenda.puede_empezar(998 * MIN, tr)[0]     # a menos de 3 min
    assert not agenda.puede_empezar(1010 * MIN, tr)[0]
    assert agenda.puede_empezar(1036 * MIN, tr)[0]


def test_modo_prudente_sin_agenda(lab):  # noqa: F811
    def rota(d):
        raise SyntaxError("apertura a medio editar")
    tr, origen = agenda.obtener(ms(2026, 10, 29, 12), openings_on=rota)
    assert origen == "prudente"
    assert agenda.en_pausa(ms(2026, 10, 29, 9, 0), tr) and agenda.en_pausa(ms(2026, 10, 29, 15, 0), tr)
    assert not agenda.en_pausa(ms(2026, 10, 29, 12, 0), tr)
    agenda.guardar([{"desde": 1, "hasta": 2, "ventanas": ["europa"], "aperturas": [], "nombre": "x", "ventana": "europa"}])
    tr2, origen2 = agenda.obtener(ajustes.ahora_ms(), openings_on=rota)
    assert origen2 == "guardada"


def test_ningun_trabajo_en_pausa_y_parada_en_t_menos_3(lab, reglas):  # noqa: F811
    apertura = ms(2026, 10, 29, 9, 0)

    def ops(d):
        return [{"id": "europa", "open_ms": apertura}] if d == date(2026, 10, 29) else []
    now = [apertura - 6 * MIN]
    la = Lanzador()
    c = corredor(now, la, sin_pausas=False, openings_on=ops)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x", "variantes": [], "cache": "x"})
    c.paso()                                               # T − 6: a menos de 3 min de la pausa (T − 5): no empieza
    assert not la.lanzados
    now[0] = apertura - 7 * MIN
    c.paso()
    assert not la.lanzados                                 # T − 7: tampoco (la pausa empieza en T − 5)
    c.tramos, c.agenda_ts = [], 0
    now[0] = apertura - 12 * MIN
    c.paso()
    assert len(la.lanzados) == 1                           # T − 12: sí
    now[0] = apertura - 4 * MIN                           # T − 4: ya en pausa: pausa.ahora
    c.paso()
    assert ajustes.PAUSA_AHORA.exists() and c.procs
    now[0] = apertura - 2 * MIN                           # T − 2: la que quede se mata, sin gastar intento
    c.paso()
    assert not c.procs
    r = c.con.execute("SELECT estado, intentos FROM trabajos").fetchone()
    assert tuple(r) == ("pendiente", 0)
    now[0] = apertura + 10 * MIN
    c.paso()
    assert len(la.lanzados) == 1                           # en la pausa no empieza nada
    now[0] = apertura + 31 * MIN
    c.paso()
    assert not ajustes.PAUSA_AHORA.exists() and len(la.lanzados) == 1 and c.procs   # vuelve a empezar tras la pausa


def test_pausa_cooperativa_codigo_75(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        tid = c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x", "variantes": [], "cache": "x"})
    c.paso()
    la.lanzados[tid].code = ajustes.CODIGO_PAUSADO
    c.vigilar_procesos(now[0])
    assert tuple(c.con.execute("SELECT estado, intentos FROM trabajos").fetchone()) == ("pendiente", 0)


# ---------------------------------------------------------------- prioridades, turno y cupos

def test_prioridades_y_turno_entre_familias(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        for hid, fam in (("h-20261029-0000-a", "fa"), ("h-20261029-0000-b", "fa"), ("h-20261029-0000-c", "fb")):
            c.con.execute("INSERT INTO hipotesis(id, recibida, familia, firma, estado, cuerpo) VALUES (?, 0, ?, 'x', "
                          "'en_rapida', '{}')", (hid, fam))
        c.encolar("rapida", "h-20261029-0000-a", {"firma_datos": "x"}, 3)
        now[0] += 1
        c.encolar("rapida", "h-20261029-0000-b", {"firma_datos": "x"}, 3)
        now[0] += 1
        c.encolar("rapida", "h-20261029-0000-c", {"firma_datos": "x"}, 3)
        c.encolar("robustez", "h-20261029-0000-b:V1", {"firma_datos": "x"}, 3)
        c.encolar("admision", "h-20261029-0000-c", {"firma_datos": "x"}, 5)
        c.con.execute("UPDATE trabajos SET empezado = ? WHERE objetivo = 'h-20261029-0000-a'", (now[0],))
        c.con.execute("INSERT INTO trabajos(id, tipo, objetivo, prioridad, estado, creado, empezado) VALUES "
                      "('x1', 'rapida', 'h-20261029-0000-a', 63, 'hecho', 0, ?)", (now[0],))
    orden = [(f["tipo"], f["objetivo"]) for f in c.siguientes()]
    assert orden[0] == ("admision", "h-20261029-0000-c")
    assert orden[1] == ("robustez", "h-20261029-0000-b:V1")
    rap = [o for t, o in orden if t == "rapida"]
    assert rap.index("h-20261029-0000-c") < rap.index("h-20261029-0000-b")   # turno: fb no ha usado su turno hoy


def test_cupo_del_dia_y_por_familia(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        for k in range(30):
            fam = "fa" if k < 12 else f"f{k}"
            hid = f"h-20261029-0000-x{k:02d}"
            c.con.execute("INSERT INTO hipotesis(id, recibida, familia, firma, estado, prioridad, cuerpo) VALUES "
                          "(?, ?, ?, 'x', 'lista_para_rapida', 3, '{}')", (hid, k, fam))
            c.con.execute("INSERT INTO variantes(id, hipotesis, vid, firma, params, salida, cumple_reglas, clase) VALUES "
                          "(?, ?, 'V1', ?, '{}', '{\"tipo\": \"A\"}', 1, 'pendiente')", (hid + ":V1", hid, hid))
    c.planificar(now[0])
    n = c.con.execute("SELECT COUNT(*) FROM trabajos WHERE tipo = 'rapida'").fetchone()[0]
    fa = c.con.execute("SELECT COUNT(*) FROM hipotesis WHERE familia = 'fa' AND estado = 'en_rapida'").fetchone()[0]
    assert n == ajustes.CUPO_DIA == 24 and fa == ajustes.CUPO_FAMILIA
    assert c.con.execute("SELECT COUNT(*) FROM hipotesis WHERE estado = 'lista_para_rapida'").fetchone()[0] == 6


def test_cupo_de_la_pasada_del_generador(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    hs = []
    for k in range(7):
        h = catalogo.siembra()[1]
        h["id"] = f"h-20261029-1215-gen{k}"
        h["autor"] = "generador-claude"
        for v in h["variantes"]:
            v["params"]["horas"] = (24, 48, 80, 96, 168, 220)[(k + int(v["id"][1])) % 6]
            v["params"]["tf_min"] = (15, 60, 240)[k % 3]
            v["params"]["stop_atr_k"] = (1.0, 1.5, 2.0)[k % 3]
        hs.append(h)
    catalogo.escribir(hs)
    c.admitir_buzon()
    est = [r[0] for r in c.con.execute("SELECT estado FROM hipotesis ORDER BY id")]
    assert est.count("admitida") == 6 and est.count("rechazada") == 1


# ---------------------------------------------------------------- reanudación, ingesta y fallos

def test_reanudacion_tras_caida(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    with c.con:
        c.con.execute("INSERT INTO trabajos(id, tipo, objetivo, prioridad, estado, intentos, creado) VALUES "
                      "('t1', 'rapida', 'h', 63, 'en_curso', 0, 0)")
    (ajustes.TRABAJOS / "t1.resultado.json.tmp").write_text("{a medias", encoding="utf-8")
    c2 = corredor(now)
    assert tuple(c2.con.execute("SELECT estado, intentos FROM trabajos WHERE id = 't1'").fetchone()) == ("pendiente", 1)
    assert not list(ajustes.TRABAJOS.glob("*.tmp"))


def test_ingesta_idempotente(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    with c.con:
        tid = c.encolar("canario", "canario", {"firma_datos": "x"})
    (ajustes.TRABAJOS / f"{tid}.resultado.json").write_text(json.dumps(
        {"estado": "ok", "libro": {"dentro_n": 2389, "dentro_R": -0.0569, "fuera_n": 1464, "fuera_R": -0.075},
         "texto": "Canario del libro: reproduce"}), encoding="utf-8")
    assert c.ingerir() == 1
    assert c.ingerir() == 0
    assert c.con.execute("SELECT COUNT(*) FROM pruebas WHERE etapa = 'canario'").fetchone()[0] == 1
    assert c.canario_ok()


def test_tres_fallos(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        c.con.execute("INSERT INTO hipotesis(id, recibida, familia, firma, estado, cuerpo) VALUES "
                      "('h-20261029-0000-f', 0, 'fa', 'x', 'en_rapida', '{}')")
        tid = c.encolar("rapida", "h-20261029-0000-f", {"firma_datos": "x"})
    for k in range(3):
        c.lanzar(now[0])
        la.lanzados[tid].code = 1
        c.vigilar_procesos(now[0])
    r = c.con.execute("SELECT estado, intentos FROM trabajos WHERE id = ?", (tid,)).fetchone()
    assert tuple(r) == ("fallido", 3)
    assert c.con.execute("SELECT estado FROM hipotesis").fetchone()[0] == "error"


def test_codigo_del_bot_no_importable_espera_sin_gastar(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        tid = c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
    c.lanzar(now[0])
    la.lanzados[tid].code = ajustes.CODIGO_NO_IMPORTABLE
    c.vigilar_procesos(now[0])
    assert tuple(c.con.execute("SELECT estado, intentos FROM trabajos").fetchone()) == ("pendiente", 0)
    la.lanzados.clear()
    c.lanzar(now[0] + 60_000)
    assert not la.lanzados                                 # 10 min de espera para la medida
    now[0] += 11 * 60_000
    c.lanzar(now[0])
    assert la.lanzados


def test_demasiado_lento(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        tid = c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
    c.lanzar(now[0])
    c.vigilar_procesos(now[0] + 16 * 60_000)
    assert la.lanzados[tid].killed
    assert c.con.execute("SELECT intentos FROM trabajos").fetchone()[0] >= 1


# ---------------------------------------------------------------- recursos y mandos

def test_guarda_de_memoria(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    mem = [1000.0]
    la = Lanzador()
    c = corredor(now, la, memoria=lambda: mem[0])
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
    c.lanzar(now[0])
    assert not la.lanzados                                 # < 1.500 MB libres: no se empieza nada
    mem[0] = 3000.0
    c.lanzar(now[0])
    assert len(la.lanzados) == 1 and c.huecos() == 0       # el segundo, solo con ≥ 4 GB libres
    mem[0] = 700.0
    c.vigilar_recursos(now[0])
    assert not c.procs                                     # < 800 MB: se mata la más reciente y vuelve a la cola
    assert tuple(c.con.execute("SELECT estado, intentos FROM trabajos").fetchone()) == ("pendiente", 0)


def test_freno_por_cpu(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la, cpu=lambda: 95.0)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
    c.vigilar_recursos(now[0])
    now[0] += 61_000
    c.vigilar_recursos(now[0])
    c.lanzar(now[0])
    assert not la.lanzados


def test_parar_y_pausa_manual(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
    ajustes.PAUSA_MANUAL.write_text("", encoding="utf-8")
    c.paso()
    assert not la.lanzados
    ajustes.PAUSA_MANUAL.unlink()
    c.paso()
    assert la.lanzados
    ajustes.PARAR.write_text("", encoding="utf-8")
    c.parar(forzar_tras_s=0)
    assert not c.procs and c.estado == "parado"
    e = json.loads(ajustes.ESTADO.read_text(encoding="utf-8"))
    assert e["estado"] == "parado"


def test_canario_fallido_para_la_medida(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = corredor(now, la)
    cache_falsa(c)
    with c.con:
        almacen.kv_set(c.con, "canario", {"estado": "fallido", "firma_codigo": c.firma_cod})
        c.encolar("rapida", "h-20261029-0000-x", {"firma_datos": "x"})
        c.encolar("admision", "h-20261029-0000-y", {"firma_datos": "x"})
    c.planificar(now[0])
    assert c.estado == "canario_fallido"
    c.lanzar(now[0])
    assert [t.split("-")[0] for t in la.lanzados] == ["admision"]   # solo sigue la admisión


def test_sin_cache_pide_los_datos(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    c.planificar(now[0])
    assert c.con.execute("SELECT tipo FROM trabajos").fetchone()[0] == "datos"


def test_familia_cambiada_sin_subir_version(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    with c.con:
        c.con.execute("UPDATE familias SET sha = 'otro' WHERE nombre = 'ruptura_canal'")
    catalogo.escribir([catalogo.siembra()[1]])
    c.admitir_buzon()
    adm = json.loads(c.con.execute("SELECT admision FROM hipotesis").fetchone()[0])
    assert any("familia cambiada sin subir versión" in m for m in adm["motivos"])


def test_reglas_apagadas_no_arranca(lab, monkeypatch):  # noqa: F811
    from bot import config
    monkeypatch.setattr(config, "USER_RULES", False)
    c = Corredor(procesos=1, sin_pausas=True, lanzador=Lanzador(), memoria=lambda: 8000.0, cpu=lambda: 1.0,
                 openings_on=lambda d: [])
    assert not c.arrancar() and c.estado == "error"


# ---------------------------------------------------------------- recursos de Windows de verdad

@pytest.mark.skipif(__import__("os").name != "nt", reason="solo en Windows")
def test_recursos_de_windows_en_un_proceso_aparte():
    """proceso_bajo de verdad, en un proceso aparte (para no tocar el de las pruebas): prioridad por debajo de lo
    normal, afinidad y Job Object. Sin argtypes, el seudomanejador del proceso (−1) daba OverflowError en Windows."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from bot.lab_vivo import recursos
    code = ("import json, ctypes\n"
            "from ctypes import wintypes\n"
            "from bot.lab_vivo import recursos as r\n"
            "out = r.proceso_bajo((0,), 400)\n"
            "k = r._k32\n"
            "h = k.GetCurrentProcess()\n"
            "out['clase'] = k.GetPriorityClass(h)\n"
            "k.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))\n"
            "en = wintypes.BOOL()\n"
            "k.IsProcessInJob(h, None, ctypes.byref(en))\n"
            "out['en_job'] = bool(en.value)\n"
            "print(json.dumps(out))\n")
    p = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=120,
                       cwd=str(Path(__file__).resolve().parents[1]))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["prioridad"] == "por debajo de lo normal" and out["clase"] == recursos.BELOW_NORMAL
    assert out["afinidad"] == [0] and out["job"] and out["en_job"]
    assert recursos.pid_vivo(os.getpid()) and recursos.memoria_proceso_mb(os.getpid()) > 0
    assert recursos.memoria_libre_mb() > 0


# ---------------------------------------------------------------- una pasada (correr --una-pasada)

def _hip(c, hid, estado, fam="fa"):
    c.con.execute("INSERT INTO hipotesis(id, recibida, familia, firma, estado, prioridad, cuerpo) VALUES "
                  "(?, 1, ?, 'x', ?, 3, '{}')", (hid, fam, estado))


def test_una_pasada_espera_a_que_no_quede_nada_y_consolida(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    la = Lanzador()
    c = Corredor(procesos=1, sin_pausas=True, reloj=lambda: now[0], lanzador=la, memoria=lambda: 8000.0,
                 cpu=lambda: 10.0, openings_on=lambda d: [], una_pasada=True)
    assert c.arrancar()
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        _hip(c, "h-20261029-0000-a", "en_rapida")
        tid = c.encolar("rapida", "h-20261029-0000-a", {"firma_datos": "x"})
    assert not c.fin_de_pasada()                                    # queda un trabajo pendiente
    q = c.lo_que_queda()
    assert q["trabajos"] == {"rapida": 1} and q["en_marcha"] == 1 and not q["terminada"]
    with c.con:
        c.con.execute("UPDATE trabajos SET estado = 'hecho' WHERE id = ?", (tid,))
        c.con.execute("UPDATE hipotesis SET estado = 'terminada'")
        _hip(c, "h-20261029-0000-b", "lista_para_rapida")             # espera al cupo de otro día: no frena la pasada
    assert c.lo_que_queda()["terminada"] and c.lo_que_queda()["esperan_cupo"] == 1
    assert c.fin_de_pasada()
    assert almacen.kv_get(c.con, "consolidacion")["bh_m"] >= 3917     # BH con todas las previas
    p = almacen.kv_get(c.con, "pasada")
    assert p["motivo"] is None and p["queda"]["esperan_cupo"] == 1
    assert (ajustes.DIR / "mejores.json").exists() and (ajustes.DIR / "hipotesis.json").exists()
    assert any("Pasada terminada" in e[0] for e in c.con.execute("SELECT texto FROM eventos"))


def test_una_pasada_no_consolida_en_pausa_ni_hace_tareas_con_hora(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 9, 0)]   # en la pausa de Europa (08:55-09:30)
    ab = ms(2026, 10, 29, 9, 0)
    la = Lanzador()
    c = Corredor(procesos=1, sin_pausas=False, reloj=lambda: now[0], lanzador=la, memoria=lambda: 8000.0,
                 cpu=lambda: 10.0, openings_on=lambda d: [{"id": "europa", "open_ms": ab}] if d == date(2026, 10, 29) else [],
                 una_pasada=True)
    assert c.arrancar()
    cache_falsa(c)
    canario_ok(c)
    with c.con:
        _hip(c, "h-20261029-0000-a", "terminada")
        c.con.execute("INSERT INTO variantes(id, hipotesis, vid, firma, params, salida, cumple_reglas, clase, avance_desde) "
                      "VALUES ('h-20261029-0000-a:V1', 'h-20261029-0000-a', 'V1', 'f', '{}', '{\"tipo\": \"A\"}', 1, "
                      "'en_avance', ?)", (now[0],))
    c.actualizar_agenda(now[0])
    assert not c.fin_de_pasada()                                    # en pausa no se consolida
    now[0] = ms(2026, 10, 29, 10, 25)
    c.paso()                                                        # a las :20 el 24/7 pediría el avance; la pasada no
    assert c.con.execute("SELECT COUNT(*) FROM trabajos WHERE tipo = 'avance'").fetchone()[0] == 0
    assert c.fin_de_pasada()


def test_una_pasada_se_para_si_falla_el_canario(lab, reglas):  # noqa: F811
    now = [ms(2026, 10, 29, 12)]
    c = Corredor(procesos=1, sin_pausas=True, reloj=lambda: now[0], lanzador=Lanzador(), memoria=lambda: 8000.0,
                 cpu=lambda: 10.0, openings_on=lambda d: [], una_pasada=True)
    assert c.arrancar()
    cache_falsa(c)
    with c.con:
        almacen.kv_set(c.con, "canario", {"estado": "fallido", "firma_codigo": c.firma_cod})
        _hip(c, "h-20261029-0000-a", "lista_para_rapida")
    c.planificar(now[0])
    assert c.estado == "canario_fallido" and c.fin_de_pasada()
    assert "canario" in almacen.kv_get(c.con, "pasada")["motivo"]


def test_lock_un_solo_corredor(lab, reglas):  # noqa: F811
    import os
    now = [ms(2026, 10, 29, 12)]
    c = corredor(now)
    assert c.tomar_lock()
    ajustes.LOCK.write_text(json.dumps({"pid": os.getppid(), "ts": 0}), encoding="utf-8")   # otro proceso vivo
    assert not c.tomar_lock()
