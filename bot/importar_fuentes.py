"""Importa lo que da por bueno el verificador de feeds (*-out.json) al registro bot/fuentes/<región>.json.

Uso: python -m bot.importar_fuentes <carpeta con *-out.json> [--destino carpeta]

La región sale del prefijo del archivo (jp-…-out.json, latam-…-out.json). Se quitan los ok=false, se deduplica por URL
normalizada (Google News, por consulta) y cada feed recibe un id estable «<región>-<host>-<hash6>» que sale solo de la URL.
Si el registro de destino ya tiene esa URL, se conserva su id aunque cambie el nombre o la región: así no se pierde su
feed_state ni las claves de vistos de la mesa (que repetirían en el arranque todo lo de las últimas horas).

Los feeds que el bot descubre solo (bot/fuentes_vivas.py, con "origen": "descubierta") se añaden con anadir() y una
importación nueva los conserva en su región aunque el verificador no los traiga."""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from . import config, regions
from .feeds import GOOGLE_NEWS_HOST, TIERS, norm_url

PREFIXES = ("jp", "cn", "hk", "kr", "de", "uk", "es", "us", "latam", "me")
DISCOVERED = "descubierta"   # valor de "origen" de los feeds que añade fuentes_vivas


def slug(text: str, maxlen: int = 32) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:maxlen].rstrip("-")


