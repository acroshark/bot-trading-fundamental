"""Cambia la estrategia del motor de aperturas por decisión del usuario (SOLO PAPER: nunca opera con dinero real).

Uso (con el bot PARADO y también su vigilante: scripts/vigilante-bot.ps1 relanza el bot unos 2 min después de que caiga;
antes de pararlo, crear el archivo DATA_DIR\\parar-vigilante y borrarlo después de volver a arrancar):
    M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe -X utf8 scripts\\activar_estrategia.py --preset seis
    M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe -X utf8 scripts\\activar_estrategia.py --preset impulso
«seis» activa la estrategia «6 diarias» (apertura.SEIS); «impulso» vuelve al preset de siempre (E2, apertura.IMPULSO).

Qué hace:
1. Se niega si el bot responde en /health (el historial se escribe en bot.db, que es del bot mientras está en marcha) o si
   DATA_DIR no tiene bot.db (un --datos equivocado no crea una base vacía ni escribe la estrategia donde no es).
2. Escribe DATA_DIR/estrategia_activa.json (escritura atómica) con la firma que exige la evolución
   (evolucion.validar_activa: estrategia «impulso», params, solo paper y las reglas del usuario: stop ≥ 1,5 %, ≥ 2R,
   ≥ 3 % neto de costes, sin break-even ni stop dinámico). Si no las cumple, no escribe nada.
3. Apunta el cambio en el historial de la evolución (evolution_log, tipo «estrategia_cambiada», origen «usuario»): «Estrategia
   cambiada por decisión del usuario el DD-MM-AAAA: 6 diarias (…)», con la estrategia entera antes y después (se puede
   revertir desde la pestaña Evolución) y las cifras del backtest si están. También marca el último cambio de
   estrategia (la evolución automática espera 14 días para promocionar otra). Los dos apuntes van en una sola
   transacción: si falla cualquiera, no queda ninguno y el archivo vuelve a como estaba.
El motor lo carga al arrancar (y en caliente entre aperturas). Con --simular solo enseña lo que escribiría.
"""
import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import apertura, config, evolucion  # noqa: E402
from bot.resumen_backtest import SEIS_FILE  # noqa: E402

MAD = ZoneInfo("Europe/Madrid")
BACKTEST = {"seis": SEIS_FILE, "impulso": "impulso_7d.json"}


