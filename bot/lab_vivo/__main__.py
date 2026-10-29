"""Órdenes del laboratorio vivo (docs/07, 11.10): python -X utf8 -m bot.lab_vivo <orden>

| Orden | Qué hace |
|---|---|
| correr [--procesos N] [--sin-pausas] [--una-vez] [--una-pasada [--max-horas H]] | El supervisor 24/7 (--sin-pausas y --una-vez solo para pruebas). Con --una-pasada hace todo lo que haya en la cola (con las mismas pausas, prioridad y guardas), consolida y se para |
| tarea <id> | Un trabajo (lo lanza el corredor) |
| validar <archivo.json> | La admisión completa SIN medir resultados (formato, reglas, duplicados, recorte y frecuencia) |
| estado | Resumen en texto de estado.json |
| canario | Los canarios ahora (reproduce seis2_resumen.json con el libro y comprueba las comisiones reales) |
| preparar-datos | La caché de la época |
| consolidar | La consolidación ahora (con el corredor en marcha, se la pide a él: es el único que escribe en la base) |
| heredar-n --nota "…" | La orden del usuario para una base con menos pruebas que n_acumulado.jsonl (registro_n.py) |
| sembrar | Mete en el buzón la siembra de docs/07 (6.7) y el catálogo inicial convertido |

Las órdenes que calculan (validar, canario, preparar-datos y consolidar) van con prioridad baja y en los núcleos del
laboratorio, y no empiezan en una pausa de las aperturas (5 min antes a 30 min después): validar deja entonces A8 y A9
para el corredor; las demás, se niegan."""
import os

# Hilos de BLAS en 1 ANTES de importar numpy: eigvalsh y compañía no abren 6 hilos cada una (11.6)
for _k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_k, "1")

import argparse   # noqa: E402
import json   # noqa: E402
import logging   # noqa: E402
import sys   # noqa: E402
import traceback   # noqa: E402

from . import ajustes   # noqa: E402

for _k in ("TMP", "TEMP", "TMPDIR"):   # temporales en M:, nunca en C:
    os.environ[_k] = str(ajustes.TMP)


