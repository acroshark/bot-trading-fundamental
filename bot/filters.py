"""Filtro de relevancia por reglas (sin IA): universo, temas de mercado, frescura, opinión y detección de duplicados.

Tres clases (28-10-2026, lo que pidió el usuario: solo cripto y lo que de verdad puede mover a la cripto):
- «cripto»: nombra BTC, ETH, SOL o XRP (van esas monedas, y solo esas) o habla de cripto en general (sin monedas).
- «macro»: tema macro o geopolítico fuerte (bancos centrales y tipos, inflación, aranceles, guerra, sanciones, datos de
  empleo y PIB de las grandes economías, crisis financieras, estrechos y bloqueos, ensayos nucleares, ataques contra un
  país, golpes de Estado, pandemias y choques del petróleo: topics.FUERTE). Va sin monedas y marcada macro; la cripto
  tiene que reflejarla (bot/reflejo.py) para que cuente en la señal.
- «contexto»: el resto de temas de mercado (política, empresas, tecnología, sucesos, economía local…). Se capta para
  medir las fuentes y ver el contexto, pero ya no va a la IA local ni lleva monedas.
Todo lo demás (ruido, opinión, antiguo, fuera del universo) se descarta como siempre."""
import re
from dataclasses import dataclass, field

from . import config
from .topics import _MASKS, TOPIC_NAMES, _clean, analyze, normalize_lang

COIN_RX = {
    "BTC": re.compile(r"\b(?:bitcoin|btc|xbt)\b|비트코인|ビットコイン|比特币|比特幣|биткоин|биткойн|біткоїн|بيتكوين|ביטקוין", re.I),
    "ETH": re.compile(r"\b(?:ethereum|ether|eth)\b|이더리움|イーサリアム|以太坊|эфириум|ефіріум|إيثريوم", re.I),
    # «sol» es también el sol del español y del portugués («acampada en Sol», el sol peruano): suelto solo cuenta en
    # mayúsculas, como $SOL o con contexto cripto (SOL_LOOSE y CRYPTO_CTX en detect_coins)
    "SOL": re.compile(r"\bsolana\b|(?-i:\bSOL\b)|\$sol\b|솔라나|ソラナ|索拉纳|索拉納|солана", re.I),
    "XRP": re.compile(r"\b(?:xrp|ripple)\b|리플|リップル|瑞波", re.I),
}
# Cripto en general, aunque no nombre una de las cuatro monedas (además del tema «cripto» de topics.py), con los
# exchanges: «Bithumb Says Internal Mobile System Hacked» es de cripto aunque no lo diga.
CRYPTO_RX = re.compile(
    r"\b(?:crypto\w*|cryptocurrenc(?:y|ies)|digital assets?|stablecoins?|tether|usdt|usdc|spot (?:bitcoin |ether )?etfs?|"
    r"crypto etfs?|mica|defi|altcoins?|memecoins?|binance|coinbase|kraken(?! robotics)|okx|bybit|bitget|upbit|bithumb|"
    r"bitvavo|bitstamp|bitfinex|kucoin|coinone|korbit)\b", re.I)
# Contexto que hace de «sol» suelto la moneda SOL.
SOL_LOOSE = re.compile(r"\bsol\b", re.I)
CRYPTO_CTX = re.compile(r"\b(?:tokens?|cripto\w*|crypto\w*|altcoins?|memecoins?|etfs?|blockchain|staking|defi)\b", re.I)
# Macro en inglés que mueve a todo el mercado aunque el titular no traiga idioma (los temas de topics.py la cubren en 14).
MACRO_RX = re.compile(
    r"\b(?:inflation|fomc|federal reserve|the fed|fed's|powell|rate cuts?|rate hikes?|interest rates?|payrolls?|"
    r"nonfarm|jobless claims|unemployment rate|gdp|pce|treasury yields?|tariffs?|recession)\b", re.I)
INFLACION_MASK = _MASKS["inflacion"]
# «CPI» solo en inglés: en portugués es una comisión parlamentaria y en español, la Corte Penal Internacional.
CPI_RX = re.compile(r"\bcpi\b", re.I)
# Temas de topics.py que son macro o geopolítica fuerte enteros. De empleo, macro y banca solo cuentan las clases de
# topics.FUERTE (datos de empleo, macro fuerte, crisis financiera); la regulación solo si es de cripto (clase cripto).
MACRO_TOPICS = {"banco_central", "inflacion", "aranceles", "guerra", "sanciones"}
STRONG_TOPICS = MACRO_TOPICS | {"cripto"}   # compatibilidad: temas que van antes a la IA local
FUERTE_NAMES = {"datos_empleo": "datos de empleo", "macro_fuerte": "macro fuerte", "crisis_financiera": "crisis financiera",
                "geopolitica_fuerte": "geopolítica fuerte", "energia_shock": "choque de energía"}
CLASES = ("cripto", "macro", "contexto")
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
    strong: bool = False   # cripto o macro fuerte: va antes a la IA local
    clase: str | None = None   # cripto | macro | contexto (solo las relevantes); el contexto no va a la IA local


def detect_coins(text: str, hints: list[str] = ()) -> list[str]:
    found = [c for c, rx in COIN_RX.items() if rx.search(text)]
    if "SOL" not in found and SOL_LOOSE.search(text) and (found or CRYPTO_RX.search(text) or CRYPTO_CTX.search(text)):
        found.append("SOL")   # «sol» con contexto cripto (otra moneda, un exchange, token, ETF…)
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
    lang_key = normalize_lang(lang)
    plain = INFLACION_MASK.sub(" ", _clean(title))   # «taxes indexées à l'inflation» no es un dato de inflación
    macro = (bool(MACRO_RX.search(plain)) or family in ("fed", "bls", "ecb")
             or (lang_key in (None, "en") and bool(CPI_RX.search(plain))))
    if coins:
        return Decision(True, coins, "Menciona " + ", ".join(coins), macro, fresh, tps, strong=True, clase="cripto")
    if "cripto" in tps or CRYPTO_RX.search(title):
        return Decision(True, [], "Cripto en general (no nombra una de las cuatro monedas)", macro, fresh, tps,
                        strong=True, clase="cripto")
    fuerte = [t for t in tps if t in MACRO_TOPICS] + list(info.get("fuerte") or [])
    if fuerte or macro:
        names = ", ".join(dict.fromkeys(TOPIC_NAMES.get(t, FUERTE_NAMES.get(t, t)).lower() for t in fuerte[:3]))
        return Decision(True, [], f"Macro o geopolítica fuerte ({names or 'macro'}): la cripto tiene que reflejarla",
                        True, fresh, tps, strong=True, clase="macro")
    if info["relevant"]:
        names = ", ".join(TOPIC_NAMES[t].lower() for t in tps[:3])
        return Decision(True, [], f"Contexto ({names}): no es cripto ni macro fuerte; se guarda sin IA", False, fresh,
                        tps, strong=False, clase="contexto")
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