def bot_en_marcha(url: str, timeout: float = 2.0) -> bool:
    """¿Responde algo en url? Cualquier respuesta HTTP (también un error) quiere decir que el bot está en marcha."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 nunca por un proxy
    try:
        with opener.open(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _backtest(datos: Path, preset: str) -> dict | None:
    """Lo esencial del backtest del preset (fuera y dentro de muestra), si está."""
    try:
        d = json.loads((datos / "backtest" / BACKTEST[preset]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = {"archivo": BACKTEST[preset]}
    for part in ("fuera", "dentro"):
        b = d.get(part) if isinstance(d.get(part), dict) else {}
        out[part] = {k: b.get(k) for k in ("n", "mean_R", "win_rate", "horas_media")}
    return out


def _txt_bt(bt: dict | None) -> str:
    f = (bt or {}).get("fuera") or {}
    if f.get("n") is None or f.get("mean_R") is None:
        return ""
    r = f"{f['mean_R']:+.2f}".replace(".", ",")
    veredicto = "no es una ventaja" if f["mean_R"] <= 0 else "no es una ventaja demostrada"
    return f" Backtest de 3 años fuera de muestra: R medio {r} en {f['n']} operaciones ({veredicto}); se mide en paper."


def construir(preset: str, ahora: int, eid: str, bt: dict | None = None, extra: str | None = None) -> dict:
    """El contenido de estrategia_activa.json para el preset (sin escribirlo)."""
    info = apertura.PRESETS[preset]
    fecha = datetime.fromtimestamp(ahora / 1000, MAD).strftime("%d-%m-%Y")
    if preset == "impulso":   # el preset de siempre: params vacíos (el motor usa IMPULSO y la evolución, su gemelo)
        d = evolucion.activa_por_defecto()
    else:
        d = {"version": 2, "estrategia": "impulso", "fuente": "usuario",
             "params": {k: v for k, v in info["params"].items() if k not in ("estrategia", "lab")},
             "lab_id": None, "lab_twin": None, "twin_filtro": None, "codigo": None, "pausada": False}
    motivo = (f"Estrategia cambiada por decisión del usuario el {fecha}: {info['nombre']} ({info['descripcion']}). "
              "Solo paper." + _txt_bt(bt) + (f" {extra.strip()}" if extra and extra.strip() else ""))
    d.update(preset=preset, nombre=info["nombre"], descripcion=info["descripcion"], desde=ahora, actualizado=ahora,
             motivo=motivo, registro=eid, solo_paper=True)
    d["firma"] = evolucion.firma_estrategia(d)
    return d


def _actual(path: Path) -> dict:
    return evolucion.leer_estrategia_activa(path) or evolucion.activa_por_defecto()


def activar(preset: str, datos: Path, ahora: int | None = None, extra: str | None = None,
            simular: bool = False) -> dict:
    """Escribe la estrategia y la apunta en el historial. {"ok", "why", "activa", "registro"}."""
    if preset not in apertura.PRESETS:
        return {"ok": False, "why": f"preset desconocido: {preset} (uno de {', '.join(apertura.PRESETS)})"}
    ahora = int(datetime.now(MAD).timestamp() * 1000) if ahora is None else int(ahora)
    path = datos / evolucion.ARCHIVO
    antes = _actual(path)
    eid = f"v{ahora}-usuario-{preset}"
    bt = _backtest(datos, preset)
    nueva = construir(preset, ahora, eid, bt, extra)
    ok, why = evolucion.validar_activa(nueva)
    if not ok:
        return {"ok": False, "why": f"no se escribe: {why}"}
    if antes.get("preset") == preset and evolucion.firma(antes) == evolucion.firma(nueva) \
            and bool(antes.get("pausada")) == bool(nueva.get("pausada")):
        return {"ok": True, "why": f"«{apertura.PRESETS[preset]['nombre']}» ya era la estrategia activa: nada que "
                                   "cambiar", "activa": antes, "registro": None}
    if simular:
        return {"ok": True, "why": "simulación: no se ha escrito nada", "activa": nueva, "registro": None}
    ruta_db = datos / "bot.db"
    if not ruta_db.is_file():
        return {"ok": False, "why": f"no hay bot.db en {datos}: ¿es la carpeta de datos del bot? (no se cambia nada)"}
    previo = path.read_bytes() if path.exists() else None
    evolucion.escribir_activa(path, nueva)
    try:
        # mode=rw: nunca crea una base nueva. Los dos apuntes en una transacción: el último cambio primero (sin
        # confirmar) y registrar() confirma los dos a la vez; si algo falla, se deshace todo.
        db = sqlite3.connect(f"file:{urllib.parse.quote(ruta_db.resolve().as_posix())}?mode=rw", uri=True, timeout=30)
        try:
            evolucion.asegurar_tablas(db)
            ev = {"por": "usuario", "preset": preset, "firma_antes": evolucion.firma_estrategia(antes),
                  "firma_despues": nueva["firma"], "backtest": bt, "script": "scripts/activar_estrategia.py"}
            db.execute("INSERT OR REPLACE INTO evolution_state VALUES (?, ?)", ("ultimo_cambio", json.dumps(ahora)))
            evolucion.registrar(db, "estrategia_cambiada", nueva["nombre"], antes, nueva, nueva["motivo"], ev,
                                "usuario", ts=ahora, id_=eid)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as e:   # sin apunte en el historial, el archivo vuelve a como estaba
        if previo is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previo)
        return {"ok": False, "why": f"no pude apuntarlo en el historial ({e}): se deja como estaba"}
    return {"ok": True, "why": None, "activa": nueva, "registro": eid}


def main(argv: list[str] | None = None, ahora: int | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cambia la estrategia del motor de aperturas (solo paper).")
    ap.add_argument("--preset", required=True, choices=sorted(apertura.PRESETS),
                    help="seis = «6 diarias»; impulso = volver a E2 (el preset de siempre)")
    ap.add_argument("--datos", type=Path, default=config.DATA_DIR, help="carpeta de datos (por defecto DATA_DIR)")
    ap.add_argument("--url", default=f"http://{config.HOST}:{config.PORT}/health",
                    help="dirección de /health del bot (si responde, no se cambia nada)")
    ap.add_argument("--motivo", default=None, help="texto que se añade al motivo del historial")
    ap.add_argument("--simular", action="store_true", help="solo enseña lo que escribiría")
    a = ap.parse_args(argv)
    if bot_en_marcha(a.url):
        print(f"El bot responde en {a.url}: páralo antes de cambiar la estrategia (no se ha cambiado nada). Para que "
              f"el vigilante no lo vuelva a arrancar, crea antes {a.datos / 'parar-vigilante'}.")
        return 2
    res = activar(a.preset, a.datos, ahora, a.motivo, a.simular)
    if not res["ok"]:
        print(f"No se ha cambiado la estrategia: {res['why']}")
        return 1
    act = res.get("activa") or {}
    print(res["why"] or f"Estrategia activa: {act.get('nombre')} (firma {act.get('firma')}, registro "
                        f"{res['registro']}) en {a.datos / evolucion.ARCHIVO}")
    if a.simular:
        print(json.dumps(act, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