def _tarea(tid: str) -> int:
    from . import recursos
    recursos.bloquear_red()
    p = ajustes.TRABAJOS / f"{tid}.trabajo.json"
    try:
        tr = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Encargo ilegible {p}: {e}", flush=True)
        return 2
    try:   # el código del bot que usa el laboratorio (otro flujo puede estar editándolo)
        from .. import apertura, backtest_aperturas, comisiones, config, evolucion, laboratorio, paper   # noqa: F401
    except Exception:
        print("código del bot no importable:", flush=True)
        traceback.print_exc()
        return ajustes.CODIGO_NO_IMPORTABLE
    from .. import config
    if not config.USER_RULES or abs(float(config.RISK_PER_TRADE_PCT) - 0.5) > 1e-9:
        print("Las reglas del usuario tienen que estar activas y el riesgo en 0,5 %: no se mide nada", flush=True)
        return 3
    from . import etapas
    t0 = __import__("time").time()
    try:
        res = etapas.ejecutar(tr)
    except etapas.Pausado:
        print("Pausa: punto guardado; vuelve a la cola", flush=True)
        return ajustes.CODIGO_PAUSADO
    q = ajustes.TRABAJOS / f"{tid}.resultado.json"
    tmp = q.with_name(q.name + ".tmp")
    tmp.write_text(json.dumps(etapas._redondea(res), ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, q)
    print(f"Trabajo {tid} ({tr['tipo']}) hecho en {__import__('time').time() - t0:.0f} s", flush=True)
    return 0


MIN = 60_000


def _bajo() -> None:
    """Prioridad baja y los núcleos del laboratorio (sin Job Object): como el corredor y sus tareas (11.6)."""
    from . import recursos
    try:
        recursos.proceso_bajo(ajustes.CPUS, None)
    except Exception as e:   # sin prioridad baja se sigue, pero se dice
        print(f"Aviso: no pude bajar la prioridad ({e})")


def _pausa_de_aperturas() -> dict | None:
    """El tramo de pausa de las aperturas en el que estamos (o el que empieza en < 3 min), o None (agenda.py)."""
    from . import agenda
    now = ajustes.ahora_ms()
    tramos, _ = agenda.obtener(now)
    x = agenda.en_pausa(now, tramos)
    if x:
        return x
    p = agenda.proxima(now, tramos)
    return p if p and p["desde"] - now < ajustes.NO_EMPEZAR_MIN * MIN else None


def _texto_pausa(x: dict) -> str:
    return f"pausa por la apertura de {x['nombre']} ({ajustes.hora_mad(x['desde'])} a {ajustes.hora_mad(x['hasta'])})"


def _corredor_vivo() -> int | None:
    """El PID del corredor en marcha (lock con el PID y su hora de creación: recursos.mismo_proceso), o None."""
    from . import recursos
    try:
        d = json.loads(ajustes.LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = d.get("pid")
    if pid and pid != os.getpid() and recursos.mismo_proceso(pid, d.get("creado"), d.get("ts")):
        return int(pid)
    return None


def _validar(ruta: str) -> int:
    """Lo que usa el generador antes de enviar (15.3): sin medir resultados. Con prioridad baja; en una pausa de las
    aperturas no hace A8 ni A9 (los hará el corredor)."""
    from . import almacen, datos, etapas, hipotesis
    try:
        h = json.loads(open(ruta, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        print(f"NO VALE: JSON ilegible ({e})")
        return 1
    _bajo()
    firmas, fams = {}, None
    if ajustes.DB.exists():
        con = almacen.conectar(solo_lectura=True)
        firmas = {r[0]: r[1] for r in con.execute("SELECT firma, id FROM variantes")}
        fams = {}
        for n, v, s in con.execute("SELECT nombre, version, sha FROM familias"):
            fams.setdefault(n, {})[v] = s
    r = hipotesis.validar(h, firmas, fams)
    mot = list(r["motivos"])
    if r["ok"]:
        car = datos.cache_existente(datos.epoca_de())
        x = _pausa_de_aperturas()
        if car is None:
            print("Aviso: sin caché de la época; A8 (recorte con datos reales) y A9 (frecuencia) los hará el corredor")
        elif x:
            print(f"Aviso: {_texto_pausa(x)}: ahora no se calcula nada; A8 (recorte con datos reales) y A9 (frecuencia) "
                  "los hará el corredor después")
        else:
            tid = f"validar-{os.getpid()}"
            tr = {"id": tid, "cache": str(car),
                  "variantes": [{"vid": v["vid"], "familia": r["familia"], "params": v["params"], "salida": v["salida"],
                                 "exploratoria": r["exploratoria"]} for v in r["variantes"]]}
            ajustes.TRABAJOS.mkdir(parents=True, exist_ok=True)

            def pausa():
                etapas.comprobar_pausa()
                if _pausa_de_aperturas():
                    raise etapas.Pausado()
            try:
                res = etapas.admision(tr, None, pausa=pausa)
            except etapas.Pausado:
                res = {"variantes": (etapas.leer_punto(tid) or {}).get("variantes") or {}}
                print("Aviso: empieza una pausa de las aperturas (o el laboratorio se para): A8 y A9 de las variantes que "
                      "faltan los hará el corredor")
            for vid, x in res["variantes"].items():
                print(f"  {vid}: {x['senales_dia_dentro']} señales al día dentro de muestra, {x['segundos']} s; recorte: "
                      f"{(x.get('recorte') or {}).get('cortes_senal')} cortes en señales y "
                      f"{(x.get('recorte') or {}).get('cortes_azar')} al azar")
                mot += [f"{vid}: {m}" for m in x["motivos"]]
            try:
                etapas.ruta(tid, "punto.json").unlink()
            except OSError:
                pass
    if mot:
        print("NO VALE:")
        for m in mot:
            print("  -", m)
        return 1
    print(f"VALE: {len(r['variantes'])} variantes "
          f"({sum(1 for v in r['variantes'] if v['duplicada_de'])} duplicadas, "
          f"{sum(1 for v in r['variantes'] if v['fuera_de_reglas'])} fuera de reglas)")
    return 0


def _estado() -> int:
    from . import salida_web
    e = salida_web.leer(ajustes.ESTADO)
    if not e:
        print("El laboratorio vivo no está en marcha (no hay estado.json): scripts/lab-vivo.ps1")
        return 1
    edad = (ajustes.ahora_ms() - (e.get("latido") or 0)) / 1000
    print(f"Estado: {e.get('estado')}{' (PARADO: latido de hace ' + str(int(edad)) + ' s)' if edad > 180 else ''}")
    if e.get("motivo"):
        print(f"  {e['motivo']}")
    c = e.get("contadores") or {}
    print(f"  Hipótesis: {c.get('hipotesis')} · pruebas: {c.get('n')}")
    print(f"  Canario: {((e.get('codigo') or {}).get('canario') or {}).get('estado')}")
    print(f"  Meta: {(e.get('meta') or {}).get('texto')}")
    for x in e.get("en_curso") or []:
        print(f"  En curso: {x}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -X utf8 -m bot.lab_vivo", description="Laboratorio vivo (docs/07)")
    sub = ap.add_subparsers(dest="orden", required=True)
    c = sub.add_parser("correr")
    c.add_argument("--procesos", type=int, default=None)
    c.add_argument("--sin-pausas", action="store_true")
    c.add_argument("--una-vez", action="store_true")
    c.add_argument("--una-pasada", action="store_true", help="todo lo que haya en la cola, una vez: consolida y se para")
    c.add_argument("--max-horas", type=float, default=24.0, help="tope de una pasada (horas)")
    c.add_argument("--cpus", default=None, help="núcleos del laboratorio, p. ej. 3,4,5")
    c.add_argument("--memoria-mb", type=int, default=None, help="memoria total del laboratorio (Job Object)")
    t = sub.add_parser("tarea")
    t.add_argument("id")
    v = sub.add_parser("validar")
    v.add_argument("archivo")
    sub.add_parser("estado")
    sub.add_parser("canario")
    sub.add_parser("preparar-datos")
    sub.add_parser("consolidar")
    hn = sub.add_parser("heredar-n", help="la base tiene menos pruebas que n_acumulado.jsonl y el usuario lo acepta")
    hn.add_argument("--nota", required=True, help="por qué (queda en eventos, en pruebas_previas y en el registro)")
    sub.add_parser("sembrar")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ajustes.preparar_carpetas()
    if a.orden == "tarea":
        return _tarea(a.id)
    if a.orden == "correr":
        from .corredor import Corredor
        if a.cpus:
            ajustes.CPUS = tuple(int(x) for x in str(a.cpus).replace(";", ",").split(",") if x.strip().isdigit()) or ajustes.CPUS
        if a.memoria_mb:
            ajustes.MEMORIA_MB = max(300, int(a.memoria_mb))
        return Corredor(procesos=a.procesos, sin_pausas=a.sin_pausas, una_pasada=a.una_pasada,
                        max_horas=a.max_horas).correr(una_vez=a.una_vez)
    if a.orden == "validar":
        return _validar(a.archivo)
    if a.orden == "estado":
        return _estado()
    if a.orden in ("canario", "preparar-datos", "consolidar"):
        x = _pausa_de_aperturas()
        if x:
            print(f"No se empieza ahora: {_texto_pausa(x)}. Vuelve a intentarlo cuando acabe.")
            return ajustes.CODIGO_PAUSADO
    if a.orden == "consolidar" and _corredor_vivo():
        ajustes.CONSOLIDAR_AHORA.write_text(json.dumps({"ts": ajustes.ahora_ms()}), encoding="utf-8")
        print(f"El corredor está en marcha (PID {_corredor_vivo()}) y es el único que escribe en lab_vivo.db: la "
              "consolidación la hará él en su próxima vuelta, fuera de las pausas")
        return 0
    if a.orden in ("canario", "preparar-datos", "consolidar"):
        _bajo()
    if a.orden == "canario":
        from . import canario
        r = canario.correr()
        print(r.get("texto") or json.dumps(r, ensure_ascii=False))
        return 0 if r.get("estado") == "ok" else 1
    if a.orden == "preparar-datos":
        from . import datos
        fu = datos.preparar_epoca()
        print(f"Caché de la época {fu.periodo.get('epoca')} en {fu.carpeta} (firma {fu.firma})")
        return 0
    if a.orden == "consolidar":
        from . import registro_n
        from .corredor import Corredor
        c = Corredor()
        mot = registro_n.comprobar(c.con)
        if mot:
            print(f"No se consolida: {mot}")
            return 2
        cons = c.consolidar()
        print(f"Consolidación hecha: N efectivo {cons['n_efectivo']:.1f}, N_dsr {cons['n_dsr']:.0f}, m de BH {cons['bh_m']}")
        return 0
    if a.orden == "heredar-n":
        if _corredor_vivo():
            tmp = ajustes.HEREDAR_N.with_name(ajustes.HEREDAR_N.name + ".tmp")
            tmp.write_text(json.dumps({"nota": a.nota, "por": "usuario", "ts": ajustes.ahora_ms()}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, ajustes.HEREDAR_N)
            print("El corredor está en marcha: aplicará la herencia en su próxima vuelta (mira estado.json)")
            return 0
        from .corredor import Corredor
        r = Corredor().aplicar_herencia(a.nota)
        print("No falta nada: la base no está por debajo de n_acumulado.jsonl" if r is None else
              f"Heredadas {r['n']} variantes medidas (N efectivo {r['n_efectivo']}) como previas: {r['nota']}")
        return 0
    if a.orden == "sembrar":
        from . import almacen, catalogo
        ya = set()
        if ajustes.DB.exists():
            con = almacen.conectar(solo_lectura=True)
            ya = {r[0] for r in con.execute("SELECT id FROM hipotesis")}
        for car in (ajustes.ACEPTADAS, ajustes.RECHAZADAS, ajustes.ENTRADA):
            ya |= {p.stem for p in car.glob("*.json")} if car.exists() else set()
        hs, pend = catalogo.catalogo()
        escritas = catalogo.escribir(catalogo.siembra() + hs, ya=ya)
        catalogo.escribir_pendientes(pend)
        print(f"Sembradas {len(escritas)} hipótesis en {ajustes.ENTRADA}; pendientes: {', '.join(sorted(pend))}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
