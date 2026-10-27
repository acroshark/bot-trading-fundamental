import re
import time

import pytest

from bot import topics as T
from bot.topics import TOPICS, analyze, is_market_relevant, noise_reason, normalize_lang, topics

# (titular, idioma, tema que debe salir). Titulares reales o calcados de agencias, recortados.
CASES = [
    # Inglés
    ("Fed signals rate cut", "en", "banco_central"),
    ("Israel strikes Iran", "en", "guerra"),
    ("Nvidia beats earnings", "en", "empresas"),
    ("US CPI rises 0.4% in September, hotter than expected", "en", "inflacion"),
    ("Nonfarm payrolls jump by 250,000", "en", "empleo"),
    ("Trump slaps 50% tariffs on Chinese steel", "en", "aranceles"),
    ("OPEC+ agrees to deepen oil output cuts", "en", "energia"),
    ("Japan's ruling party picks new leader ahead of snap election", "en", "elecciones"),
    ("EU adopts 20th package of sanctions against Russia", "en", "sanciones"),
    ("Typhoon forces Tokyo Stock Exchange to halt trading", "en", "catastrofe"),
    ("SEC sues Coinbase over unregistered securities", "en", "regulacion"),
    ("Bitcoin tops $150,000 as ETF inflows surge", "en", "cripto"),
    ("China home prices fall for 30th straight month", "en", "inmobiliario"),
    ("Bank run fears hit regional lenders", "en", "banca"),
    ("Nikkei 225 hits record high", "en", "bolsa"),
    ("UK GDP shrinks unexpectedly in third quarter", "en", "macro"),
    ("Taiwan Strait tensions rise after PLA drills", "en", "geopolitica"),
    ("TSMC to build new AI chip plant in Arizona", "en", "tecnologia"),
    ("US and China agree trade truce in Geneva", "en", "comercio"),
    ("Senate passes stopgap bill to avoid government shutdown", "en", "politica"),
    # Español
    ("Elecciones generales: el PP gana en Madrid", "es", "elecciones"),
    ("OPEP+ recorta la producción", "es", "energia"),
    ("El BCE baja los tipos de interés un cuarto de punto", "es", "banco_central"),
    ("El IPC se modera al 2,1 % en octubre", "es", "inflacion"),
    ("El paro baja en 20.000 personas en septiembre", "es", "empleo"),
    ("El Ibex 35 cae un 2 % arrastrado por la banca", "es", "bolsa"),
    ("Inditex dispara su beneficio un 15 %", "es", "empresas"),
    ("Sánchez anuncia un adelanto electoral", "es", "politica"),
    ("Milei logra aprobar el presupuesto en el Congreso", "es", "politica"),
    ("La DANA deja inundaciones en Valencia", "es", "catastrofe"),
    ("El precio de la vivienda sube un 10 % en Madrid", "es", "inmobiliario"),
    ("México responde con aranceles a Estados Unidos", "es", "aranceles"),
    ("La CNMV multa a una gestora por publicidad engañosa", "es", "regulacion"),
    # Portugués
    ("Lula anuncia pacote fiscal", "pt", "politica"),
    ("Lula anuncia pacote fiscal", "pt", "macro"),
    ("Copom eleva a Selic para 15%", "pt", "banco_central"),
    ("Ibovespa fecha em alta com Petrobras", "pt", "bolsa"),
    ("IPCA desacelera em setembro", "pt", "inflacion"),
    ("Trump impõe tarifa de 50% ao Brasil", "pt", "aranceles"),
    ("Enchentes no Rio Grande do Sul deixam mortos", "pt", "catastrofe"),
    ("Bolsonaro é condenado pelo STF", "pt", "politica"),
    # Alemán
    ("Bundestag beschließt Haushalt", "de", "politica"),
    ("Erdbeben erschüttert Japan", "de", "catastrofe"),
    ("EZB senkt den Leitzins erneut", "de", "banco_central"),
    ("DAX schließt auf Rekordhoch", "de", "bolsa"),
    ("Inflation in Deutschland steigt auf 2,4 Prozent", "de", "inflacion"),
    ("USA verhängen Strafzölle auf EU-Autos", "de", "aranceles"),
    ("Volkswagen kündigt Stellenabbau an", "de", "empleo"),
    ("Gaspreis steigt nach Pipeline-Ausfall", "de", "energia"),
    # Francés
    ("La BCE maintient ses taux directeurs", "fr", "banco_central"),
    ("Le gouvernement Lecornu menacé par une motion de censure", "fr", "politica"),
    ("Le CAC 40 recule de 1 %", "fr", "bolsa"),
    ("Grève générale contre la réforme des retraites", "fr", "empleo"),
    ("Séisme de magnitude 7 au large du Japon", "fr", "catastrofe"),
    # Italiano
    ("La Bce taglia i tassi di interesse", "it", "banco_central"),
    ("Meloni presenta la legge di bilancio", "it", "politica"),
    ("Piazza Affari chiude in rialzo", "it", "bolsa"),
    ("Dazi americani sul vino italiano", "it", "aranceles"),
    # Neerlandés
    ("ASML verhoogt winstverwachting", "nl", "empresas"),
    ("Kabinet-Schoof valt na ruzie over asiel", "nl", "politica"),
    ("AEX sluit hoger", "nl", "bolsa"),
    # Japonés
    ("日銀、追加利上げを決定", "ja", "banco_central"),
    ("日経平均が史上最高値を更新", "ja", "bolsa"),
    ("トヨタ、純利益が過去最高", "ja", "empresas"),
    ("衆院選、自民党が過半数割れ", "ja", "elecciones"),
    ("能登半島で震度7の地震", "ja", "catastrofe"),
    ("米国が日本製品に追加関税", "ja", "aranceles"),
    # Chino
    ("美中贸易战升级", "zh", "comercio"),
    ("美中贸易战升级", "zh", "aranceles"),
    ("國會通過預算案", "zh", "politica"),
    ("中国人民银行宣布降准", "zh", "banco_central"),
    ("恒生指数大涨3%", "zh", "bolsa"),
    ("比特币突破十万美元", "zh", "cripto"),
    ("台积电第三季度净利创新高", "zh", "empresas"),
    ("以军空袭加沙", "zh", "guerra"),
    # Coreano
    ("국회 탄핵안 가결", "ko", "politica"),
    ("한국은행, 기준금리 동결", "ko", "banco_central"),
    ("코스피 급락… 외국인 순매도", "ko", "bolsa"),
    ("삼성전자 3분기 영업이익 급증", "ko", "empresas"),
    ("북한, 동해상으로 탄도미사일 발사", "ko", "guerra"),
    ("비트코인 사상 최고가 경신", "ko", "cripto"),
    # Árabe
    ("البنك المركزي المصري يرفع أسعار الفائدة", "ar", "banco_central"),
    ("أوبك+ تخفض إنتاج النفط", "ar", "energia"),
    ("غارات إسرائيلية على جنوب لبنان", "ar", "guerra"),
    ("ارتفاع التضخم في السعودية", "ar", "inflacion"),
    # Hebreo
    ("בנק ישראל הוריד את הריבית", "he", "banco_central"),
    ("הכנסת אישרה את התקציב", "he", "politica"),
    ('צה"ל תקף בלבנון', "he", "guerra"),
    # Ruso
    ("Банк России повысил ставку", "ru", "banco_central"),
    ("Нефть Brent подорожала на 3%", "ru", "energia"),
    ("Госдума приняла бюджет", "ru", "politica"),
    ("Землетрясение на Камчатке", "ru", "catastrofe"),
    # Ucraniano
    ("ЄС запроваджує нові санкції", "uk", "sanciones"),
    ("НБУ знизив облікову ставку", "uk", "banco_central"),
    ("Масована атака дронів на Київ", "uk", "guerra"),
    ("Верховна Рада ухвалила бюджет", "uk", "politica"),
    # Ataques con verbos genéricos
    ("ロシアがウクライナに大規模攻撃", "ja", "guerra"),
    ("러시아, 우크라이나에 대규모 공격", "ko", "guerra"),
    ("Rússia lança ataque massivo contra a Ucrânia", "pt", "guerra"),
    ("Ukrainian drones hit Russian oil refinery", "en", "guerra"),
    ("Russia pounds Ukraine's energy grid", "en", "guerra"),
    ("Pakistan and Afghanistan clash at border", "en", "guerra"),
    # Negociaciones comerciales entre EE. UU. y China
    ("中美经贸会谈取得积极进展", "zh", "comercio"),
    ("美中經貿磋商達成共識", "zh", "comercio"),
    ("US, China reach framework deal in Kuala Lumpur talks", "en", "comercio"),
    # Bancos centrales y elecciones de Latinoamérica
    ("El BCRA endurece el cepo", "es", "banco_central"),
    ("El Banco de México recorta su tasa", "es", "banco_central"),
    ("Banco de la República de Colombia mantiene su tasa", "es", "banco_central"),
    ("Chile vota en primarias", "es", "elecciones"),
    ("Honduras elige presidente", "es", "elecciones"),
    # Los falsos amigos solo se quitan al inglés: las palabras propias del idioma siguen contando
    ("Krieg in der Ukraine: Russland greift Kiew an", "de", "guerra"),
    ("Nato-Gipfel in Den Haag", "de", "geopolitica"),
    ("La Nato rafforza il fianco est", "it", "geopolitica"),
    ("OpenAI lanza un nuevo modelo de IA", "es", "tecnologia"),
    ("L'IA générative bouleverse la finance", "fr", "tecnologia"),
    ("Le Premier ministre présente son budget", "fr", "politica"),
    ("Coup d'État au Niger", "fr", "politica"),
    ("Le chef de l'État s'exprime ce soir", "fr", "politica"),
    ("La SEC approuve les ETF bitcoin", "fr", "regulacion"),
    ("Il Parlamento Ue approva MiCA", "it", "regulacion"),
    ("Terrorismo: detenidos tres yihadistas", "es", "guerra"),
    ("El Gobierno aprueba una reforma fiscal", "es", "macro"),
    ("Nvidia unveils new AI chips", "en", "tecnologia"),
    ("한은, 기준금리 동결", "ko", "banco_central"),
    # Aranceles con contexto comercial
    ("Trump anuncia tarifas sobre o aço brasileiro", "pt", "aranceles"),
    ("Трамп пригрозил тарифами Китаю", "ru", "aranceles"),
    ("США ввели заградительные тарифы на сталь", "ru", "aranceles"),
    ("Trump minaccia nuove tariffe sull'Ue", "it", "aranceles"),
    ("Trump dreigt met nieuwe heffingen", "nl", "aranceles"),
]


