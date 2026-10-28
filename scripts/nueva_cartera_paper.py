"""Empieza una cartera paper nueva y archiva la anterior sin borrar nada.

Uso (con el bot PARADO):
    M:\\BotFundamental-runtime\\venv\\Scripts\\python.exe scripts\\nueva_cartera_paper.py --eur 100
    ... --usdc 250          (capital directamente en USDC)

- Las operaciones (`trades`) pasan a `trades_archivo_<fecha>` y el estado (`paper_state`) a `paper_state_archivo_<fecha>`.
- El capital nuevo queda guardado en paper_state.start_equity, que manda sobre BOT_PAPER_EQUITY en los arranques siguientes.
- Con --eur se convierte a USDC al cambio EUR/USDC de Binance en ese momento y se apunta en paper_state.cuenta.
- Todo es dinero ficticio: solo paper.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config  # noqa: E402


def eur_usdc() -> float:
    try:
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    url = "https://api.binance.com/api/v3/ticker/price?symbol=EURUSDC"
    return float(json.loads(urllib.request.urlopen(url, timeout=15).read())["price"])


def bot_vivo() -> bool:
    try:
        urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}/health", timeout=2)
        return True
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--eur", type=float, help="capital en euros (se convierte a USDC al cambio actual)")
    g.add_argument("--usdc", type=float, help="capital en USDC")
    ap.add_argument("--nombre", default="", help="nombre de la cartera (por defecto «Cartera de prueba de N €»)")
    a = ap.parse_args()
    if bot_vivo():
        raise SystemExit("El bot está en marcha: páralo antes de cambiar de cartera.")
    rate = eur_usdc() if a.eur is not None else None
    usdc = round(a.eur * rate, 2) if a.eur is not None else round(a.usdc, 2)
    if usdc <= 0:
        raise SystemExit("El capital tiene que ser positivo.")
    now = int(time.time() * 1000)
    tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    cuenta = {"nombre": a.nombre or (f"Cartera de prueba de {a.eur:g} €" if a.eur is not None else f"Cartera de prueba de {usdc:g} USDC"),
              "eur": a.eur, "eur_usdc": rate, "usdc": usdc, "desde": now, "ficticia": True}

    db = sqlite3.connect(config.DB_PATH)
    with db:
        tablas = {n for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("trades", "paper_state"):
            if t in tablas:
                db.execute(f"ALTER TABLE {t} RENAME TO {t}_archivo_{tag}")
        db.execute("DROP INDEX IF EXISTS trades_closed")   # al renombrar, el índice se queda en la tabla archivada
        db.execute("CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, story TEXT, opened INTEGER, closed INTEGER, body TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS trades_closed ON trades(closed)")
        db.execute("CREATE TABLE IF NOT EXISTS paper_state(key TEXT PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO paper_state VALUES ('start_equity', ?)", (json.dumps(usdc),))
        db.execute("INSERT INTO paper_state VALUES ('cuenta', ?)", (json.dumps(cuenta, ensure_ascii=False),))
    db.close()
    print(f"{cuenta['nombre']}: {usdc:.2f} USDC" + (f" (1 € = {rate:.4f} USDC)" if rate else ""))
    print(f"La cartera anterior queda en trades_archivo_{tag} y paper_state_archivo_{tag}.")


if __name__ == "__main__":
    main()