def make_id(region: str, url: str) -> str:
    """Id nuevo: host y hash de la URL normalizada. No depende del nombre, que el verificador puede retocar."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    base = "gnews" if host == GOOGLE_NEWS_HOST else slug(host) or "feed"
    return f"{region}-{base}-{hashlib.sha1(norm_url(url).encode()).hexdigest()[:6]}"


def existing_ids(dest: Path) -> dict[str, str]:
    """Ids del registro que ya existe: URL normalizada → id."""
    out: dict[str, str] = {}
    for path in sorted(Path(dest).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for d in data if isinstance(data, list) else []:
            if isinstance(d, dict) and d.get("id") and d.get("url"):
                try:
                    out.setdefault(norm_url(str(d["url"])), str(d["id"]))
                except ValueError:
                    continue
    return out


def _read_list(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, list) else []


def _write(path: Path, entries: list[dict]) -> None:
    """Un feed por línea (cómodo de revisar a mano). Atómico: se escribe aparte y se sustituye de una vez, para que
    load_registry nunca lea un archivo a medias."""
    text = "[\n" + ",\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n]\n"
    tmp = path.with_name(path.name + ".tmp")   # *.json.tmp no lo lee load_registry
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _discovered(path: Path) -> list[dict]:
    """Feeds descubiertos por el bot que ya están en ese archivo de región."""
    try:
        data = _read_list(path)
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("origen") == DISCOVERED and d.get("id") and d.get("url")]


def anadir(entry: dict, dest: Path = config.FEEDS_DIR) -> bool:
    """Añade un feed al <región>.json del registro si su URL no está ya en ningún archivo. Lo usa fuentes_vivas con
    los feeds que descubre (entry con id, name, url, region y "origen": "descubierta"). Devuelve True si lo añadió."""
    dest = Path(dest)
    url, region = str(entry.get("url") or ""), str(entry.get("region") or "")
    if not entry.get("id") or not url.startswith(("http://", "https://")) or region not in regions.REGIONS:
        raise ValueError(f"entrada no válida para el registro: {entry!r}"[:200])
    if norm_url(url) in existing_ids(dest):
        return False
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{region}.json"
    entries = _read_list(path) if path.exists() else []   # un archivo roto lanza ValueError: no se pisa
    _write(path, entries + [entry])
    return True


def _entry(d: dict, region: str, known: dict[str, str]) -> dict:
    url = str(d["url"]).strip()
    name = " ".join(str(d.get("name") or "").split()) or (urlsplit(url).hostname or url)
    iv = d.get("interval_s")
    ok_iv = isinstance(iv, (int, float)) and not isinstance(iv, bool) and iv > 0
    return {"id": known.get(norm_url(url)) or make_id(region, url), "name": name, "url": url,
            "lang": d.get("lang") or None, "country": d.get("country") or None, "region": region,
            "kind": str(d.get("kind") or "rss"), "tier": d.get("tier") if d.get("tier") in TIERS else "T3",
            "interval_s": float(iv) if ok_iv else None}


def importar(src: Path, dest: Path = config.FEEDS_DIR) -> dict:
    """Escribe un <región>.json por cada región con datos. Devuelve el recuento por región y por tipo."""
    dest = Path(dest)
    known = existing_ids(dest)
    by_region: dict[str, list[dict]] = {}
    seen: set[str] = set()
    dropped = dupes = 0
    skipped: list[str] = []
    for path in sorted(Path(src).glob("*-out.json")):
        region = path.name.split("-", 1)[0].lower()
        if region not in PREFIXES:
            skipped.append(path.name)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))   # admite BOM (Bloc de notas, PowerShell)
        except (OSError, ValueError):
            skipped.append(path.name)
            continue
        if not isinstance(data, list):
            skipped.append(path.name)
            continue
        for d in data:
            if not isinstance(d, dict) or not d.get("ok") or not str(d.get("url") or "").startswith(("http://", "https://")):
                dropped += 1
                continue
            try:
                key = norm_url(str(d["url"]))
            except ValueError:   # puerto o host mal escritos
                dropped += 1
                continue
            if key in seen:
                dupes += 1
                continue
            seen.add(key)
            by_region.setdefault(region, []).append(_entry(d, region, known))

    dest.mkdir(parents=True, exist_ok=True)
    kept = 0
    for region, entries in by_region.items():   # lo que el bot descubrió solo sigue en su región
        for d in _discovered(dest / f"{region}.json"):
            try:
                key = norm_url(str(d["url"]))
            except ValueError:
                continue
            if key not in seen:
                seen.add(key)
                entries.append(d)
                kept += 1
    kinds: Counter = Counter()
    used: set[str] = set()   # en todas las regiones: load_registry descarta ids repetidos
    for region, entries in by_region.items():
        for e in entries:   # choque de id (mismo host y mismo hash6): casi imposible, pero no debe pisar a otro
            base, n = e["id"], 2
            while e["id"] in used:
                e["id"], n = f"{base}-{n}", n + 1
            used.add(e["id"])
            if e.get("origen") != DISCOVERED:
                kinds[e["kind"]] += 1
        entries.sort(key=lambda e: (str(e.get("tier") or "T3"), str(e.get("name") or "").casefold(), e["id"]))
        _write(dest / f"{region}.json", entries)
    counts = {r: sum(e.get("origen") != DISCOVERED for e in by_region[r]) for r in PREFIXES if r in by_region}
    return {"regions": counts, "kinds": dict(kinds.most_common()), "total": sum(counts.values()), "dropped": dropped,
            "dupes": dupes, "skipped": skipped, "kept": kept, "dest": str(dest)}


def report(res: dict) -> str:
    lines = ["Feeds importados por región:"]
    for r, n in res["regions"].items():
        name, market = regions.REGIONS[r]
        lines.append(f"  {r:<6} {n:>5}  {name}" + (f" → {regions.MARKETS[market].name}" if market else ""))
    lines.append("Por tipo: " + (" · ".join(f"{k} {n}" for k, n in res["kinds"].items()) or "ninguno"))
    lines.append(f"Total: {res['total']} feeds en {res['dest']} "
                 f"({res['dropped']} con ok=false o sin URL y {res['dupes']} repetidos descartados)")
    if res.get("kept"):
        lines.append(f"Se conservan {res['kept']} feeds que el bot descubrió solo")
    if res["skipped"]:
        lines.append("Archivos ignorados (prefijo desconocido o ilegibles): " + ", ".join(res["skipped"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot.importar_fuentes",
                                 description="Importa el resultado del verificador de feeds al registro bot/fuentes.")
    ap.add_argument("carpeta", type=Path, help="carpeta con los *-out.json del verificador")
    ap.add_argument("--destino", type=Path, default=config.FEEDS_DIR, help="carpeta del registro (por defecto bot/fuentes)")
    a = ap.parse_args(argv)
    if not a.carpeta.is_dir():
        print(f"No existe la carpeta {a.carpeta}", file=sys.stderr)
        return 2
    print(report(importar(a.carpeta, a.destino)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