@pytest.mark.parametrize("title,lang,topic", CASES)
def test_detecta_tema(title, lang, topic):
    assert topic in topics(title, lang)
    assert topic in topics(title)            # sin idioma busca en los 14 y también lo encuentra
    assert noise_reason(title, lang) is None  # con tema de mercado nunca es ruido


NOISE_CASES = [
    ("Real Madrid gana la Liga", "es", "Deportes"),
    ("Real Madrid gana la Liga", None, "Deportes"),
    ("Bayern München gewinnt gegen Dortmund", "de", "Deportes"),
    ("大谷翔平が2打席連続ホームラン", "ja", "Deportes"),
    ("손흥민 결승골", "ko", "Deportes"),
    ("Taylor Swift and Travis Kelce set wedding date", "en", "Famosos"),
    ("Horóscopo de hoy: Aries y Tauro", "es", "Horóscopo"),
    ("Oroscopo di domani", "it", "Horóscopo"),
    ("Receta de tarta de queso en 10 minutos", "es", "Recetas"),
    ("Рецепт идеального борща", "ru", "Recetas"),
    ("Chocolate chip cookies recipe", "en", "Recetas"),
    ("Chocolate chip cookies recipe", None, "Recetas"),
    ("El tiempo mañana: lluvias en el norte", "es", "Tiempo"),
    ("Погода на выходные: снег и мороз", "ru", "Tiempo"),
    ("Weather: rain and snow this weekend", "en", "Tiempo"),
    ("Lotería de Navidad: el Gordo cae en Madrid", "es", "Loterías"),
    ("Box office: new Marvel movie tops the weekend", "en", "Cultura"),
    ("Festival de Cannes : le palmarès", "fr", "Cultura"),
    ("10 restaurantes para una escapada romántica", "es", "Ocio"),
]


