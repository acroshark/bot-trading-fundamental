"""Filtro de relevancia por reglas (sin IA): universo, temas de mercado, frescura, opinión y detección de duplicados."""
import re
from dataclasses import dataclass, field

from . import config
from .topics import TOPIC_NAMES, analyze, normalize_lang

COIN_RX = {
    "BTC": re.compile(r"\b(?:bitcoin|btc|xbt)\b|비트코인|ビットコイン|比特币|比特幣|биткоин|биткойн|біткоїн|بيتكوين|ביטקוין", re.I),
    "ETH": re.compile(r"\b(?:ethereum|ether|eth)\b|이더리움|イーサリアム|以太坊|эфириум|ефіріум|إيثريوم", re.I),
    "SOL": re.compile(r"\b(?:solana|sol)\b|솔라나|ソラナ|索拉纳|索拉納|солана", re.I),
    "XRP": re.compile(r"\b(?:xrp|ripple)\b|리플|リップル|瑞波", re.I),
}
# Temas que mueven a todo el mercado cripto aunque no nombren una moneda.
MACRO_RX = re.compile(
    r"\b(?:cpi|inflation|fomc|federal reserve|the fed|fed's|powell|rate cuts?|rate hikes?|interest rates?|payrolls?|"
    r"nonfarm|jobless claims|unemployment rate|gdp|pce|treasury yields?|tariffs?|recession|crypto|cryptocurrenc(?:y|ies)|"
    r"digital assets?|stablecoins?|tether|usdt|usdc|spot etfs?|crypto etfs?|mica|cftc|liquidations?)\b", re.I)
# Temas fuertes (mueven a todo el mercado en horas) frente al resto, que es contexto de cada bolsa.
STRONG_TOPICS = {"banco_central", "inflacion", "empleo", "aranceles", "guerra", "sanciones", "cripto", "regulacion"}
# Fuentes oficiales: solo cuenta lo que mueve mercados, no cada nota institucional.
OFFICIAL_RX = {
    "fed": re.compile(r"fomc|monetary policy|federal funds|interest rate|discount rate|minutes|balance sheet|"
                      r"economic projections|beige book|powell|testimony|stablecoin|crypto|digital asset", re.I),
    "bls": re.compile(r"consumer price|cpi|employment situation|payroll|producer price|ppi|job openings|jolts|"
                      r"unemployment|employment cost|real earnings", re.I),
    "ecb": re.compile(r"monetary policy|interest rates?|press conference|lagarde|inflation|digital euro|crypto|stablecoin", re.I),
    "sec": re.compile(r"crypto|digital asset|bitcoin|ether|solana|xrp|stablecoin|token|blockchain|etf|exchange-traded", re.I),
}
OPINION_RX = re.compile(
    r"price predictions?|price analysis|here'?s why|here is why|what'?s next for|top \d+ (?:coins|altcoins|cryptos?|tokens)|"
    r"\bpredicts?\b|^prediction:|investors should|should you (?:buy|sell)|stocks? to buy|best \w+ to buy|"
    r"\bmy (?:top|favorite) |\?\s*$|？\s*$", re.I)
TICKER_RX = re.compile(r"\(([A-Z0-9]{2,10})\)")
STOP = set("the and for with from that this will has have are was its into over after says said new amid than as at by "
           "of on in to a an is be or not but out up de la el en y los las del que por con para una se su al lo "
           "der die das und mit von den für auf ist dem des zu im do da em os as um uma no na".split())
WORD_RX = re.compile(r"\w+", re.U)
# Escrituras sin espacios entre palabras: chino, japonés (kanji y kana) y tailandés. Se comparan por pares de caracteres.
CJK_RX = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿฀-๿]+")


@dataclass
class Decision:
    relevant: bool
    coins: list[str]
    reason: str
    macro: bool = False
    fresh: bool = True
    topics: list[str] = field(default_factory=list)
    strong: bool = False   # monedas, macro o tema fuerte: va antes a la IA local


def detect_coins(text: str, hints: list[str] = ()) -> list[str]:
    found = [c for c, rx in COIN_RX.items() if rx.search(text)]
    for h in hints:
        if h in config.COINS and h not in found:
            found.append(h)
    return [c for c in config.COINS if c in found]


def _age_text(age_s: float) -> str:
    mins = int(age_s // 60)
    return f"{mins} min" if mins < 120 else f"{mins // 60} h"


def classify(title: str, source_id: str, age_s: float | None, hints: list[str] = (), symbols: list[str] = (),
             lang: str | None = None) -> Decision:
    """Decide si una noticia pasa al siguiente agente y explica por qué.

    Fresca (hasta FRESH_MAX_S): puede disparar operaciones. Contexto (hasta CONTEXT_MAX_S): se captura y se puntúa
    para preparar aperturas y medir el sentimiento de cada bolsa. Más antigua: se descarta."""
    info = analyze(title, normalize_lang(lang))
    tps = info["topics"]
    fresh = age_s is None or age_s <= config.FRESH_MAX_S
    if age_s is not None and age_s > config.CONTEXT_MAX_S:
        return Decision(False, detect_coins(title, hints), f"Antigua: se publicó hace {_age_text(age_s)}", fresh=False, topics=tps)
    if OPINION_RX.search(title):
        return Decision(False, detect_coins(title, hints), "Opinión o predicción, no es un hecho", fresh=fresh, topics=tps)
    family = next((k for k in OFFICIAL_RX if source_id.startswith(k)), None)
    if family and not OFFICIAL_RX[family].search(title):
        return Decision(False, [], "Nota institucional sin efecto previsible en el mercado", fresh=fresh, topics=tps)
    coins = detect_coins(title, hints)
    macro = bool(MACRO_RX.search(title)) or family in ("fed", "bls", "ecb")
    if coins:
        return Decision(True, coins, "Menciona " + ", ".join(coins), macro, fresh, tps, strong=True)
    if macro:
        return Decision(True, list(config.COINS), "Tema macro o regulatorio: afecta a todo el universo", True, fresh, tps, strong=True)
    if info["relevant"]:
        names = ", ".join(TOPIC_NAMES[t].lower() for t in tps[:3])
        strong = bool(STRONG_TOPICS & set(tps))
        return Decision(True, list(config.COINS), f"Tema de mercado: {names}", strong, fresh, tps, strong=strong)
    if info["noise"]:
        return Decision(False, [], info["noise"], fresh=fresh, topics=tps)
    others = [s for s in list(symbols) + TICKER_RX.findall(title) if s not in config.COINS]
    extra = f" (habla de {', '.join(dict.fromkeys(others[:3]))})" if others else ""
    return Decision(False, [], "Fuera del universo y sin tema de mercado" + extra, fresh=fresh, topics=tps)


def tokens(title: str) -> set[str]:
    """Palabras significativas; en chino y japonés, pares de caracteres (no separan palabras con espacios)."""
    low = title.lower()
    out = {w for w in WORD_RX.findall(CJK_RX.sub(" ", low)) if len(w) >= 3 and w not in STOP and not w.isdigit()}
    for run in CJK_RX.findall(low):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def similar(a: set[str], b: set[str]) -> bool:
    """Misma noticia contada con palabras parecidas (titulares de relays y medios)."""
    if len(a) < 3 or len(b) < 3:
        return False
    inter = len(a & b)
    return inter / len(a | b) >= 0.5 or (min(len(a), len(b)) >= 4 and inter / min(len(a), len(b)) >= 0.75)
