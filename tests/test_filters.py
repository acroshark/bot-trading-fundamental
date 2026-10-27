from bot.filters import classify, similar, tokens


def test_menciona_moneda_del_universo():
    d = classify("Spot bitcoin ETFs see $610M in net inflows", "coindesk", 30)
    assert d.relevant and d.coins == ["BTC"]


def test_varias_monedas_en_orden_del_universo():
    d = classify("XRP and Solana lead gains as Ether lags", "theblock", 10)
    assert d.coins == ["ETH", "SOL", "XRP"]


def test_dato_oficial_del_bls_afecta_a_todo():
    d = classify("Consumer Price Index – September 2026", "bls", 5)
    assert d.relevant and d.macro and d.coins == ["BTC", "ETH", "SOL", "XRP"]


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
    assert guerra.relevant and guerra.strong and "guerra" in guerra.topics and guerra.coins == ["BTC", "ETH", "SOL", "XRP"]
    elecciones = classify("Sánchez convoca elecciones generales anticipadas", "elpais", 60, lang="es")
    assert elecciones.relevant and "elecciones" in elecciones.topics
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


def test_coreano_con_pista_de_ticker():
    d = classify("비트코인(BTC) 입출금 일시 중단 안내", "upbit", 5, hints=["BTC"])
    assert d.relevant and d.coins == ["BTC"]


def test_misma_noticia_con_otras_palabras():
    a = tokens("SEC approves spot Solana ETF with staking")
    b = tokens("SEC approves Solana spot ETF featuring staking")
    assert similar(a, b)
    assert not similar(a, tokens("Bitcoin miners sell reserves as hashprice drops"))
