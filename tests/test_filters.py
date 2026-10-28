import pytest

from bot.filters import classify, similar, tokens


def test_menciona_moneda_del_universo():
    d = classify("Spot bitcoin ETFs see $610M in net inflows", "coindesk", 30)
    assert d.relevant and d.coins == ["BTC"]


def test_varias_monedas_en_orden_del_universo():
    d = classify("XRP and Solana lead gains as Ether lags", "theblock", 10)
    assert d.coins == ["ETH", "SOL", "XRP"]


def test_dato_oficial_del_bls_es_macro_sin_monedas():
    """Hallazgo n.º 2: ya no se reparten las cuatro monedas a todo. Un dato macro va marcado macro y sin monedas (la
    cripto tiene que reflejarlo: bot/reflejo.py)."""
    d = classify("Consumer Price Index – September 2026", "bls", 5)
    assert d.relevant and d.macro and d.coins == [] and d.clase == "macro" and d.strong


def test_nota_institucional_de_la_fed_se_descarta():
    d = classify("Federal Reserve Board announces termination of enforcement action with Example Bank", "fed", 5)
    assert not d.relevant and "institucional" in d.reason


def test_comunicado_del_fomc_pasa():
    assert classify("Federal Reserve issues FOMC statement", "fed", 5).relevant


def test_fuera_del_universo_dice_de_que_habla():
    d = classify("PEPE surges 300% after whale buys", "tree", 5, symbols=["PEPE"])
    assert not d.relevant and "PEPE" in d.reason


def test_opinion_se_descarta():
    d = classify("Bitcoin price prediction: will BTC hit $200K this year?", "cointelegraph", 5)
    assert not d.relevant and "Opinión" in d.reason


def test_antigua_se_descarta_aunque_sea_relevante():
    d = classify("SEC approves spot Solana ETF", "coindesk", 7 * 3600)
    assert not d.relevant and d.reason.startswith("Antigua") and d.coins == ["SOL"]


def test_consejos_de_inversion_son_opinion():
    for t in ("Prediction: A Stock Market Crash Is Coming. Here's What Investors Should Do",
              "3 Bitcoin Stocks to Buy Before the Fed Meeting", "Should You Buy XRP Now"):
        assert not classify(t, "gnews", 60, lang="en").relevant, t


def test_de_unas_horas_entra_como_contexto():
    d = classify("SEC approves spot Solana ETF", "coindesk", 3 * 3600)
    assert d.relevant and not d.fresh and d.coins == ["SOL"]
    assert classify("SEC approves spot Solana ETF", "coindesk", 60).fresh


def test_temas_de_mercado_en_otros_idiomas_sin_monedas():
    guerra = classify("Israel strikes Iranian oil terminals", "reuters", 60, lang="en")
    assert guerra.relevant and guerra.strong and "guerra" in guerra.topics and guerra.coins == []
    assert guerra.clase == "macro" and guerra.macro
    elecciones = classify("Sánchez convoca elecciones generales anticipadas", "elpais", 60, lang="es")
    assert elecciones.relevant and "elecciones" in elecciones.topics
    assert elecciones.clase == "contexto" and not elecciones.strong and elecciones.coins == []
    assert classify("中国对美国商品加征关税", "xinhua", 60, lang="zh").relevant
    assert classify("日銀、利上げを決定", "nikkei", 60, lang="ja").strong


def test_ruido_dice_por_que():
    d = classify("Real Madrid gana la Liga", "marca", 60, lang="es")
    assert not d.relevant and "Deportes" in d.reason


def test_monedas_en_japones_y_chino():
    assert classify("ビットコイン急伸、最高値を更新", "nikkei", 60, lang="ja").coins == ["BTC"]
    assert classify("以太坊现货ETF获批", "caixin", 60, lang="zh").coins == ["ETH"]


def test_misma_noticia_en_chino_se_agrupa():
    a = tokens("中国宣布对美国商品加征15%关税")
    b = tokens("中国宣布对美商品加征15%关税 外交部回应")
    assert similar(a, b)
    assert not similar(a, tokens("日本央行决定维持利率不变"))