@pytest.mark.parametrize("title,lang,reason", NOISE_CASES)
def test_ruido_seguro(title, lang, reason):
    assert topics(title, lang) == []
    assert noise_reason(title, lang).startswith(reason)


@pytest.mark.parametrize("title,lang", [
    ("Le chef de l'État s'exprime ce soir", "fr"),
    ("Neuer Chef für die Deutsche Bahn", "de"),
    ("Ryanair opera 50 rutas nuevas desde Madrid", "es"),
])
def test_ruido_ingles_no_se_aplica_a_falsos_amigos(title, lang):
    assert noise_reason(title, lang) is None


@pytest.mark.parametrize("title,lang", [
    ("Real Madrid lanza una emisión de bonos de 1.000 millones", "es"),
    ("Lluvias torrenciales provocan inundaciones en Valencia", "es"),
    ("Weather disrupts oil output in the Gulf of Mexico", "en"),
    ("台風で東証が取引停止", "ja"),
])
def test_con_tema_de_mercado_nunca_es_ruido(title, lang):
    assert topics(title, lang)
    assert noise_reason(title, lang) is None


@pytest.mark.parametrize("title", [
    "Workers strike at Boeing plant",
    "Machinists strike at Boeing enters third week",
    "Hollywood writers strike ends",
])
def test_huelga_no_es_guerra(title):
    t = topics(title, "en")
    assert "empleo" in t and "guerra" not in t


@pytest.mark.parametrize("title", [
    "Magnitude 7.1 earthquake strikes off Japan",
    "Hurricane Melissa strikes Jamaica",
])
def test_catastrofe_que_golpea_no_es_guerra(title):
    t = topics(title, "en")
    assert "catastrofe" in t and "guerra" not in t


def test_acuerdo_comercial_no_es_guerra():
    t = topics("US strikes trade deal with Japan", "en")
    assert "comercio" in t and "guerra" not in t


@pytest.mark.parametrize("title,lang", [
    ("Guerra comercial entre EE UU y China", "es"),
    ("Se recrudece la guerra de aranceles entre EE UU y China", "es"),
    ("Guerra de tarifas entre EUA e China", "pt"),
    ("Handelskrieg zwischen USA und China eskaliert", "de"),
    ("美中贸易战升级", "zh"),
    ("미중 무역전쟁 격화", "ko"),
    ("تصاعد الحرب التجارية بين أمريكا والصين", "ar"),
    ('מלחמת הסחר בין ארה"ב לסין', "he"),
])
def test_guerra_comercial_no_es_guerra(title, lang):
    t = topics(title, lang)
    assert "aranceles" in t and "comercio" in t and "guerra" not in t


@pytest.mark.parametrize("title", ["الصين ترد على الرسوم الجمركية الأمريكية", "فرض رسوماً جمركية على الصلب"])
def test_aranceles_en_arabe_con_articulo_y_tanwin(title):
    assert "aranceles" in topics(title, "ar")


@pytest.mark.parametrize("title,lang", [
    ("Bank of Japan holds rates steady", "en"),
    ("日銀、追加利上げを決定", "ja"),
    ("Банк России повысил ставку", "ru"),
    ("中国人民银行宣布降准", "zh"),
    ("Banco de la República de Colombia mantiene su tasa", "es"),
])
def test_banco_central_no_es_banca(title, lang):
    t = topics(title, lang)
    assert "banco_central" in t and "banca" not in t