# Ejemplos reales de lo que hoy entraba como relevante (con las cuatro monedas) y no debería: bot.db del 27 y el 28-10-2026
# y bot.log (hallazgo n.º 2 de docs/auditoria-2026-10-28.md). Se siguen captando como contexto, sin IA y sin monedas.
CONTEXTO_REAL = [
    ("LG화학, LG엔솔 지분으로 2조5000억 확보…\"재무 개선·주주환원 추진\"", "ko", "empresas"),
    ("기초생보 못 받는 빈곤층 137만 명…정부, 대상 선정 기준 단계적 확대", "ko", "politica"),
    ("18억 아파트 8억에 또 나왔다", "ko", "inmobiliario"),
    ("IBK투자증권, 2027년 신입사원 공개채용 나서", "ko", "bolsa"),
    ("한전, 전기료 체납 홈플러스에 “매장별 단전 검토”", "ko", "energia"),
    ("전북 핵심 현안 예산 반드시 확보해야", "ko", "macro"),
    ("比亚迪最大“黑武士”6座SUV到店，毫无疑问平价路线！", "zh", "empresas"),
    ("招待選民赴陸旅遊賄選 台南市議員候選人羈押禁見", "zh", "elecciones"),
    ("美國議息結果出爐前 恒指收升53點", "zh", "bolsa"),
    ("ドコモ、スマホの使用履歴から「睡眠の質」を推定 ウェアラブル不要", "ja", "tecnologia"),
    ("Cientista político critica Lula após vitória: \"Revanchista\"", "pt", "politica"),
    ("Greve dos metroviários paralisa São Paulo", "pt", "empleo"),
    ("Finanzielle Notbremse: Landkreis Augsburg erlässt eine Haushaltssperre", "de", "macro"),
    ("Rio Tinto Seeks 56% Boost in CEO Target Compensation", "en", "empresas"),
    ("New Oriental Announces Results for the First Fiscal Quarter Ended August 31, 2026", "en", "macro"),
    ("Maryland election's board confirms DOJ election monitors", "en", "elecciones"),
    ("EU antitrust regulators fine Google 2 billion euros", "en", "regulacion"),
    ("Apple unveils new iPhone with AI features", "en", "tecnologia"),
    ("Amazon anuncia 14.000 despidos en todo el mundo", "es", "empleo"),
]


@pytest.mark.parametrize("title,lang,topic", CONTEXTO_REAL)
def test_politica_empresas_y_tecnologia_son_contexto(title, lang, topic):
    d = classify(title, "x", 60, lang=lang)
    assert topic in d.topics                            # el tema se sigue detectando (para medir las fuentes)
    assert d.relevant and d.clase == "contexto" and not d.strong and not d.macro and d.coins == []
    assert d.reason.startswith("Contexto")


MACRO_REAL = [
    ("Rusia lanza más de 100 misiles balísticos contra Kiev", "es"),
    ("Israel strikes Iranian oil terminals", "en"),
    ("中国对美国商品加征关税", "zh"),
    ("日銀、利上げを決定", "ja"),
    ("미 연준, 기준금리 0.25%p 인하", "ko"),
    ("Nonfarm payrolls rise by 150,000 in October", "en"),
    ("美国10月非农就业人数增加15万", "zh"),
    ("Arbeitslosenquote in den USA steigt überraschend", "de"),
    ("Regional lender collapses as banking crisis fears grow", "en"),
    ("US GDP grows 2.1% in third quarter", "en"),
    ("EU agrees new sanctions package against Russia", "en"),
]


@pytest.mark.parametrize("title,lang", MACRO_REAL)
def test_macro_y_geopolitica_fuertes_pasan_marcadas_macro(title, lang):
    d = classify(title, "x", 60, lang=lang)
    assert d.relevant and d.clase == "macro" and d.macro and d.strong and d.coins == []


def test_cripto_lleva_sus_monedas_y_solo_las_suyas():
    assert classify("ビットコイン急伸、最高値を更新", "nikkei", 60, lang="ja").coins == ["BTC"]
    d = classify("XRP and Solana lead gains as Ether lags", "theblock", 10)
    assert d.clase == "cripto" and d.coins == ["ETH", "SOL", "XRP"]
    # De cripto en general (stablecoins, exchanges): pasa como cripto, pero sin inventarse monedas.
    for t, lang in (("Stablecoin bill clears Senate committee", "en"),
                    ("삼성전자·하나금융 기술 왜 한국서 못 써?…스테이블코인 ‘그림자규제’가 만든 현실", "ko"),
                    ("[사설] 증권사·빗썸까지 해킹 확산, 실효성 있는 대책 마련을", "ko")):
        d = classify(t, "x", 60, lang=lang)
        assert d.relevant and d.clase == "cripto" and d.strong and d.coins == [], t


def test_coreano_con_pista_de_ticker():
    d = classify("비트코인(BTC) 입출금 일시 중단 안내", "upbit", 5, hints=["BTC"])
    assert d.relevant and d.coins == ["BTC"]


def test_misma_noticia_con_otras_palabras():
    a = tokens("SEC approves spot Solana ETF with staking")
    b = tokens("SEC approves Solana spot ETF featuring staking")
    assert similar(a, b)
    assert not similar(a, tokens("Bitcoin miners sell reserves as hashprice drops"))


# Revisión del 28-10-2026, hallazgo n.º 2: geopolítica y energía de gran impacto que caían en «contexto» (nunca se
# triaban ni podían confirmarse). Titulares de la revisión y reales de bot.db del 27 y el 28-10-2026.
GEO_FUERTE = [
    ("Iran closes Strait of Hormuz", "en"),
    ("Nuclear test by North Korea", "en"),
    ("Oil prices surge 8% after OPEC+ announces surprise output cut", "en"),
    ("Pandemic declared by WHO", "en"),
    ("Il nuovo attacco di Usa e Israele all\u2019Iran (dopo le Midterm)", "it"),
    ("Alista EU posible ofensiva contra Irán tras elecciones", "es"),
    ("Ukraine recaptures territory as forces go on offensive, Zelensky says", "en"),
    ("Трамп: конфликт с Ираном закончится, когда я захочу", "ru"),
    ("北朝鮮が弾道ミサイルを発射", "ja"),
    ("欧佩克+意外宣布减产", "zh"),
    ("Le pétrole flambe de 9 % après l\u2019attaque contre l\u2019Iran", "fr"),
    ("Ölpreis springt um 7 % nach Angriff auf Iran", "de"),
]