# (titular, idioma, tema que NO debe salir): falsos amigos ingleses, siglas que son palabras corrientes y términos
# CJK cortos que caen dentro de otras palabras.
FALSE_POSITIVES = [
    ("Das war knapp: Bayern siegt in letzter Minute", "de", "guerra"),
    ("Helene Fischer war schwanger", "de", "guerra"),
    ("Kane gilt als Favorit für den Ballon d'Or", "de", "macro"),
    ("Bund und Länder streiten über die Schulreform", "de", "macro"),
    ("Juventus, ai rigori batte il Milan", "it", "tecnologia"),
    ("Sinner vola ai quarti di finale", "it", "tecnologia"),
    ("« J'ai eu peur » : le témoignage d'une chanteuse", "fr", "tecnologia"),
    ("Ele ia ao estádio quando foi assaltado", "pt", "tecnologia"),
    ("Le PSG remporte son premier match", "fr", "politica"),
    ("Coup dur pour l'OM", "fr", "politica"),
    ("Dieci modi per cucinare la pasta", "it", "politica"),
    ("Non è mica facile vincere a Napoli", "it", "regulacion"),
    ("È nato il figlio di Chiara Ferragni", "it", "guerra"),
    ("Las mejores películas de terror para Halloween", "es", "guerra"),
    ("La Fiscalía pide prisión para el futbolista", "es", "macro"),
    ("El BOE publica la nueva ley de vivienda", "es", "banco_central"),
    ("Sin embargo, el Real Madrid gana", "es", "sanciones"),
    ("STF rejeita embargos de declaração de Bolsonaro", "pt", "sanciones"),
    ("Un automne sec dans le sud", "fr", "regulacion"),
    ("신청 기한은 이달 말까지", "ko", "banco_central"),
    ("대선 여론조사 결과", "ko", "regulacion"),
    ("전세계 증시 급락", "ko", "inmobiliario"),
    ("양자컴퓨터 개발 경쟁", "ko", "geopolitica"),
    ("政府機関がシャットダウン", "ja", "bolsa"),
    ("新しい変異株が見つかる", "ja", "bolsa"),
    ("宝くじで1等当選", "ja", "elecciones"),
    ("新品上市", "zh", "bolsa"),
    ("调查显示年轻人更喜欢网购", "zh", "regulacion"),
    ("Тарифы ЖКХ вырастут с 1 октября", "ru", "aranceles"),
    ("Тарифи на світло зростуть", "uk", "aranceles"),
    ("Tarifa de energia sobe 10% em novembro", "pt", "aranceles"),
    ("Ofgem raises energy price cap", "en", "sanciones"),
    ("The ripple effects of the shutdown on airports", "en", "cripto"),
    ("Kraken beat Canucks 3-2", "en", "cripto"),
    ("Израиль нанес удар по сектору Газа", "ru", "energia"),
    ("Что угодно, только не это", "ru", "geopolitica"),
    ("الهدف الرئيسي للمشروع", "ar", "politica"),
    ("تنظيم مهرجان للأطفال", "ar", "regulacion"),
]


@pytest.mark.parametrize("title,lang,topic", FALSE_POSITIVES)
def test_falsos_positivos(title, lang, topic):
    assert topic not in topics(title, lang)


def test_el_ingles_sigue_contando_en_ingles():
    assert "guerra" in topics("Russia-Ukraine war enters fourth year", "en")
    assert "macro" in topics("Gilt yields jump after UK budget", "en")
    assert "politica" in topics("French premier survives no-confidence vote", "en")


@pytest.mark.parametrize("title,lang", [
    ("BC Lions win the Grey Cup", "en"),
    ("Apple event set for 10 a.m. PT", "en"),
    ("Dana White announces UFC 320 card", "en"),
    ("Measles continues to spread in Texas", "en"),
    ("Scientists test new hypothesis on dark matter", "en"),
    ("PF faz operação contra tráfico", "pt"),
])
def test_con_idioma_no_busca_en_los_demas(title, lang):
    assert topics(title, lang) == []


def test_sin_idioma_solo_busca_en_su_escritura():
    # «PF» es un término coreano (project financing): no debe saltar en un titular con letras latinas.
    assert "inmobiliario" not in topics("PF faz operação contra tráfico")
    assert "inmobiliario" in topics("건설사 PF 부실 우려")


def test_ancho_completo_y_siglas_latinas_en_japones():
    assert "banco_central" in topics("ＦＲＢが利下げ", "ja")
    assert "tecnologia" in topics("生成AIブームで半導体株が上昇", "ja")


def test_siglas_de_cjk_no_saltan_dentro_de_palabras_latinas():
    assert "tecnologia" not in topics("Rain and maintenance delay the parade")


def test_orden_estable_y_temas_implicados():
    t = topics("Trump slaps 50% tariffs on Chinese steel", "en")
    assert t == sorted(t, key=TOPICS.index)
    assert "comercio" in t   # los aranceles arrastran comercio


def test_titular_vacio():
    assert topics("") == [] and topics(None) == []
    assert noise_reason("") is None


def test_sin_tema_ni_ruido():
    assert topics("Local library extends opening hours", "en") == []
    assert noise_reason("Local library extends opening hours", "en") is None


def test_is_market_relevant():
    assert is_market_relevant(["guerra"])
    assert is_market_relevant(("deportes", "bolsa"))
    assert not is_market_relevant(["deportes"])
    assert not is_market_relevant([])
    assert not is_market_relevant(None)


@pytest.mark.parametrize("raw,key", [("pt-BR", "pt"), ("zh-Hant", "zh"), ("zh_TW", "zh"), ("jp", "ja"),
                                     ("KO", "ko"), ("ua", "uk"), ("xx", None), (None, None), ("", None)])
def test_normalize_lang(raw, key):
    assert normalize_lang(raw) == key


def test_idioma_desconocido_busca_en_todos():
    assert "politica" in topics("국회 탄핵안 가결", "xx")


def test_analyze_para_el_patch():
    a = analyze("Fed signals rate cut", "en")
    assert a == {"topics": ["banco_central"], "relevant": True, "noise": None}
    b = analyze("Real Madrid gana la Liga", "es")
    assert b["topics"] == [] and not b["relevant"] and b["noise"].startswith("Deportes")
    assert analyze("") == {"topics": [], "relevant": False, "noise": None}


def test_analyze_busca_los_temas_una_sola_vez(monkeypatch):
    calls = []
    real = T._topics
    monkeypatch.setattr(T, "_topics", lambda text, key: calls.append(text) or real(text, key))
    assert analyze("Local library extends opening hours", "en")["noise"] is None
    assert len(calls) == 1


def test_texto_largo_se_recorta():
    long = "Local council approves new playground after months of public consultation meetings " * 100
    assert len(T._clean(long)) <= T.MAX_LEN
    t0 = time.perf_counter()
    analyze(long, "en")
    analyze(long)
    assert time.perf_counter() - t0 < 0.1


def test_todos_los_temas_tienen_nombre_y_expresion():
    assert set(T.TOPIC_NAMES) == set(TOPICS)
    for lang in T.LANGS:
        assert set(T.TERMS[lang]) == set(TOPICS), lang
        assert set(T.NOISE[lang]) == set(T.NOISE_REASONS), lang
    assert set().union(*T._BY_SCRIPT.values()) == set(TOPICS)
    assert {lang for _, langs in T._SCRIPTS.values() for lang in langs} == set(T.LANGS)


def test_las_expresiones_se_compilan_una_sola_vez(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("re.compile en cada llamada")
    monkeypatch.setattr(re, "compile", boom)
    assert "banco_central" in topics("Fed signals rate cut", "en")
    assert noise_reason("Real Madrid gana la Liga", "es")


def test_cjk_sin_limites_de_palabra():
    for lang in ("ja", "zh", "ko"):
        for rx in T._BY_LANG[lang].values():
            assert r"\b" not in rx.pattern