@pytest.mark.parametrize("title,lang", GEO_FUERTE)
def test_geopolitica_y_energia_fuertes_son_macro(title, lang):
    d = classify(title, "x", 60, lang=lang)
    assert d.relevant and d.clase == "macro" and d.macro and d.strong and d.coins == [], (title, d)


# Hallazgo n.º 3: noticias de cripto reales (exchanges, 加密…, 코인…) que caían en «contexto».
CRIPTO_SIN_MONEDA = [
    ("Bithumb Says Internal Mobile System Hacked, Personal Data Leaked", "en"),
    ("COINDESK: Bithumb hacked through employee system, personal data leaked", "en"),
    ("Bithumb fue hackeado: se filtraron datos personales de sus clientes", "es"),
    ("Presidential Office Convenes Emergency Meeting over Mirae Asset, Bithumb Hacks", "en"),
    ("加密市场普遍上涨，Meme板块涨近4%", "zh"),
    ("加密行业在本轮美国中期选举中已花费约1.76亿美元", "zh"),
    ("Robinhood 第三季加密營收年減 55%", "zh"),
    ("코인베이스 \"내부 해킹 아니다\"", "ko"),
    ("증권사·운용사·코인거래소까지…금융권 해킹 전방위 확산", "ko"),
    ("Upbit suspends deposits after wallet incident", "en"),
]


@pytest.mark.parametrize("title,lang", CRIPTO_SIN_MONEDA)
def test_exchanges_y_cripto_en_chino_y_coreano_son_cripto(title, lang):
    d = classify(title, "x", 60, lang=lang)
    assert d.relevant and d.clase == "cripto" and d.strong and d.coins == [], (title, d)
    assert classify(title, "x", 60).clase == "cripto"   # también sin idioma


# Hallazgo n.º 12: falsos positivos de macro que gastaban IA local y podían pasar al reflejo.
NO_MACRO = [
    ("CPI a mandatos de Luís Neves na PJ", "pt"),                       # CPI: comissão parlamentar de inquérito
    ("La CPI emite una orden de detención contra un exministro", "es"),  # la Corte Penal Internacional
    ("ブレイブリーデフォルト II の新情報が公開", "ja"),                    # un videojuego
    ("퇴직연금 디폴트옵션 수익률 1위", "ko"),                               # la opción por defecto de una pensión
    ("VfB Lübeck: Fanstreit und Finanzkrise", "de"),                     # un club de fútbol
    ("Zookeeper dies after being attacked by leopard, safari park says", "en"),
    ("Vers des taxes municipales à l\u2019inflation à Lévis", "fr"),       # impuestos indexados, no un dato
    ("Rents tied to inflation rise in Ontario", "en"),
    ("코인노래방 매출 회복세", "ko"),
    ("加密通信技术取得突破", "zh"),                                        # «加密» = cifrado
]


@pytest.mark.parametrize("title,lang", NO_MACRO)
def test_falsos_positivos_no_son_macro_ni_cripto(title, lang):
    d = classify(title, "x", 60, lang=lang)
    assert d.clase not in ("macro", "cripto"), (title, d)


def test_macro_de_verdad_sigue_siendo_macro():
    assert classify("US CPI rises 0.3% in September", "x", 60, lang="en").clase == "macro"
    assert classify("Hausse de l\u2019inflation en France en octobre", "x", 60, lang="fr").clase == "macro"
    assert classify("日本の債務デフォルト懸念が浮上", "x", 60, lang="ja").clase == "macro"
    assert classify("Russia attacked by drones overnight, says ministry", "x", 60, lang="en").clase == "macro"


# Hallazgo n.º 13: «sol» es también el sol (y el sol peruano) en español y portugués.
@pytest.mark.parametrize("title,lang,coins", [
    ("Una radical acampada en Sol propina un puñetazo a una mujer", "es", []),
    ("Super Astro Sol: estos son los números ganadores del 27 de octubre", "es", []),
    ("Sol retrocede 0,25% frente al dólar mientras la Bolsa de Lima avanza", "es", []),
    ("Chuva e pouco sol: veja a previsão para novembro", "pt", []),
    ("SOL ETF approval odds rise", "en", ["SOL"]),
    ("$SOL rallies as memecoin season returns", "en", ["SOL"]),
    ("Bitcoin y sol suben con fuerza", "es", ["BTC", "SOL"]),
    ("El token sol marca máximos", "es", ["SOL"]),
    ("Solana hits record", "en", ["SOL"]),
])
def test_sol_solo_con_contexto_cripto(title, lang, coins):
    assert classify(title, "x", 60, lang=lang).coins == coins
