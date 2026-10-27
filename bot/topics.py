"""Temas de mercado en titulares de 14 idiomas, sin IA.

Dice de qué habla una noticia (banco central, aranceles, guerra...) y reconoce el ruido seguro (deportes, famosos,
horóscopo...). Lo usan la mesa de noticias, el triaje y el resumen de fuentes.

Reglas de las expresiones:
- Idiomas con letras (latinas, cirílicas): la palabra tiene que empezar y acabar en límite de palabra; las raíces se
  escriben con `\\w*` explícito.
- Árabe y hebreo: se toleran los prefijos pegados (و, ال, ב, ה...) y unos pocos sufijos.
- Chino, japonés y coreano: sin límites de palabra (no hay espacios o llevan partículas pegadas). Si un término lleva
  letras latinas (AI, LNG...), solo se exige que no esté pegado a otras letras latinas.
- Las siglas que en algún idioma son palabras corrientes van sensibles a mayúsculas: AI (it «ai», fr «j'ai»),
  IA (pt «ia»), SEC (fr «sec»), MiCA (it «mica»), NATO (it «nato»).
- El inglés se busca siempre junto al idioma del titular, salvo sus falsos amigos en ese idioma (_FALSE_FRIENDS).
"""
import re
import unicodedata

TOPICS = ("macro", "banco_central", "inflacion", "empleo", "bolsa", "empresas", "banca", "energia", "comercio",
          "aranceles", "elecciones", "politica", "guerra", "geopolitica", "sanciones", "catastrofe", "tecnologia",
          "cripto", "regulacion", "inmobiliario")
_TOPIC_SET = frozenset(TOPICS)

# Nombre de cada tema para la web.
TOPIC_NAMES = {
    "macro": "Macroeconomía", "banco_central": "Banco central", "inflacion": "Inflación", "empleo": "Empleo",
    "bolsa": "Bolsa", "empresas": "Empresas", "banca": "Banca", "energia": "Energía", "comercio": "Comercio",
    "aranceles": "Aranceles", "elecciones": "Elecciones", "politica": "Política", "guerra": "Guerra",
    "geopolitica": "Geopolítica", "sanciones": "Sanciones", "catastrofe": "Catástrofe", "tecnologia": "Tecnología",
    "cripto": "Cripto", "regulacion": "Regulación", "inmobiliario": "Inmobiliario",
}

# Un tema arrastra a otro más general.
IMPLIES = {"aranceles": "comercio", "sanciones": "geopolitica", "guerra": "geopolitica", "elecciones": "politica"}

# Ruido seguro, en el orden en que se comprueba.
NOISE_REASONS = {
    "deportes": "Deportes: no mueve el mercado",
    "famosos": "Famosos y corazón: no mueve el mercado",
    "horoscopo": "Horóscopo: no mueve el mercado",
    "recetas": "Recetas y cocina: no mueve el mercado",
    "tiempo": "Tiempo meteorológico sin catástrofe: no mueve el mercado",
    "loterias": "Loterías y sorteos: no mueve el mercado",
    "cultura": "Cultura y espectáculos: no mueve el mercado",
    "ocio": "Ocio y estilo de vida: no mueve el mercado",
}

LANGS = ("en", "es", "pt", "de", "fr", "it", "nl", "ja", "zh", "ko", "ar", "he", "ru", "uk")
_ALIASES = {"jp": "ja", "kr": "ko", "cn": "zh", "tw": "zh", "hk": "zh", "br": "pt", "ua": "uk", "iw": "he"}
_CJK = ("ja", "zh", "ko")

TERMS: dict[str, dict[str, str]] = {}   # idioma → tema → alternativas de la expresión
NOISE: dict[str, dict[str, str]] = {}   # idioma → tipo de ruido → alternativas

# --- Inglés (va siempre, porque muchos titulares mezclan siglas y nombres en inglés) ---
TERMS["en"] = {
    "macro": r"gdp|recessions?|recessionary|econom(?:y|ies|ic|ics|ists?)|fiscal|budgets?|deficits?|stimulus|"
             r"debt ceiling|sovereign debt|public debt|national debt|bond yields?|treasury yields?|treasur(?:y|ies)|"
             r"bonds|gilts?|bunds?|yields?|pmi|retail sales|industrial (?:output|production)|"
             r"consumer (?:confidence|spending|sentiment)|imf|world bank|oecd|g7|g20|downgrade[sd]?|"
             r"credit ratings?|moody'?s|fitch|currenc(?:y|ies)|dollar|yen|yuan|renminbi|euro|sterling|rupee|forex|fx",
    "banco_central": r"federal reserve|(?-i:Fed|FED)|fomc|powell|warsh|hassett|ecb|lagarde|boj|bank of japan|ueda|"
                     r"boe|bank of england|pboc|people'?s bank of china|snb|rba|rbi|bank of korea|bank of canada|"
                     r"reserve bank|banxico|copom|bundesbank|riksbank|norges bank|central banks?|central bankers?|"
                     r"rate cuts?|rate hikes?|rate rises?|rate decision|rates? on hold|interest rates?|policy rates?|"
                     r"key rate|benchmark rate|monetary policy|monetary (?:easing|tightening)|hawkish|dovish|"
                     r"quantitative (?:easing|tightening)|basis points?|"
                     r"(?:cuts?|raises?|hikes?|holds?|lowers?|keeps?|leaves?) (?:interest )?rates|yield curve control",
    "inflacion": r"inflation\w*|disinflation\w*|deflation\w*|stagflation\w*|cpi|ppi|pce|core prices|"
                 r"consumer prices?|producer prices?|price index|price pressures|cost[- ]of[- ]living",
    "empleo": r"jobs reports?|jobs data|payrolls?|nonfarm|non-farm|unemployment|jobless\w*|employment|employers|"
              r"labou?r (?:market|force)|wages?|wage growth|layoffs?|laid off|job (?:cuts|losses|openings)|jolts|"
              r"hiring|(?:workers|union|general|labou?r|rail|dock|port|auto|transit|teachers'?|nurses'?|doctors'?|"
              r"pilots'?|machinists'?|writers'?|actors'?|miners'?|drivers'?|dockworkers'?|autoworkers'?|employees'?|"
              r"staff|guild) strikes?|strike action|on strike|walkouts?|trade unions?|labou?r unions?",
    "bolsa": r"stocks?|stock (?:markets?|exchanges?)|equit(?:y|ies)|shares? (?:fell|fall|falls|rose|rise|rises|sank|"
             r"slid|jump\w*|drop\w*|plunge\w*|surge\w*|soar\w*|slump\w*|tumble\w*|climb\w*|sink\w*|slide\w*|gain\w*|"
             r"rall\w*)|share prices?|wall street|nasdaq|dow jones|the dow|s&p 500|s&p|russell 2000|nikkei|topix|"
             r"kospi|kosdaq|hang seng|csi 300|shanghai composite|ftse|dax|cac 40|ibex|stoxx|sensex|nifty|bovespa|"
             r"ibovespa|tsx|asx|index futures|stock futures|futures|sell-?offs?|bull market|bear market|"
             r"market (?:correction|crash|rout|turmoil|selloff|sell-off|rally)|ipos?|initial public offering|"
             r"listings?|delist\w*|vix|volatility|investors|traders",
    "empresas": r"earnings|profits?|profit warning|net income|revenues?|(?:quarterly|annual) results|"
                r"(?:first|second|third|fourth)[- ]quarter|q[1-4] (?:results|earnings|sales|profit)|guidance|"
                r"sales (?:forecast|growth|fell|rose|jump\w*|drop\w*)|"
                r"(?:raises|cuts|lowers|lifts) (?:its )?(?:forecast|outlook|guidance)|mergers?|acquisitions?|"
                r"acquires?|acquired|takeovers?|buyouts?|m&a|bids? for|ceo|cfo|chief executive|bankruptcy|bankrupt|"
                r"chapter 11|insolvency|dividends?|buybacks?|share repurchases?|spin-?off|apple inc|apple's|"
                r"iphone maker|microsoft|nvidia|tesla|amazon|alphabet|google|meta platforms|meta's|samsung|tsmc|"
                r"toyota|sony|softbank|alibaba|tencent|byd|huawei|xiaomi|lvmh|sap|siemens|volkswagen|boeing|airbus|"
                r"(?-i:Intel)|amd|broadcom|oracle|berkshire|exxon|chevron|(?-i:Shell)|bp|aramco|asml|nestl[eé]|"
                r"novo nordisk|hsbc",
    "banca": r"banks?|banking|bankers?|lenders?|lending|loans?|credit (?:crunch|crisis|suisse|agricole)|deposits?|"
             r"bank runs?|bailouts?|stress tests?|capital requirements|basel iii|non-performing|bad loans|jpmorgan|"
             r"goldman sachs|goldman|morgan stanley|citigroup|citi|bank of america|wells fargo|hsbc|barclays|ubs|"
             r"deutsche bank|bnp paribas|santander|bbva|mufg|mizuho|nomura|standard chartered",
    "energia": r"(?<!olive )(?<!coconut )(?<!essential )oil|crude|brent|wti|opec\+?|natural gas|gas prices|lng|"
               r"energy|power grids?|electricity|power prices|coal|nuclear (?:power|plants?|reactors?|energy)|"
               r"renewables?|solar|wind (?:power|farms?)|uranium|pipelines?|refiner(?:y|ies|s)|gasoline|petrol|"
               r"diesel|barrels?|bpd|shale|aramco|exxon|chevron",
    "comercio": r"trade (?:wars?|deals?|talks|deficits?|surplus(?:es)?|balance|tensions|agreements?|pacts?|"
                r"disputes?|data|truce|negotiations|barriers?|policy|routes?|partners?|flows)|free trade|exports?|"
                r"exported|exporters?|imports?|imported|importers?|wto|supply chains?|shipping|freight|"
                r"container ships?|tariff wars?|trade framework|"
                r"(?-i:US|U\.S\.)(?:-| and |, ?)china (?:talks|deal|agreement|negotiations|consensus|truce)|"
                r"china(?:-| and |, ?)(?-i:US|U\.S\.) (?:talks|deal|agreement|negotiations|consensus|truce)|"
                r"(?-i:US|U\.S\.)(?: and |, ?)china (?:reach|agree|strike|seal|clinch)\w* (?:an? )?(?:\w+ )?"
                r"(?:deal|agreement|consensus)",
    "aranceles":r"tariffs?|tariffed|(?:import|customs|export|countervailing) duties|anti-?dumping|levies|trade wars?|"
                 r"tariff wars?|section (?:232|301)|reciprocal (?:tariffs?|duties)",
    "elecciones": r"elections?|electoral|electorate|re-?elect\w*|polls?|pollsters?|polling|voters?|voting|ballots?|"
                  r"turnout|referendums?|referenda|run-?offs?|primaries|primary elections?|exit polls?|"
                  r"campaign trail|snap election|candidates?|caucus(?:es)?|midterms?",
    "politica": r"governments?|govt|parliament\w*|congress\w*|senate|senators?|house of representatives|"
                r"house speaker|white house|presiden(?:t|ts|cy|tial)|prime ministers?|premier(?! league)|"
                r"chancellor|ministers?|ministry|cabinet|coalition|impeach\w*|lawmakers?|legislat\w*|"
                r"bills? (?:passes|passed|signed)|budget bill|politic\w*|opposition|shutdown|protests?|protesters|"
                r"coup|regime|kremlin|downing street|[ée]lys[ée]e|bundestag|knesset|duma|trump|biden|harris|vance|"
                r"xi jinping|putin|zelensk\w*|modi|starmer|macron|merz|meloni|pedro s[aá]nchez|milei|lula|"
                r"sheinbaum|ishiba|takaichi|lee jae-myung|erdogan|netanyahu|carney|albanese|prabowo",
    "guerra": r"wars?|warfare|wartime|militar(?:y|ies)|army|armies|troops|soldiers|airstrikes?|air strikes?|strikes?|"
              r"missiles?|rocket (?:attacks?|fire|launches?)|drones? (?:attacks?|strikes?|hit|hits|struck|target\w*)|"
              r"bomb(?:s|ing|ings|ed|ers?)?|(?:russia|israel|ukraine|iran|army|troops|forces|artillery|warplanes|jets|"
              r"missiles|drones) pound(?:s|ed|ing)|border clash\w*|clash(?:es|ed)? (?:at|on|along) (?:the |their )?"
              r"borders?|armed clash\w*|"
              r"shelling|invasion|invade[sd]?|ceasefire|cease-fire|truce|(?-i:NATO|Nato)|hamas|hezbollah|houthis?|idf|"
              r"pentagon|nuclear weapons?|warships?|navy|naval|artillery|front ?lines?|attacks? on|attacked|"
              r"terror\w*|insurgents?|militants?|offensive against|hostages?|war crimes",
    "geopolitica": r"geopolitic\w*|tensions|diplomac(?:y|ies)|diplomats?|diplomatic|summits?|(?-i:NATO|Nato)|"
                   r"united nations|"
                   r"security council|sovereignty|territorial|border (?:dispute|clash\w*|tensions)|annex\w*|"
                   r"alliances?|treat(?:y|ies)|embass(?:y|ies)|foreign ministers?|state department|"
                   r"secretary of state|bilateral|brics|taiwan strait|south china sea|north korea|pyongyang|"
                   r"strait of hormuz|red sea",
    "sanciones": r"sanctions?|sanctioned|embargo(?:es|ed)?|blacklist\w*|entity list|export (?:controls?|curbs|bans?)|"
                 r"asset freezes?|frozen assets|ofac|oil price caps?|price caps? on (?:russian )?(?:oil|crude|fuel)|"
                 r"(?:russian|g7) (?:oil )?price caps?",
    "catastrofe": r"earthquakes?|quakes?|tsunamis?|typhoons?|hurricanes?|cyclones?|tornado(?:es|s)?|floods?|flooding|"
                  r"wildfires?|bushfires?|forest fires?|volcan\w*|eruptions?|landslides?|mudslides?|droughts?|"
                  r"heat ?waves?|pandemics?|epidemics?|outbreaks?|explosions?|disasters?|catastroph\w*|blackouts?|"
                  r"power outages?|derail\w*|plane crash\w*|famine",
    "tecnologia": r"(?<![a-z]')(?-i:AI|A\.I\.)|artificial intelligence|genai|generative ai|chipmak\w*|"
                  r"semiconductors?|microchips?|"
                  r"ai chips?|chip (?:exports?|sales|stocks?|shares|makers?|wars?|curbs|bans?|plants?|factor(?:y|ies)|"
                  r"shortages?|design\w*)|gpus?|data cent(?:er|re)s?|cloud computing|software|cyber\w*|hacked|"
                  r"hackers?|hacking|ransomware|5g|6g|quantum|smartphones?|iphones?|openai|chatgpt|anthropic|"
                  r"deepseek|robot\w*|tech|technolog(?:y|ies)|big tech|nvidia|tsmc|asml",
    "cripto": r"crypto\w*|bitcoin\w*|btc|ethereum|ether|eth|solana|xrp|ripple(?! effects?)|stablecoins?|tether|"
              r"usdt|usdc|blockchain\w*|defi|nfts?|binance|coinbase|kraken (?:exchange|crypto)|exchange kraken|okx|"
              r"bybit|altcoins?|tokens?|tokeni[sz]ation|halving|"
              r"cbdcs?|digital (?:euro|yuan|dollar|currenc(?:y|ies)|assets?)|spot etfs?|memecoins?",
    "regulacion": r"antitrust|regulat(?:or|ors|ion|ions|ory|e|es|ed|ing)|watchdogs?|(?-i:SEC)|cftc|finra|fca|esma|"
                  r"bafin|"
                  r"ftc|doj|department of justice|justice department|european commission|brussels|"
                  r"competition (?:authority|watchdog|commission|regulator)|probes?|investigations?|crackdowns?|"
                  r"compliance|lawsuits?|sues|sued|class action|supreme court|court rul\w*|fined|"
                  r"licen[cs]es?|(?-i:MiCA|MICA)|bans? (?:on|crypto|short)|legislation|new rules",
    "inmobiliario": r"real estate|property|properties|housing|home ?prices|house prices|home sales|housing starts|"
                    r"mortgages?|homebuilders?|homebuyers?|evergrande|country garden|vanke|reits?|"
                    r"commercial property|office (?:market|vacanc\w*)|rents?|rental\w*|landlords?",
}
NOISE["en"] = {
    "deportes": r"football|soccer|nba|nfl|mlb|nhl|premier league|champions league|la liga|serie a|bundesliga|"
                r"world cup|olympic\w*|tennis|golf|formula (?:1|one)|f1|grand prix|motogp|wimbledon|us open|"
                r"super bowl|playoffs?|semi-?finals?|quarter-?finals?|touchdown|striker|goalkeeper|midfielder|"
                r"head coach|transfer window|ufc|boxing|cricket|rugby|marathon|medals?|real madrid|fc barcelona|"
                r"manchester (?:united|city)|liverpool fc|chelsea fc|lakers|yankees|messi|ronaldo|lebron|nadal|"
                r"djokovic|alcaraz|jannik sinner",
    "famosos": r"celebrit(?:y|ies)|celebs?|kardashians?|taylor swift|beyonc[eé]|royal family|prince harry|"
               r"meghan markle|red carpet|dating rumou?rs|divorce|wedding|engagement ring|pregnan\w*|baby bump|"
               r"hollywood|influencers?|reality (?:tv|show|star)|instagram post|tiktok star|paparazzi|gossip",
    "horoscopo": r"horoscopes?|zodiac|astrolog\w*|tarot|star signs?|mercury retrograde",
    "recetas": r"recipes?|cooking|bake|baking|desserts?|ingredients|cookbook|how to make|dinner ideas|easy meals?|"
               r"chefs?",
    "tiempo": r"weather|forecasters|temperatures|sunny|rain|rainy|showers|snow(?:fall|storm)?|cloudy|chilly|"
              r"cold snap|cold front|met office|national weather service|frost|humid\w*|thunderstorms?|drizzle",
    "loterias": r"lotter(?:y|ies)|lotto|powerball|mega millions|jackpots?|euromillions|winning numbers|scratch-?off",
    "cultura": r"movies?|films?|box office|albums?|concerts?|tour dates|world tour|grammys?|oscars?|emmys?|"
               r"golden globes?|bafta|festivals?|museums?|exhibitions?|novels?|bestsellers?|theat(?:re|er)|opera|"
               r"broadway|tv series|season finale|video games?|eurovision|anime|k-pop|singer|actor|actress|rapper",
    "ocio": r"travel tips|vacation\w*|holiday (?:destinations|ideas|deals)|restaurants?|nightlife|"
            r"fashion (?:week|trends)|beauty|skincare|makeup|fitness|workouts?|weight loss|diet tips|lifestyle|"
            r"gardening|pets|dog breeds?|wellness",
}

# --- Español ---
TERMS["es"] = {
    # «fiscal» suelto es también el fiscal o la Fiscalía: solo cuentan las expresiones de política fiscal.
    "macro": r"pib|recesi[oó]n|econom[ií]\w*|crecimiento econ[oó]mico|fiscalidad|"
             r"(?:pol[ií]tica|presi[oó]n|reforma|reglas?|consolidaci[oó]n|rebajas?|ajuste|paquete|est[ií]mulos?|"
             r"disciplina|estabilidad|a[nñ]o|ejercicio|pacto|d[eé]ficit) fiscal(?:es)?|presupuest\w*|d[eé]ficit|"
             r"deuda (?:p[uú]blica|soberana)|prima de riesgo|bonos?|letras del tesoro|tesoro|rating|"
             r"calificaci[oó]n crediticia|fmi|banco mundial|ocde|pmi|ventas minoristas|producci[oó]n industrial|"
             r"divisas?|d[oó]lar|euro|yen|yuan|peso (?:argentino|mexicano|chileno|colombiano)|tipo de cambio|"
             r"rebaja de (?:la )?calificaci[oó]n",
    "banco_central": r"bce|banco central|banco de españa|(?-i:Fed|FED)|reserva federal|powell|lagarde|"
                     r"tipos? de inter[eé]s|tasas? de inter[eé]s|tasa de referencia|(?:bajada|subida|recorte|alza) "
                     r"de (?:los )?tipos|(?:recorta|sube|baja|mantiene|eleva) (?:los )?tipos|"
                     r"(?:recorta|sube|baja|mantiene|eleva) (?:la )?tasa|pol[ií]tica monetaria|banxico|copom|selic|"
                     r"eur[ií]bor|bcra|bcrp|bcch|banco de m[eé]xico|banco de la rep[uú]blica|"
                     r"banco central de reserva",
    "inflacion": r"inflaci[oó]n|inflacionari\w*|ipc|precios al consumo|ipp|deflaci[oó]n|subida de (?:los )?precios|"
                 r"carest[ií]a|coste de la vida|costo de (?:la )?vida",
    "empleo": r"empleo|desempleo|paro|parados|afiliaci[oó]n|afiliados|(?-i:ERE|ERTE)|despidos?|huelgas?|salari\w*|"
              r"smi|salario m[ií]nimo|mercado laboral|contrataci[oó]n|n[oó]minas|trabajador\w*|sindicat\w*",
    "bolsa": r"bolsas?|ibex\w*|acciones|burs[aá]til\w*|parqué|wall street|nasdaq|dow jones|s&p|nikkei|dax|cac|"
             r"ftse|eurostoxx|stoxx|merval|ibovespa|cotiza\w*|desplome|mercados|mercado (?:burs[aá]til|de valores)|"
             r"futuros|renta variable|volatilidad|salida a bolsa|opv|inversores",
    "empresas": r"beneficios?|beneficio neto|resultados (?:trimestrales|semestrales|anuales|del (?:primer|segundo|"
                r"tercer|cuarto) trimestre)|presenta resultados|facturaci[oó]n|ingresos|ventas|ganancias|"
                r"fusi[oó]n|fusiones|adquisici[oó]n|adquiere|opa|dividendo\w*|recompra|consejero delegado|"
                r"(?-i:CEO)|presidente ejecutivo|quiebra|concurso de acreedores|suspensi[oó]n de pagos|empresas?|"
                r"empresarial\w*|compañ[ií]as?|multinacional\w*|inditex|telef[oó]nica|iberdrola|repsol|"
                r"ferrovial|endesa|naturgy|mercadona|pemex|petrobras|ypf|nvidia|tesla|amazon|google|microsoft",
    "banca": r"bancos?|banca|bancari\w*|entidades financieras|cr[eé]ditos?|pr[eé]stamos?|morosidad|dep[oó]sitos|"
             r"rescate bancario|santander|bbva|caixabank|sabadell|bankinter|unicaja",
    "energia": r"petr[oó]leo|crudo|brent|opep\+?|gas natural|gas|gasolina|gas[oó]leo|di[eé]sel|carburantes?|"
               r"combustibles?|electricidad|el[eé]ctric\w*|precio de la luz|factura de la luz|energ[eé]tic\w*|"
               r"energ[ií]as?|renovables?|centrales? nucleares?|energ[ií]a nuclear|apag[oó]n|barril\w*|"
               r"refiner\w*|gasoducto|oleoducto|hidr[oó]geno|hidrocarburos?",
    "comercio": r"comercio\w*|comerciales?|exportaci\w*|importaci\w*|exportador\w*|importador\w*|balanza comercial|"
                r"guerra comercial|acuerdo comercial|libre comercio|omc|cadenas? de suministro|aduanas?|mercosur",
    "aranceles": r"arancel\w*|tarifas? (?:aduaneras|comerciales)|guerra (?:comercial|arancelaria)|antidumping|"
                 r"grav[aá]menes? a las importaciones",
    "elecciones": r"elecci[oó]n\w*|electoral\w*|comicios|votaci[oó]n|votantes|urnas|sondeos?|encuestas?|escrutinio|"
                  r"candidat\w*|refer[eé]ndum|segunda vuelta|balotaje|campaña electoral|(?:en|las|unas) primarias|"
                  r"(?-i:PASO)|las (?:auton[oó]micas|municipales|legislativas|presidenciales)|"
                  r"elig(?:e|en|ir[aá]n?) (?:a (?:su )?)?(?:nuev[oa] )?(?:presidente|presidenta|alcalde\w*|"
                  r"parlamento|gobernador\w*)|vota(?:n)? (?:hoy|este domingo|el domingo)",
    "politica": r"gobiernos?|gubernamental\w*|congreso|senado|parlamento\w*|presidente|presidenta|ministr\w*|"
                r"moncloa|(?-i:PP|PSOE|VOX|Vox|Sumar|Junts|PNV|ERC|Bildu)|s[aá]nchez|feij[oó]o|ayuso|abascal|"
                r"puigdemont|coalici[oó]n|moci[oó]n de censura|investidura|leyes|ley|decretos?|real decreto|"
                r"decreto ley|oposici[oó]n|partido (?:popular|socialista|republicano|dem[oó]crata|comunista)|"
                r"pol[ií]tic\w*|diputad\w*|alcalde\w*|milei|sheinbaum|petro|boric|maduro|lula|bukele|noboa|trump|"
                r"putin|xi jinping|zelenski|macron|merz|meloni|casa blanca|kremlin|dimisi[oó]n|dimite|protestas?|"
                r"manifestaci[oó]n\w*|golpe de estado|indulto|amnist[ií]a",
    "guerra": r"guerras?|b[eé]lic\w*|militar\w*|ej[eé]rcitos?|tropas|soldados|misil\w*|drones|bombarde\w*|"
              r"ataques? (?:con|contra|de) (?:misiles|drones)|ataques? a[eé]reos?|ataque militar|ofensiva militar|"
              r"invasi[oó]n|invade|alto el fuego|tregua|otan|ham[aá]s|hezbol[aá]|hut[ií]es|"
              r"armas? nucleares?|armament\w*|ministerio de defensa|gasto en defensa|conflicto armado|atentado\w*|"
              r"rehenes|bombas?",
    "geopolitica": r"geopol[ií]tic\w*|tensi[oó]n\w*|diplomac\w*|diplom[aá]tic\w*|cumbres?|otan|onu|"
                   r"consejo de seguridad|soberan[ií]a|territorial\w*|fronteras?|anexi[oó]n|alianzas?|tratados?|"
                   r"embajad\w*|canciller\w*|exteriores|bilateral\w*|brics|g7|g20|estrecho de taiw[aá]n|"
                   r"mar de china|corea del norte",
    "sanciones": r"sanci[oó]n|sanciones|sancion\w*|embargo\w*|lista negra|congelaci[oó]n de activos|"
                 r"controles a la exportaci[oó]n|tope al precio del (?:petr[oó]leo|crudo)",
    "catastrofe": r"terremotos?|sismos?|se[ií]smos?|tsunamis?|tif[oó]n|tifones|hurac[aá]n|huracanes|cicl[oó]n\w*|"
                  r"tornados?|inundaci[oó]n\w*|riadas?|dana|gota fr[ií]a|incendios?(?: forestal\w*)?|"
                  r"volc[aá]n\w*|erupci[oó]n|sequ[ií]a|ola de calor|pandemia|epidemia|brotes? de|explosi[oó]n\w*|"
                  r"cat[aá]strofe\w*|desastre\w*|apag[oó]n|descarrila\w*|accidente a[eé]reo|v[ií]ctimas mortales|"
                  r"deslizamientos? de tierra",
    "tecnologia": r"inteligencia artificial|(?-i:IA)|chips? (?:de|para|avanzados)|fabricantes? de chips|"
                  r"semiconductor\w*|microchips?|tecnol[oó]gic\w*|tecnolog[ií]a\w*|"
                  r"ciberataque\w*|ciberseguridad|hackers?|software|5g|cu[aá]ntic\w*|robot\w*|smartphones?|"
                  r"openai|chatgpt|nvidia|tsmc|huawei|centros? de datos",
    "cripto": r"cripto\w*|criptomoneda\w*|criptoactivos?|bitcoin|ethereum|ether|solana|xrp|ripple|stablecoins?|"
              r"blockchain|cadena de bloques|tokens?|binance|coinbase|euro digital|cbdc|miner[ií]a de bitcoin",
    "regulacion": r"regulaci[oó]n\w*|regulador\w*|regulatori\w*|normativa\w*|cnmv|cnmc|(?-i:SEC)|cftc|bruselas|"
                  r"comisi[oó]n europea|multas?|multad\w*|investiga\w*|demandas?|tribunal\w*|supremo|"
                  r"constitucional|(?-i:MiCA|MICA)|supervisor\w*|antimonopolio|autoridad de la competencia|"
                  r"proh[ií]be\w*|"
                  r"prohibici[oó]n\w*",
    "inmobiliario": r"inmobiliari\w*|viviendas?|hipotec\w*|alquiler\w*|ladrillo|promotor\w*|constructor\w*|"
                    r"socimis?|evergrande|country garden|burbuja inmobiliaria|pisos",
}
NOISE["es"] = {
    "deportes": r"f[uú]tbol\w*|liga|laliga|real madrid|bar[cç]a|atl[eé]tico de madrid|copa del rey|champions|"
                r"mundial de f[uú]tbol|goles?|partido de|entrenador|fichaje\w*|jornada \d+|cl[aá]sico|"
                r"baloncesto|tenis|nadal|alcaraz|f[oó]rmula 1|motogp|ciclismo|tour de francia|vuelta a españa|"
                r"selecci[oó]n española|eurocopa|olimpiadas?|juegos ol[ií]mpicos|medallas?",
    "famosos": r"famos[oa]s|boda|divorcio|embarazada|ruptura|prensa rosa|coraz[oó]n|novi[oa] de|"
               r"isabel pantoja|shakira|pique|influencers?|presentador[a]? de|reality",
    "horoscopo": r"hor[oó]scopo\w*|zod[ií]aco|astrolog\w*|tarot|signos? del zod[ií]aco",
    "recetas": r"recetas?|cocina|ingredientes|postres?|tarta|guiso|cocinar|paella|chef",
    "tiempo": r"el tiempo (?:hoy|mañana|este fin de semana|para)|previsi[oó]n meteorol[oó]gica|aemet|lluvias?|"
              r"tormentas?|temperaturas|calor|fr[ií]o|nieve|nevadas?|chubascos|meteorolog[ií]a|meteorol[oó]gic\w*|"
              r"ola de fr[ií]o|heladas?",
    "loterias": r"loter[ií]a\w*|loto|euromillones|primitiva|bonoloto|el gordo|sorteos?|cup[oó]n de la once",
    "cultura": r"pel[ií]culas?|cine|estrenos?|[aá]lbum|conciertos?|gira de conciertos|festival\w*|museos?|novela|"
               r"teatro|serie de televisi[oó]n|premios goya|goya|oscar|grammy|eurovisi[oó]n|cantante|actor|"
               r"actriz|videojuegos?",
    "ocio": r"viajes|vacaciones|escapadas?|restaurantes?|moda|belleza|maquillaje|dieta|adelgazar|mascotas|"
            r"jardiner[ií]a|estilo de vida|planes para el fin de semana",
}

# --- Portugués ---
TERMS["pt"] = {
    "macro": r"pib|recess[aã]o|economia\w*|econ[oô]mic\w*|fiscal|fiscais|orçament\w*|"
             r"d[eé]ficit|d[ií]vida p[uú]blica|tesouro|t[ií]tulos p[uú]blicos|rating|fmi|banco mundial|c[aâ]mbio|"
             r"d[oó]lar|haddad|pmi|vendas no varejo|produç[aã]o industrial",
    "banco_central": r"banco central|(?-i:BC|BCB)|copom|selic|juros|taxa (?:de juros|b[aá]sica)|gal[ií]polo|"
                     r"campos neto|(?-i:Fed|FED)|bce|pol[ií]tica monet[aá]ria|(?:corte|alta|aumento) (?:de|dos) juros",
    "inflacion": r"inflaç[aã]o|inflacion[aá]ri\w*|ipca|igp-m|igpm|inpc|preços ao consumidor|deflaç[aã]o|carestia|"
                 r"custo de vida",
    "empleo": r"emprego\w*|desemprego|caged|demiss\w*|greves?|sal[aá]ri\w*|sal[aá]rio m[ií]nimo|"
              r"mercado de trabalho|trabalhador\w*|sindicat\w*|vagas de trabalho",
    "bolsa": r"bolsas?|ibovespa|ibov|(?-i:B3)|aç[oõ]es|mercados|mercado (?:financeiro|de aç[oõ]es)|wall street|"
             r"nasdaq|dow jones|s&p|preg[aã]o|cotaç\w*|ipo|abertura de capital|renda vari[aá]vel|futuros|"
             r"volatilidade|investidores",
    "empresas": r"lucros?|lucro l[ií]quido|receitas? (?:l[ií]quidas?|trimestral|recorde)|resultados? (?:trimestrais|"
                r"do trimestre|anuais)|balanço trimestral|faturamento|fus[aã]o|fus[oõ]es|aquisiç[aã]o|aquisiç[oõ]es|"
                r"adquire|dividendos?|recompra|(?-i:CEO)|presidente-executivo|fal[eê]ncia|recuperaç[aã]o judicial|"
                r"empresas?|companhias?|petrobras|(?-i:Vale)|ita[uú]|bradesco|ambev|embraer|jbs|weg|"
                r"magazine luiza|eletrobras|nubank",
    "banca": r"bancos?|banc[aá]ri\w*|cr[eé]ditos?|empr[eé]stimos?|inadimpl[eê]ncia|ita[uú]|bradesco|santander|"
             r"caixa econ[oô]mica|banco do brasil|btg|nubank|dep[oó]sitos",
    "energia": r"petr[oó]leo|brent|opep\+?|g[aá]s natural|g[aá]s|gasolina|diesel|combust[ií]ve\w*|energia\w*|"
               r"energ[eé]tic\w*|eletricidade|el[eé]tric\w*|apag[aã]o|barril\w*|refinaria\w*|pr[eé]-sal|"
               r"renov[aá]ve\w*|energia nuclear|usinas? nuclear\w*|hidrel[eé]tric\w*",
    "comercio": r"com[eé]rcio\w*|comerciais|comercial|exportaç\w*|importaç\w*|exportador\w*|importador\w*|"
                r"balança comercial|guerra comercial|acordo comercial|omc|mercosul|cadeias? de suprimentos?|"
                r"alf[aâ]ndega|aduana",
    # «tarifa» suelta es sobre todo la de la luz, el agua o el transporte: se exige contexto comercial.
    "aranceles": r"tarifas? (?:de importaç[aã]o|sobre|contra|americanas?|dos eua|de trump|rec[ií]procas?|"
                 r"retaliat[oó]rias?|adicionais|alfandeg[aá]rias?|comerciais)|tarifas? de \d+(?:[,.]\d+)? ?%|"
                 r"(?:imp[õo]e|imp[õo]em|impor|imp[ôo]s|imposiç[aã]o de|anuncia|anunciou|aplica|aplicar) tarifas?|"
                 r"tarifaço|tarifaç[aã]o|sobretaxa\w*|guerras? (?:comercia(?:l|is)|tarif[aá]rias?|de tarifas)|"
                 r"antidumping|impostos? de importaç[aã]o",
    "elecciones": r"eleiç[aã]o|eleiç[oõ]es|eleitor\w*|eleitora\w*|votaç[aã]o|urnas?|pesquisa eleitoral|datafolha|"
                  r"quaest|segundo turno|primeiro turno|candidat\w*|plebiscito|referendo|(?-i:TSE)",
    "politica": r"governo\w*|congresso|senado|c[aâ]mara dos deputados|c[aâ]mara|deputad\w*|senador\w*|presidente|"
                r"ministr\w*|planalto|(?-i:STF)|supremo|lula|bolsonaro|tarc[ií]sio|alckmin|alcolumbre|hugo motta|"
                r"(?-i:PT|PL|MDB|PSDB|PSD)|partido (?:dos trabalhadores|liberal)|pol[ií]tic\w*|impeachment|"
                r"decretos?|lei|medida provis[oó]ria|protestos?|manifestaç\w*|golpe de estado|golpista\w*|"
                r"tentativa de golpe",
    "guerra": r"guerras?|b[eé]lic\w*|militar\w*|ex[eé]rcito|tropas|soldados|m[ií]sseis|m[ií]ssil|drones|"
              r"bombarde\w*|ataques? (?:a[eé]reos?|com m[ií]sseis|com drones|de m[ií]sseis|de drones|massivos?|"
              r"russos?|israelenses?|ucranianos?|iranianos?)|ataques? contra (?:a |o )?(?:ucr[aâ]nia|r[uú]ssia|"
              r"israel|ir[aã]|gaza|l[ií]bano|kiev|kyiv)|ofensiva militar|invas[aã]o|invade|"
              r"cessar-fogo|tr[eé]gua|otan|hamas|hezbollah|houthis|armas nucleares|atentado\w*|ref[eé]ns",
    "geopolitica": r"geopol[ií]tic\w*|tens[aã]o|tens[oõ]es|diplomac\w*|diplom[aá]tic\w*|c[uú]pula|otan|onu|"
                   r"conselho de segurança|soberania|fronteira\w*|anexaç[aã]o|aliança|tratados?|embaixad\w*|"
                   r"chanceler\w*|itamaraty|bilateral\w*|brics|g7|g20",
    "sanciones": r"sanç[aã]o|sanç[oõ]es|sancion\w*|embargo\w*|lista negra|magnitsky|congelamento de bens|"
                 r"controles de exportaç[aã]o",
    "catastrofe": r"terremotos?|sismos?|tsunamis?|tuf[aã]o|furac[aã]o|ciclone\w*|tornados?|enchentes?|inundaç\w*|"
                  r"alagamento\w*|deslizamento\w*|queimadas?|inc[eê]ndios?|vulc[aã]o|vulc[aã]nic\w*|erupç[aã]o|"
                  r"seca|estiagem|onda de calor|pandemia|epidemia|surtos?|explos[aã]o|cat[aá]strofe\w*|"
                  r"desastres?|trag[eé]dia|rompimento de barragem",
    "tecnologia": r"intelig[eê]ncia artificial|(?-i:IA)|chips? (?:de|para|avançados)|fabricantes? de chips|"
                  r"semicondutor\w*|tecnologia\w*|tecnol[oó]gic\w*|"
                  r"ciberataque\w*|hackers?|software|5g|qu[aâ]ntic\w*|rob[oô]s?|smartphones?|big techs?",
    "cripto": r"cripto\w*|criptomoeda\w*|criptoativos?|bitcoin|ethereum|solana|xrp|stablecoins?|blockchain|"
              r"tokens?|drex|binance|coinbase|mercado bitcoin",
    "regulacion": r"regulaç\w*|regulador\w*|regulament\w*|(?-i:CVM|ANATEL|ANEEL|ANP|CADE|SEC)|multas?|"
                  r"investigaç\w*|tribunal\w*|proibi\w*|fiscalizaç\w*|antitruste|normas? (?:do|da)",
    "inmobiliario": r"imobili[aá]ri\w*|im[oó]ve\w*|habitaç\w*|moradia|minha casa,? minha vida|hipoteca\w*|"
                    r"financiamento imobili[aá]rio|alugu[eé]is|aluguel\w*|construtora\w*|incorporadora\w*|"
                    r"fundos imobili[aá]rios",
}
NOISE["pt"] = {
    "deportes": r"futebol|brasileir[aã]o|campeonato brasileiro|flamengo|corinthians|palmeiras|s[aã]o paulo fc|"
                r"vasco|fluminense|botafogo|gr[eê]mio|gols?|seleç[aã]o brasileira|copa do brasil|libertadores|"
                r"t[eé]cnico do|neymar|v[oô]lei|basquete|t[eê]nis|f[oó]rmula 1|olimp[ií]adas",
    "famosos": r"famosos?|famosas?|celebridades?|casamento|div[oó]rcio|gr[aá]vida|namorad[oa]|(?-i:BBB)|"
               r"big brother|fofoca",
    "horoscopo": r"hor[oó]scopo|signos?|astrolog\w*|tar[oô]",
    "recetas": r"receitas? de (?:bolo|torta|frango|p[aã]o|doce)|culin[aá]ria|sobremesas?|ingredientes",
    "tiempo": r"previs[aã]o do tempo|tempo (?:hoje|amanh[aã])|chuvas?|temperaturas?|frio|calor|meteorologia|"
              r"inmet|climatempo|frente fria",
    "loterias": r"loterias?|mega-sena|lotof[aá]cil|quina|sorteios?|dupla sena",
    "cultura": r"filmes?|cinema|estreia|[aá]lbum|shows? de|turn[eê]|festival|museu|exposiç[aã]o de arte|novela|"
               r"livros?|teatro|oscar|grammy|cantor\w*|ator|atriz|videogames?",
    "ocio": r"viagens?|f[eé]rias|restaurantes?|moda|beleza|maquiagem|dieta|emagrecer|pets|jardinagem|"
            r"estilo de vida",
}

# --- Alemán ---
TERMS["de"] = {
    "macro": r"bip|bruttoinlandsprodukt\w*|konjunktur\w*|rezession\w*|wirtschaft\w*|wachstum\w*|haushalt\w*|"
             r"schuldenbremse|staatsschulden|defizit\w*|sondervermögen|staatsanleihe\w*|anleihe\w*|"
             r"bundesanleihe\w*|rendite\w*|ifo\w*|zew\w*|einkaufsmanagerindex|pmi|industrieproduktion|"
             r"auftragseingang\w*|einzelhandel\w*|iwf|weltbank|oecd|euro|dollar|yen|währung\w*|rating\w*|"
             r"herabgestuft|konsum\w*",
    "banco_central": r"ezb|notenbank\w*|zentralbank\w*|bundesbank|(?-i:Fed|FED)|us-notenbank|leitzins\w*|zins|"
                     r"zinsen|zinssenkung\w*|zinserhöhung\w*|zinsentscheid\w*|zinswende|zinspause|geldpolitik\w*|"
                     r"lagarde|nagel|powell|bank of japan|boj|snb|schweizerische nationalbank",
    "inflacion": r"inflation\w*|teuerung\w*|verbraucherpreise?|preissteigerung\w*|erzeugerpreise?|deflation|"
                 r"lebenshaltungskosten|hvpi|vpi|preisauftrieb",
    "empleo": r"arbeitsmarkt\w*|arbeitslos\w*|beschäftigung\w*|jobabbau|stellenabbau|entlassung\w*|kündigung\w*|"
              r"streik\w*|warnstreik\w*|gewerkschaft\w*|verdi|ig metall|löhne|lohn\w*|mindestlohn|"
              r"tarifverhandlung\w*|tarifabschluss|kurzarbeit\w*|fachkräfte\w*",
    "bolsa": r"börse\w*|dax\w*|mdax|sdax|tecdax|aktie|aktien\w*|aktienmarkt\w*|aktienkurs\w*|wall street|dow jones|"
             r"nasdaq|s&p|nikkei|kurssturz|kurseinbruch|kursrutsch|kursgewinn\w*|kursverlust\w*|anleger\w*|"
             r"börsengang|ipo|leitindex|finanzmärkte|märkte|euro stoxx|stoxx|volatilität",
    "empresas": r"gewinn|gewinne|gewinnrückgang|gewinneinbruch|gewinnsprung|gewinnwarnung|nettogewinn|"
                r"quartalsgewinn|umsatz\w*|quartalszahlen|quartalsergebnis\w*|jahreszahlen|bilanz\w*|ergebnis\w*|"
                r"übernahme\w*|fusion\w*|zukauf|dividende\w*|aktienrückkauf|insolvenz\w*|pleite|konzern\w*|"
                r"unternehmen\w*|vorstandschef\w*|siemens|sap|volkswagen|vw|bmw|mercedes-benz|daimler|bayer|basf|"
                r"allianz|telekom|lufthansa|rheinmetall|adidas|porsche|infineon|thyssenkrupp",
    "banca": r"bank|banken\w*|deutsche bank|commerzbank|sparkasse\w*|volksbank\w*|kredit\w*|einlagen|"
             r"bankenaufsicht|ubs|landesbank\w*|großbank\w*",
    "energia": r"öl|ölpreis\w*|rohöl\w*|brent|opec\+?|erdgas\w*|gas|gaspreis\w*|gasversorgung|gasspeicher\w*|"
               r"gaslieferung\w*|lng|strom|strompreis\w*|stromversorgung|stromnetz\w*|strommarkt|"
               r"stromerzeugung|energie\w*|kohle\w*|atomkraft\w*|kernkraft\w*|akw|erneuerbare\w*|windkraft\w*|"
               r"solar\w*|pipeline\w*|nord stream|benzin\w*|diesel\w*|heizöl|raffinerie\w*|barrel",
    "comercio": r"handel|handels\w*|export\w*|import|importe|importen|importeure|ausfuhr\w*|einfuhr\w*|"
                r"lieferkette\w*|wto|freihandel\w*",
    "aranceles": r"\w*zöll\w*|zoll|zolls|strafzoll\w*|"
                 r"\w*zoll(?:streit|krieg|erhöhung|politik|paket|drohung|abkommen|satz|sätze|schranken)\w*|"
                 r"handelskrieg\w*|antidumping\w*",
    "elecciones": r"wahl|wahlen|bundestagswahl\w*|landtagswahl\w*|europawahl|kommunalwahl\w*|präsidentschaftswahl\w*|"
                  r"wähler\w*|umfrage\w*|wahlkampf\w*|wahlergebnis\w*|kandidat\w*|stichwahl|referendum|"
                  r"volksabstimmung",
    "politica": r"bundestag\w*|bundesrat|bundesregierung|regierung\w*|kanzler\w*|bundeskanzler\w*|merz|scholz|"
                r"minister\w*|koalition\w*|ampel|(?-i:CDU|CSU|SPD|AfD|FDP|BSW|Grüne|Grünen|Linke)|parlament\w*|"
                r"opposition|misstrauensvotum|vertrauensfrage|gesetz\w*|politik\w*|politisch\w*|präsident\w*|"
                r"weißes haus|kreml|macron|trump|putin|selenskyj|xi jinping|regierungskrise|rücktritt|"
                r"proteste?|demonstration\w*|putsch\w*",
    "guerra": r"krieg\w*|militär\w*|armee|bundeswehr|truppen|soldaten|rakete\w*|drohnen\w*|luftangriff\w*|"
              r"raketenangriff\w*|drohnenangriff\w*|angriffe? (?:auf|gegen)|bombard\w*|beschuss|invasion|"
              r"waffenruhe|waffenstillstand|feuerpause|(?-i:NATO|Nato)|hamas|hisbollah|huthi\w*|atomwaffen\w*|"
              r"kämpfe\w*|"
              r"gefecht\w*|waffen\w*|waffenlieferung\w*|rüstung\w*|geiseln",
    "geopolitica": r"geopoliti\w*|spannungen|diplomat\w*|gipfel\w*|(?-i:NATO|Nato)|(?-i:UNO)|vereinte nationen|"
                   r"sicherheitsrat|souveränität|grenze\w*|annexion|bündnis\w*|abkommen|botschaft\w*|"
                   r"außenminister\w*|aussenminister\w*|bilateral\w*|g7|g20|brics|taiwan-straße|"
                   r"südchinesisches meer|nordkorea",
    "sanciones": r"sanktion\w*|embargo\w*|strafmaßnahmen|exportkontrolle\w*|schwarze liste|eingefroren\w*|"
                 r"ölpreisdeckel|preisdeckel für russisches öl",
    "catastrofe": r"erdbeben\w*|beben|tsunami\w*|taifun\w*|hurrikan\w*|wirbelsturm\w*|tornado\w*|"
                  r"überschwemmung\w*|hochwasser\w*|flut|fluten|flutkatastrophe|sturmflut|unwetter\w*|"
                  r"waldbr[aä]nd\w*|vulkan\w*|ausbruch|dürre\w*|hitzewelle\w*|pandemie\w*|epidemie\w*|"
                  r"explosion\w*|katastrophe\w*|unglück\w*|stromausfall|blackout|erdrutsch\w*",
    "tecnologia": r"künstliche\w* intelligenz|(?-i:KI)|chipherstell\w*|chipfabrik\w*|chipkonzern\w*|chipindustrie|"
                  r"halbleiter\w*|mikrochip\w*|technologie\w*|tech|techkonzern\w*|techwerte|tech-\w*|software\w*|"
                  r"cyber\w*|hacker\w*|cyberangriff\w*|rechenzentr\w*|quanten\w*|roboter\w*|smartphone\w*|"
                  r"digitalisierung",
    "cripto": r"krypto\w*|bitcoin\w*|ethereum|ether|solana|xrp|stablecoin\w*|blockchain\w*|token\w*|"
              r"digitale\w* euro|kryptowährung\w*|kryptobörse\w*",
    "regulacion": r"regulier\w*|regulierung\w*|aufsicht\w*|bafin|finanzaufsicht|kartellamt|bundeskartellamt|"
                  r"eu-kommission|brüssel|strafe|strafen|bußgeld\w*|verbot\w*|klage\w*|gerichtshof|"
                  r"bundesgerichtshof|verfassungsgericht|bundesverfassungsgericht|urteil\w*|ermittlung\w*|"
                  r"verordnung\w*|richtlinie\w*|(?-i:MiCA|MICA|SEC)",
    "inmobiliario": r"immobilien\w*|wohnung\w*|wohnungsbau\w*|wohnungsmarkt|mieten|miete|miet\w*|hauspreise|"
                    r"baufinanzierung|hypothek\w*|bauzinsen|bauträger|vonovia|evergrande|country garden",
}
NOISE["de"] = {
    "deportes": r"fußball\w*|bundesliga|fc bayern|bayern münchen|borussia|bvb|trainer|spieltag|dfb\w*|"
                r"champions league|olympia\w*|olympische\w*|tennis|formel 1|handball|eishockey|torjäger|"
                r"elfmeter|weltmeister\w*|pokal\w*|abstieg|aufstieg in die",
    "famosos": r"promi\w*|stars?|hochzeit|scheidung|schwanger\w*|royals|dschungelcamp|liebes-aus|trennung|"
               r"klatsch|influencer\w*",
    "horoscopo": r"horoskop\w*|sternzeichen|astrolog\w*|tarot",
    "recetas": r"rezept\w*|kochen|backen|zutaten|kuchen|nachtisch|gericht des tages",
    "tiempo": r"wetter\w*|regen|schnee\w*|gewitter\w*|temperaturen|dwd|sonnig|hitze|frost|glätte",
    "loterias": r"lotto\w*|lottozahlen|eurojackpot|gewinnzahlen|ziehung der lottozahlen|glücksspirale",
    "cultura": r"film\w*|kino\w*|album|konzert\w*|tournee|festival\w*|museum|museen|ausstellung\w*|bestseller|"
               r"theater|oper|tatort|eurovision|sänger\w*|schauspieler\w*|netflix-serie",
    "ocio": r"urlaub\w*|reisetipps|restaurant\w*|modetrends|schönheit|diät\w*|abnehmen|haustier\w*|garten\w*|"
            r"lifestyle|freizeit\w*",
}

# --- Francés ---
TERMS["fr"] = {
    "macro": r"pib|r[eé]cession|[eé]conomi\w*|croissance|budget\w*|budg[eé]taire\w*|d[eé]ficit\w*|"
             r"dette (?:publique|souveraine)|obligations?|oat|taux d'emprunt|rendements?|spread|fmi|"
             r"banque mondiale|ocde|pmi|notation|agence de notation|euro|dollar|yen|yuan|devises?|insee|"
             r"conjoncture|consommation des m[eé]nages|production industrielle|plan de relance",
    "banco_central": r"bce|banque centrale|banque de france|(?-i:Fed|FED)|r[eé]serve f[eé]d[eé]rale|"
                     r"taux directeurs?|taux d'int[eé]r[eê]t|(?:baisse|hausse) des taux|politique mon[eé]taire|"
                     r"lagarde|villeroy|powell|banque du japon|bns",
    "inflacion": r"inflation|inflationnist\w*|indice des prix|prix [aà] la consommation|hausse des prix|"
                 r"d[eé]flation|co[uû]t de la vie|ipc|ipch|vie ch[eè]re",
    "empleo": r"emplois?|ch[oô]mage|ch[oô]meurs?|licenciement\w*|suppressions? de postes|plan social|gr[eè]ves?|"
              r"syndicat\w*|salaires?|smic|march[eé] du travail|embauches?|france travail|intersyndicale",
    "bolsa": r"bourses?|boursi\w*|cac 40|cac|actionnaires?|les actions|l'action|march[eé]s|"
             r"march[eé] (?:boursier|financier|obligataire|des actions)|wall street|nasdaq|dow jones|s&p|nikkei|"
             r"dax|euronext|introduction en bourse|krach|euro stoxx|stoxx|investisseurs",
    "empresas": r"b[eé]n[eé]fice\w*|chiffre d'affaires|r[eé]sultats (?:annuels|semestriels|trimestriels)|"
                r"fusions?|acquisitions?|rachats?|opa|dividende\w*|pdg|directeur g[eé]n[eé]ral|faillite|"
                r"redressement judiciaire|liquidation judiciaire|entreprises?|lvmh|totalenergies|airbus|renault|"
                r"stellantis|sanofi|l'or[eé]al|danone|kering|herm[eè]s|carrefour|(?-i:Orange)|vinci|michelin",
    "banca": r"banques?|bancaires?|cr[eé]dits?|pr[eê]ts?|bnp paribas|soci[eé]t[eé] g[eé]n[eé]rale|"
             r"cr[eé]dit agricole|natixis|bpce|d[eé]p[oô]ts|caisse d'[eé]pargne",
    "energia": r"p[eé]trol\w*|(?:le|du) brut|brent|opep\+?|gaz|gaz naturel|gnl|[eé]lectricit[eé]|[eé]nergie\w*|"
               r"[eé]nerg[eé]tique\w*|centrales? nucl[eé]aires?|nucl[eé]aire civil|parc nucl[eé]aire|"
               r"r[eé]acteurs?|edf|charbon|renouvelable\w*|[eé]olien\w*|solaire|carburants?|prix de l'essence|"
               r"l'essence|gazole|baril\w*|raffinerie\w*|gazoduc|ol[eé]oduc",
    "comercio": r"commerce\w*|commercia\w*|exportation\w*|importation\w*|exportateur\w*|importateur\w*|"
                r"balance commerciale|guerre commerciale|accord commercial|libre-[eé]change|libre [eé]change|omc|"
                r"cha[iî]nes? d'approvisionnement|douane\w*|mercosur",
    "aranceles": r"droits de douane|droits douaniers|tarifs douaniers|surtaxe\w*|guerre commerciale|antidumping|"
                 r"taxes? (?:sur les importations|douani[eè]res)",
    "elecciones": r"[eé]lections?|[eé]lectora\w*|[eé]lecteurs?|scrutin\w*|sondages?|pr[eé]sidentielle\w*|"
                  r"l[eé]gislatives?|municipales|second tour|premier tour|candidat\w*|r[eé]f[eé]rendum|urnes",
    "politica": r"gouvernement\w*|parlement\w*|assembl[eé]e nationale|s[eé]nat|premier ministre|ministres?|"
                r"pr[eé]sident\w*|[eé]lys[eé]e|matignon|macron|bayrou|lecornu|barnier|le pen|bardella|"
                r"m[eé]lenchon|(?-i:RN|LFI|PS|LR)|motion de censure|censure|dissolution|coalition|lois?|"
                r"projet de loi|d[eé]crets?|politique\w*|opposition|d[eé]mission\w*|manifestation\w*|49\.3|49-3|"
                r"chef de l'[eé]tat|chef du gouvernement|coup d'[eé]tat|putsch\w*",
    "guerra": r"guerres?|militaires?|arm[eé]es?|troupes|soldats|missiles?|drones|frappes|"
              r"frappes? (?:a[eé]riennes?|isra[eé]liennes?|russes?|am[eé]ricaines?|de missiles|de drones)|"
              r"bombarde\w*|attaques? (?:de|aux) (?:missiles|drones)|invasion|cessez-le-feu|tr[eê]ve|otan|hamas|"
              r"hezbollah|houthis|armes? nucl[eé]aires?|attentats?|otages",
    "geopolitica": r"g[eé]opoliti\w*|tensions?|diplomati\w*|sommets?|otan|onu|nations unies|"
                   r"conseil de s[eé]curit[eé]|souverainet[eé]|fronti[eè]res?|annexion|alliances?|trait[eé]s?|"
                   r"ambassade\w*|quai d'orsay|affaires [eé]trang[eè]res|bilat[eé]ra\w*|g7|g20|brics|"
                   r"d[eé]troit de ta[iï]wan|mer de chine|cor[eé]e du nord",
    "sanciones": r"sanctions?|sanctionn\w*|embargo\w*|liste noire|gel des avoirs|contr[oô]les? [aà] l'exportation|"
                 r"plafonnement du prix",
    "catastrofe": r"s[eé]ismes?|tremblements? de terre|tsunamis?|typhons?|ouragans?|cyclones?|tornades?|"
                  r"inondations?|crues?|intemp[eé]ries|incendies?|feux de for[eê]t|volcan\w*|[eé]ruption|"
                  r"s[eé]cheresse|canicule|pand[eé]mie|[eé]pid[eé]mie|explosions?|catastrophe\w*|d[eé]sastres?|"
                  r"panne g[eé]ante|blackout|glissements? de terrain",
    "tecnologia": r"intelligence artificielle|(?-i:IA)|puces?|semi-?conducteurs?|technologi\w*|tech|cyberattaque\w*|"
                  r"pirates? informatiques?|logiciels?|num[eé]rique|quantique|robots?|smartphones?|"
                  r"g[eé]ants? du num[eé]rique|centres? de donn[eé]es",
    "cripto": r"crypto\w*|cryptomonnaie\w*|bitcoin|ethereum|solana|xrp|stablecoins?|blockchain|jetons?|"
              r"euro num[eé]rique|binance|coinbase",
    "regulacion": r"r[eé]gulat\w*|r[eé]glementation\w*|r[eé]glementaire\w*|r[eé]gulateur\w*|(?-i:AMF|ACPR|SEC)|"
                  r"autorit[eé] de la concurrence|bruxelles|commission europ[eé]enne|amendes?|enqu[eê]tes?|"
                  r"proc[eè]s|tribunal|conseil d'[eé]tat|conseil constitutionnel|interdiction\w*|interdit|"
                  r"(?-i:MiCA|MICA)|"
                  r"directive|r[eè]glement",
    "inmobiliario": r"immobili\w*|logements?|loyers?|cr[eé]dits? immobiliers?|pr[eê]ts? immobiliers?|"
                    r"promoteurs?|hlm|fonci[eè]re\w*|b[aâ]timent",
}
NOISE["fr"] = {
    "deportes": r"football|ligue 1|ligue des champions|(?-i:PSG|OM|OL)|entra[iî]neur|mercato|coupe de france|"
                r"tennis|roland-garros|tour de france|jeux olympiques|rugby|ballon d'or|mbapp[eé]|"
                r"formule 1|coupe du monde",
    "famosos": r"presse people|les people|c[eé]l[eé]brit\w*|mariage|divorce|enceinte|rupture|star de|t[eé]l[eé]r[eé]alit[eé]",
    "horoscopo": r"horoscope\w*|astrolog\w*|signe astrologique|zodiaque",
    "recetas": r"recettes? de|cuisine|ingr[eé]dients|desserts?|g[aâ]teaux?|p[aâ]tisserie",
    "tiempo": r"m[eé]t[eé]o\w*|pluies?|neige|orages?|temp[eé]ratures?|chaleur|froid|m[eé]t[eé]o-france|"
              r"vigilance orange",
    "loterias": r"loto|euromillions|tirage du loto|fdj|grattage",
    "cultura": r"films?|cin[eé]ma|sortie en salles|albums?|concerts?|tourn[eé]e|festival\w*|mus[eé]es?|"
               r"th[eé][aâ]tre|op[eé]ra|s[eé]rie t[eé]l[eé]|c[eé]sars?|oscars?|chanteu\w*|acteur|actrice|"
               r"jeux? vid[eé]o",
    "ocio": r"id[eé]es de voyage|vacances|restaurants?|beaut[eé]|maquillage|minceur|jardinage|"
            r"animaux de compagnie|loisirs|bien-[eê]tre",
}

# --- Italiano ---
TERMS["it"] = {
    "macro": r"pil|recessione|economi\w*|crescita|manovra|legge di bilancio|bilancio|deficit|debito pubblico|btp|"
             r"spread|rendiment\w*|fmi|banca mondiale|ocse|pmi|istat|rating|agenzia di rating|euro|dollaro|valut\w*|"
             r"produzione industriale|consumi",
    "banco_central": r"bce|banca centrale|bankitalia|banca d'italia|(?-i:Fed|FED)|riserva federale|tassi|"
                     r"tassi d'interesse|tassi di interesse|taglio dei tassi|rialzo dei tassi|tasso di riferimento|"
                     r"politica monetaria|lagarde|panetta|powell",
    "inflacion": r"inflazione|prezzi al consumo|carovita|caro prezzi|deflazione|indice dei prezzi|rincari",
    "empleo": r"lavoro|occupazione|occupati|disoccupazione|disoccupati|licenziament\w*|esuberi|sciopero|scioperi|"
              r"sindacat\w*|salari\w*|stipendi|cassa integrazione|mercato del lavoro|assunzioni",
    "bolsa": r"borsa\w*|piazza affari|ftse mib|azioni|azionar\w*|mercati|wall street|nasdaq|dow jones|s&p|nikkei|"
             r"indici|titoli|quotazione|ipo|listino|crollo dei mercati|investitori",
    "empresas": r"utili|utile netto|ricavi|fatturato|trimestrale|risultati (?:trimestrali|semestrali|annuali)|"
                r"fusione|fusioni|acquisizion\w*|acquisisce|opa|dividend\w*|amministratore delegato|fallimento|"
                r"bancarotta|aziend\w*|impres[ae]|eni|enel|stellantis|intesa sanpaolo|unicredit|(?-i:Generali)|"
                r"telecom italia|(?-i:TIM)|mediobanca|pirelli|prada",
    "banca": r"banca|banche|bancari\w*|credito|prestiti|intesa sanpaolo|unicredit|mps|monte dei paschi|"
             r"mediobanca|banco bpm|depositi",
    "energia": r"petrolio|greggio|brent|opec\+?|gas|gas naturale|gnl|elettricit[aà]|energi\w*|bollett\w*|"
               r"centrali? nucleari?|energia nucleare|carbone|rinnovabil\w*|eolic\w*|solare|carburant\w*|benzina|"
               r"gasolio|barili?|raffineri\w*|gasdotto|oleodotto",
    "comercio": r"commercio|commercial\w*|esportazion\w*|importazion\w*|export|l'export|bilancia commerciale|"
                r"guerra commerciale|accordo commerciale|libero scambio|omc|catene? di approvvigionamento|"
                r"dogan\w*",
    "aranceles": r"dazi|dazio|tariffe doganali|tariffe (?:usa|americane|di trump|reciproche|commerciali)|"
                 r"(?:minacci|impon|annunci)\w* (?:nuove )?tariffe|guerra (?:commerciale|dei dazi)|antidumping",
    "elecciones": r"elezion\w*|elettora\w*|elettori|voto|votazion\w*|urne|sondaggi\w*|ballottaggio|candidat\w*|"
                  r"referendum|regionali|amministrative",
    "politica": r"governo|parlamento|camera dei deputati|senato|premier(?! league)|presidente del consiglio|"
                r"ministr\w*|meloni|salvini|tajani|schlein|mattarella|palazzo chigi|quirinale|fratelli d'italia|"
                r"(?-i:PD|M5S)|forza italia|coalizione|maggioranza|opposizione|legge|decret\w*|politic\w*|"
                r"dimission\w*|crisi di governo",
    "guerra": r"guerr[ae]|bellic\w*|militar\w*|esercito|truppe|soldati|missil\w*|droni|bombard\w*|raid|"
              r"attacchi (?:con|di) (?:missili|droni)|attacco (?:aereo|missilistico|con droni)|offensiva|"
              r"invasione|cessate il fuoco|tregua|(?-i:NATO|Nato)|hamas|hezbollah|houthi|armi nucleari|attentat\w*|"
              r"ostaggi",
    # «nato» en minúscula es «nacido»: la OTAN solo cuenta con mayúscula.
    "geopolitica": r"geopolitic\w*|tension\w*|diplomazi\w*|diplomatic\w*|vertice|summit|(?-i:NATO|Nato)|onu|"
                   r"nazioni unite|"
                   r"consiglio di sicurezza|sovranit[aà]|confin\w*|annessione|alleanza|trattato|ambasciat\w*|"
                   r"farnesina|bilateral\w*|g7|g20|brics|corea del nord",
    "sanciones": r"sanzion\w*|embargo|lista nera|congelamento dei beni|controlli sulle esportazioni|price cap",
    "catastrofe": r"terremoto|sisma|scosse? di terremoto|tsunami|tifone|uragano|ciclone|tornado|alluvion\w*|"
                  r"inondazion\w*|esondazion\w*|frana|frane|incendi\w*|vulcan\w*|eruzione|etna|siccit[aà]|"
                  r"ondata di calore|pandemia|epidemia|esplosione|catastrof\w*|disastr\w*|blackout",
    "tecnologia": r"intelligenza artificiale|(?-i:IA)|chip (?:per|di|avanzati)|produttori di chip|semicondutt\w*|"
                  r"tecnologi\w*|tech|cyber\w*|attacco hacker|"
                  r"hacker|software|digitale|quantistic\w*|robot\w*|smartphone",
    "cripto": r"cripto\w*|criptovalut\w*|bitcoin|ethereum|solana|xrp|stablecoin|blockchain|token|euro digitale",
    "regulacion": r"regolament\w*|regolator\w*|regolamentazione|consob|antitrust|agcm|bruxelles|"
                  r"commissione europea|multa|multe|sanzione|indagine|inchiesta|tribunale|"
                  r"corte (?:costituzionale|di cassazione|dei conti)|(?-i:TAR)|consiglio di stato|divieto|vieta|"
                  r"normativa|(?-i:MiCA|MICA|SEC)",
    "inmobiliario": r"immobiliar\w*|mercato immobiliare|prezzi delle case|mutui|mutuo|affitti|affitto|edilizia|"
                    r"superbonus|compravendite",
}
NOISE["it"] = {
    "deportes": r"calcio|serie a|juventus|juve|scudetto|campionato|allenatore|gol|tennis|sinner|motogp|"
                r"giro d'italia|olimpiadi|formula 1|coppa italia|champions league|derby",
    "famosos": r"vip|gossip|matrimonio|divorzio|incinta|fidanzat\w*|grande fratello",
    "horoscopo": r"oroscopo|segni zodiacali|zodiaco|astrolog\w*",
    "recetas": r"ricett\w*|cucina|ingredienti|dolci|torta",
    "tiempo": r"meteo|pioggia|piogge|neve|temporal\w*|temperature|caldo|freddo|maltempo|allerta gialla",
    "loterias": r"lotto|superenalotto|estrazion\w*|gratta e vinci|lotteria",
    "cultura": r"film|cinema|album|concert\w*|festival|sanremo|museo|musei|mostra|romanzo|teatro|serie tv|"
               r"cantante|attore|attrice",
    "ocio": r"vacanz\w*|ristorant\w*|moda|bellezza|trucco|dieta|giardinaggio|animali domestici|tempo libero|"
            r"benessere",
}

# --- Neerlandés ---
TERMS["nl"] = {
    "macro": r"bbp|recessie|economie\w*|economisch\w*|groei|begroting\w*|tekort\w*|staatsschuld|"
             r"staatsobligaties|obligaties|imf|wereldbank|oeso|pmi|(?-i:CBS|CPB)|euro|dollar|koopkracht|"
             r"consumentenvertrouwen",
    "banco_central": r"ecb|centrale bank|dnb|de nederlandsche bank|(?-i:Fed|FED)|rente\w*|renteverlaging\w*|"
                     r"renteverhoging\w*|rentebesluit|monetair beleid|knot|lagarde",
    "inflacion": r"inflatie|prijsstijging\w*|consumentenprijzen|cpi|hicp|deflatie",
    "empleo": r"werkloosheid|werkgelegenheid|banen|ontslag\w*|staking\w*|vakbond\w*|fnv|lonen|loon\w*|"
              r"minimumloon|arbeidsmarkt|cao",
    "bolsa": r"beurs\w*|aex|amx|aandelen|aandeel|wall street|nasdaq|dow jones|s&p|beleggers|koers\w*|beursgang|"
             r"euronext",
    "empresas": r"winst\w*|omzet\w*|kwartaalcijfers|jaarcijfers|fusie\w*|overname\w*|dividend\w*|topman|"
                r"faillissement\w*|failliet|bedrijf\w*|onderneming\w*|asml|(?-i:Shell)|philips|ahold|"
                r"unilever|heineken|(?-i:ING)|abn amro|prosus|adyen",
    "banca": r"bank|banken|banksector|bankensector|(?-i:ING)|abn amro|rabobank|kredie\w*|leningen|spaargeld",
    "energia": r"olie|olieprijs\w*|ruwe olie|brent|opec\+?|gas|aardgas|gasprijs\w*|lng|stroom|stroomprijs\w*|"
               r"elektriciteit|energie\w*|kernenergie|kerncentrale\w*|steenkool|windenergie|zonne-energie|"
               r"benzine|diesel|vaten|raffinaderij\w*|ttf",
    "comercio": r"handel|handelsoorlog|handelsakkoord|handelsbeleid|handelsconflict|export|exporten|import|importen|uitvoer|invoer|"
                r"handelsbalans|wto|toeleveringsketen\w*|douane|haven van rotterdam",
    # «tarieven» y «heffingen» sueltos son también los de la luz o los municipales: se exige contexto comercial.
    "aranceles": r"invoerrechten|invoerheffing\w*|importheffing\w*|importtarie\w*|invoertarie\w*|handelstarie\w*|"
                 r"(?:heffingen|tarieven) (?:op|tegen) (?:de )?(?:import|invoer)\w*|"
                 r"(?:amerikaanse|chinese|europese) (?:heffingen|tarieven)|(?:heffingen|tarieven) van trump|"
                 r"trumps? (?:heffingen|tarieven)|dreig\w* met (?:nieuwe )?(?:heffingen|tarieven)|handelsoorlog|"
                 r"antidumping",
    "elecciones": r"verkiezing\w*|kiezers|stembus|peiling\w*|lijsttrekker\w*|kandida\w*|referendum",
    "politica": r"kabinet\w*|regering\w*|tweede kamer|eerste kamer|parlement\w*|minister\w*|premier(?! league)|"
                r"schoof|jetten|wilders|(?-i:PVV|VVD|D66|CDA|BBB|NSC)|gl-pvda|coalitie\w*|oppositie|"
                r"wetsvoorstel\w*|wetgeving|politiek\w*|motie|aftreden|demonstratie\w*",
    "guerra": r"oorlog\w*|militair\w*|leger|troepen|soldaten|raket\w*|drones|luchtaanval\w*|raketaanval\w*|"
              r"aanval(?:len)? op|bombardement\w*|invasie|staakt-het-vuren|wapenstilstand|navo|hamas|hezbollah|"
              r"houthi's|kernwapens|wapen\w*|defensie|gijzelaars",
    "geopolitica": r"geopoliti\w*|spanningen|diplomat\w*|navo|(?-i:VN)|verenigde naties|veiligheidsraad|"
                   r"soevereiniteit|grens\w*|annexatie|bondgenoot\w*|verdrag|ambassade\w*|buitenlandse zaken|"
                   r"bilateraal|g7|g20|brics|noord-korea",
    "sanciones": r"sanctie\w*|embargo\w*|zwarte lijst|exportcontrole\w*|bevriezing van tegoeden",
    "catastrofe": r"aardbeving\w*|tsunami|tyfoon|orkaan|cycloon|tornado|overstroming\w*|watersnood|bosbrand\w*|"
                  r"natuurbrand\w*|vulkaan\w*|uitbarsting|droogte|hittegolf|pandemie|epidemie|explosie\w*|"
                  r"natuurramp\w*|rampgebied|rampen|stroomstoring",
    "tecnologia": r"kunstmatige intelligentie|(?-i:AI)|chipfabrikant\w*|chipmachine\w*|chipsector|chipindustrie|"
                  r"halfgeleider\w*|technologie\w*|tech|techbedrij\w*|cyberaanval\w*|hackers?|software|"
                  r"quantum\w*|robot\w*|smartphone\w*",
    "cripto": r"crypto\w*|bitcoin|ethereum|solana|xrp|stablecoin\w*|blockchain|tokens?|digitale euro",
    "regulacion": r"regulering|regelgeving|toezichthouder\w*|(?-i:AFM|ACM|SEC)|europese commissie|brussel|"
                  r"boete\w*|rechtbank|verbod\w*|(?-i:MiCA|MICA)",
    "inmobiliario": r"woningmarkt|woning\w*|huizenprijzen|hypothe\w*|huur\w*|huurprijzen|vastgoed\w*|woningbouw",
}
NOISE["nl"] = {
    "deportes": r"voetbal\w*|eredivisie|ajax|psv|feyenoord|oranje|doelpunt\w*|wielrennen|schaatsen|"
                r"verstappen|formule 1|olympische spelen",
    "famosos": r"bekende nederlander|bn'ers?|huwelijk|scheiding|zwanger|royals|showbizz",
    "horoscopo": r"horoscoop|sterrenbeeld\w*|astrologie",
    "recetas": r"recept\w*|koken|bakken|ingredi[eë]nten|toetje",
    "tiempo": r"het weer|weerbericht|weersverwachting|knmi|regen|sneeuw|onweer|temperaturen|code geel|code oranje",
    "loterias": r"loterij|staatsloterij|postcodeloterij|lotto",
    "cultura": r"film\w*|bioscoop|album|concert\w*|festival\w*|museum|tentoonstelling|theater|zanger\w*|acteur|"
               r"songfestival",
    "ocio": r"vakantie\w*|restaurant\w*|schoonheid|make-up|dieet|afvallen|tuinieren|huisdier\w*|vrije tijd",
}

# --- Japonés (sin límites de palabra; alternativas simples: sin «|» dentro de grupos, porque _wrap las separa) ---
# Sin límites, los términos cortos caen dentro de otras palabras: se tapan con (?<!...) y (?!...).
TERMS["ja"] = {
    "macro": "景気|経済|GDP|国内総生産|景気後退|リセッション|財政|予算|国債|長期金利|利回り|円安|円高|為替|ドル円|"
             "短観|PMI|鉱工業生産|経常収支|消費支出|IMF|格下げ|補正予算",
    "banco_central": "日銀|日本銀行|植田|利上げ|利下げ|金融政策|政策金利|金利|FRB|FOMC|パウエル|ECB|欧州中央銀行|"
                     "中央銀行|量的緩和|金融緩和|金融引き締め|マイナス金利|イールドカーブ",
    "inflacion": "インフレ|物価|消費者物価|CPI|デフレ|値上げ|物価上昇",
    "empleo": "雇用|失業|有効求人倍率|賃金|賃上げ|春闘|労働|解雇|リストラ|人員削減|ストライキ|人手不足",
    "bolsa": "日経平均|日経225|TOPIX|東証|株価|株式(?!会社)|株安|株高|日本株|米国株|米株|中国株|ハイテク株|半導体株|"
             "銀行株|関連株|相場|市場|ダウ(?!ン)|ナスダック|S&P|上場|IPO|先物|暴落|急落|急騰|反発|続落|続伸|売り越し|"
             "買い越し|投資家",
    "empresas": "決算|業績|純利益|営業利益|売上高|増益|減益|上方修正|下方修正|買収|合併|M&A|TOB|配当|自社株買い|"
                "社長|CEO|倒産|経営破綻|企業|トヨタ|ソニー|ソフトバンク|任天堂|日立|三菱|ホンダ|日産|パナソニック|"
                "キーエンス|ファーストリテイリング|ユニクロ|東京エレクトロン|アドバンテスト|ルネサス|セブン&アイ",
    "banca": "銀行|メガバンク|三菱UFJ|みずほ|三井住友|りそな|融資|預金|不良債権|地銀",
    "energia": "原油|石油|ガソリン|天然ガス|LNG|OPEC|エネルギー|電力|電気料金|原発|原子力発電|再稼働|"
               "再生可能エネルギー|太陽光|石炭|ブレント|WTI",
    "comercio": "貿易|輸出|輸入|貿易収支|貿易赤字|貿易黒字|通商|サプライチェーン|供給網|WTO|貿易摩擦|貿易戦争",
    "aranceles": "関税|追加関税|報復関税|貿易戦争|反ダンピング|相互関税",
    "elecciones": "選挙|総選挙|衆院選|参院選|知事選|大統領選|投票|開票|出口調査|初当選|当選確実|当確|落選|候補者|"
                  "世論調査|総裁選",
    "politica": "国会|首相|内閣|政権|政府|総理|自民党|立憲民主党|公明党|維新|与党|野党|衆院|参院|衆議院|参議院|"
                "官房長官|大臣|閣議|石破|高市|岸田|小泉|トランプ|習近平|プーチン|ゼレンスキー|政治|法案|予算案|"
                "解散|辞任|不信任|弾劾|抗議デモ",
    "guerra": "戦争|紛争|軍事|軍隊|国軍|米軍|ロシア軍|イスラエル軍|ウクライナ軍|中国軍|人民解放軍|ミサイル|空爆|"
              "爆撃|侵攻|侵略|停戦|休戦|NATO|ハマス|ヒズボラ|フーシ|核兵器|ドローン攻撃|ミサイル攻撃|大規模攻撃|"
              "軍事攻撃|(?<!サイバー)(?<!ウェア)(?<!ハッカー)攻撃(?![陣力的])|砲撃|戦闘|自衛隊|兵士|人質",
    "geopolitica": "地政学|緊張|外交|首脳会談|サミット|国連|安保理|安全保障|領土|領海|尖閣|台湾海峡|南シナ海|同盟|"
                   "条約|大使館|外相|外務省|二国間|G7|G20|BRICS|北朝鮮",
    "sanciones": "制裁|経済制裁|禁輸|輸出規制|輸出管理|資産凍結|エンティティーリスト",
    "catastrofe": "地震|震度|津波|台風|ハリケーン|サイクロン|竜巻|洪水|豪雨|大雨特別警報|浸水|土砂崩れ|山火事|火災|"
                  "噴火|干ばつ|猛暑|熱波|パンデミック|感染拡大|爆発|災害|被災|停電|大震災",
    "tecnologia": "人工知能|AI|生成AI|半導体|チップ|テクノロジー|技術|ハイテク|サイバー攻撃|ハッカー|ソフトウェア|"
                  "データセンター|量子|ロボット|スマホ|スマートフォン|エヌビディア|オープンAI|TSMC|ラピダス",
    "cripto": "暗号資産|仮想通貨|暗号通貨|ビットコイン|イーサリアム|ソラナ|リップル|ステーブルコイン|"
              "ブロックチェーン|BTC|ETH|XRP|トークン",
    "regulacion": "規制|金融庁|証券取引等監視委員会|公正取引委員会|公取委|独占禁止法|罰金|制裁金|課徴金|行政処分|"
                  "業務改善命令|訴訟|提訴|裁判所|最高裁|法改正|SEC|禁止",
    "inmobiliario": "不動産|住宅|地価|マンション|住宅ローン|REIT|リート|賃貸|家賃|公示地価|基準地価|恒大",
}
NOISE["ja"] = {
    "deportes": "サッカー|野球|プロ野球|大谷|甲子園|Jリーグ|五輪|オリンピック|大相撲|相撲|ゴルフ|テニス|W杯|"
                "ワールドカップ|侍ジャパン|阪神|巨人|試合|優勝|ラグビー|フィギュア|マラソン",
    "famosos": "芸能|俳優|女優|アイドル|結婚|離婚|熱愛|不倫|ジャニーズ|タレント",
    "horoscopo": "星座|占い|運勢",
    "recetas": "レシピ|料理|作り方|スイーツ|献立",
    "tiempo": "天気|天気予報|気温|雨|雪|晴れ|気象庁|寒波|暑さ|冷え込み",
    "loterias": "宝くじ|ロト|ジャンボ",
    "cultura": "映画|アニメ|ドラマ|アルバム|コンサート|公演|美術館|展覧会|小説|舞台|紅白|主演|漫画",
    "ocio": "グルメ|ファッション|美容|ダイエット|ペット|レジャー|温泉",
}

# --- Chino (simplificado y tradicional) ---
TERMS["zh"] = {
    "macro": "经济|經濟|GDP|国内生产总值|國內生產總值|衰退|财政|財政|预算|預算|国债|國債|债券|債券|收益率|人民币|"
             "人民幣|汇率|匯率|美元|港元|新台币|新台幣|PMI|工业增加值|工業增加值|社会消费品零售|社會消費品零售|"
             "经济增长|經濟成長|经济成长|赤字|IMF|国际货币基金|國際貨幣基金|降级|降級",
    "banco_central": "央行|中央银行|中央銀行|人民银行|人民銀行|美联储|美聯儲|联储|聯儲|聯準會|欧洲央行|歐洲央行|"
                     "日本央行|日银|日銀|降息|加息|升息|利率|基准利率|基準利率|LPR|MLF|降准|降準|存款准备金|"
                     "存款準備金|货币政策|貨幣政策|鲍威尔|鮑威爾|潘功胜|潘功勝|量化宽松|量化寬鬆|金管局",
    "inflacion": "通胀|通脹|通货膨胀|通貨膨脹|CPI|消费者物价|消費者物價|居民消费价格|居民消費價格|物价|物價|PPI|"
                 "通缩|通縮|涨价|漲價",
    "empleo": "就业|就業|失业|失業|非农|非農|裁员|裁員|罢工|罷工|工资|工資|薪资|薪資|劳动力|勞動力|招聘|劳工|勞工|"
              "最低工资|最低工資",
    "bolsa": "股市|股票|股价|股價|A股|港股|美股|台股|上证|上證|深证|深證|恒指|恒生指数|恒生指數|沪指|滬指|创业板|"
             "創業板|科创板|科創板|纳斯达克|納斯達克|道琼斯|道瓊|标普|標普|日经|日經|加权指数|加權指數|大盘|大盤|"
             "IPO|上市公司|首次上市|挂牌上市|掛牌上市|赴港上市|赴美上市|在港上市|港股上市|上市首日|分拆上市|借壳上市|"
             "借殼上市|申请上市|申請上市|退市|暴跌|大跌|大涨|大漲|期货|期貨|成交量|北向资金|北向資金|南向资金|南向資金|"
             "牛市|熊市|投资者|投資者",
    "empresas": "财报|財報|业绩|業績|净利|淨利|营收|營收|盈利|获利|獲利|亏损|虧損|并购|併購|收购|收購|合并|合併|"
                "股息|分红|分紅|回购|回購|首席执行官|執行長|总裁|總裁|破产|破產|企业|企業|公司|阿里巴巴|腾讯|騰訊|"
                "比亚迪|比亞迪|华为|華為|小米集团|小米集團|台积电|台積電|鸿海|鴻海|联发科|聯發科|宁德时代|寧德時代|"
                "美团|美團|京东|京東|拼多多|英伟达|輝達|特斯拉",
    "banca": "银行|銀行|贷款|貸款|存款|不良贷款|不良貸款|信贷|信貸|汇丰|匯豐|中信|国泰金|國泰金|富邦",
    "energia": "原油|石油|油价|油價|天然气|天然氣|液化天然气|液化天然氣|LNG|欧佩克|歐佩克|OPEC|能源|电力|電力|"
               "电价|電價|核电|核電|煤炭|新能源|光伏|风电|風電|汽油|柴油|布伦特|布蘭特|中石油|中石化|中海油|限电|"
               "限電",
    "comercio": "贸易|貿易|出口|进口|進口|外贸|外貿|贸易战|貿易戰|顺差|順差|逆差|供应链|供應鏈|世贸|世貿|WTO|通商|"
                "海关|海關|贸易协定|貿易協定|经贸|經貿|中美会谈|中美會談|美中会谈|美中會談|中美磋商|美中磋商",
    "aranceles": "关税|關稅|加征|加徵|贸易战|貿易戰|反倾销|反傾銷|报复性关税|報復性關稅|对等关税|對等關稅",
    "elecciones": "选举|選舉|大选|大選|投票|选民|選民|民调|民調|候选人|候選人|当选|當選|胜选|勝選|公投|罢免|罷免|"
                  "普选|普選|开票|開票|初选|初選",
    "politica": "国会|國會|议会|議會|立法会|立法會|立法院|预算案|預算案|政府|国务院|國務院|总统|總統|总理|總理|首相|"
                "行政院|赖清德|賴清德|习近平|習近平|李强|李強|全国人大|全國人大|人大常委会|人大常委會|政协|政協|"
                "政治局|两会|兩會|中共|共产党|共產黨|民进党|民進黨|国民党|國民黨|特朗普|川普|普京|普丁|泽连斯基|"
                "澤倫斯基|法案|弹劾|彈劾|辞职|辭職|抗议|抗議|示威|政变|政變|内阁|內閣|执政党|執政黨",
    "guerra": "战争|戰爭|开战|開戰|军事|軍事|军队|軍隊|导弹|導彈|飞弹|飛彈|空袭|空襲|轰炸|轟炸|袭击|襲擊|入侵|侵略|"
              "停火|休战|休戰|北约|北約|哈马斯|哈馬斯|真主党|真主黨|胡塞|核武|无人机袭击|無人機襲擊|炮击|砲擊|交火|"
              "冲突|衝突|以军|以軍|俄军|俄軍|解放军|解放軍|军演|軍演|战机|戰機|人质|人質",
    "geopolitica": "地缘政治|地緣政治|紧张|緊張|外交|峰会|峰會|联合国|聯合國|安理会|安理會|主权|主權|领土|領土|台海|"
                   "台湾海峡|台灣海峽|南海|同盟|盟友|条约|條約|大使馆|大使館|外长|外長|外交部|双边|雙邊|G7|G20|"
                   "金砖|金磚|朝鲜|北韓|中美关系|中美關係|美中关系|美中關係",
    "sanciones": "制裁|禁运|禁運|出口管制|实体清单|實體清單|黑名单|黑名單|冻结资产|凍結資產|资产冻结|資產凍結",
    "catastrofe": "地震|海啸|海嘯|台风|颱風|飓风|颶風|龙卷风|龍捲風|洪水|洪灾|洪災|暴雨|水灾|水災|山火|野火|火灾|"
                  "火災|火山|喷发|噴發|干旱|乾旱|热浪|熱浪|疫情|大流行|爆炸|灾害|災害|灾难|災難|泥石流|山体滑坡|"
                  "坍塌|停电|停電",
    "tecnologia": "人工智能|AI|芯片|晶片|半导体|半導體|科技|技术|技術|网络攻击|網路攻擊|黑客|駭客|软件|軟體|"
                  "数据中心|數據中心|資料中心|量子|机器人|機器人|智能手机|智慧型手機|手机|手機|英伟达|輝達|台积电|"
                  "台積電|DeepSeek|OpenAI",
    "cripto": "加密货币|加密貨幣|虚拟货币|虛擬貨幣|比特币|比特幣|以太坊|稳定币|穩定幣|区块链|區塊鏈|数字货币|"
              "數字貨幣|数字人民币|數字人民幣|币安|幣安|加密资产|加密資產",
    "regulacion": "监管|監管|证监会|證監會|金融监管总局|金融監管總局|金管会|金管會|反垄断|反壟斷|罚款|罰款|处罚|"
                  "處罰|被调查|被調查|接受调查|接受調查|展开调查|展開調查|启动调查|啟動調查|反垄断调查|反壟斷調查|"
                  "反倾销调查|反傾銷調查|法院|起诉|起訴|诉讼|訴訟|新规|新規|法规|法規|禁令|立案",
    "inmobiliario": "房地产|房地產|楼市|樓市|房价|房價|房市|住房|住宅|房贷|房貸|按揭|租金|恒大|碧桂园|碧桂園|万科|"
                    "萬科|开发商|開發商|地产|地產|物业|物業",
}
NOISE["zh"] = {
    "deportes": "足球|篮球|籃球|NBA|奥运|奧運|世界杯|世界盃|中超|英超|比赛|比賽|夺冠|奪冠|网球|網球|羽毛球|乒乓|"
                "棒球|中職|球队|球隊",
    "famosos": "明星|艺人|藝人|娱乐圈|娛樂圈|绯闻|緋聞|结婚|結婚|离婚|離婚|恋情|戀情|偶像",
    "horoscopo": "星座|运势|運勢|塔罗|塔羅|算命|生肖运程|生肖運程",
    "recetas": "食谱|食譜|菜谱|菜譜|做法|美食|料理|烹饪|烹飪|甜点|甜點",
    "tiempo": "天气|天氣|气温|氣溫|降雨|下雨|降雪|气象台|氣象局|寒流|冷空气|冷空氣",
    "loterias": "彩票|乐透|樂透|双色球|雙色球|大乐透|大樂透|六合彩|中奖|中獎",
    "cultura": "电影|電影|票房|电视剧|電視劇|专辑|專輯|演唱会|演唱會|博物馆|博物館|展览|展覽|小说|小說|综艺|綜藝|"
               "动漫|動漫",
    "ocio": "时尚|時尚|美容|减肥|減肥|宠物|寵物|休闲|休閒|网红|網紅",
}

# --- Coreano ---
TERMS["ko"] = {
    "macro": "경제|국내총생산|GDP|경기침체|경기 침체|경기부양|경기 둔화|경기둔화|경기 회복|경기회복|침체|성장률|재정|"
             "예산|국채|국고채|환율|원화|원·달러|달러|추경|추가경정|경상수지|산업생산|IMF|적자",
    "banco_central": "한국은행|(?<![가-힣])한은|기준금리|금리|금리인하|금리인상|금리 인하|금리 인상|연준|FOMC|파월|이창용|"
                     "금통위|금융통화위원회|통화정책|양적완화|ECB|유럽중앙은행|일본은행|중앙은행",
    "inflacion": "인플레이션|인플레|물가|소비자물가|CPI|생산자물가|디플레이션|물가상승",
    "empleo": "고용|실업|실업률|취업자|일자리|임금|최저임금|파업|노조|해고|구조조정|감원|노동|비농업",
    "bolsa": "코스피|코스닥|증시|주식|주가|증권|상장|IPO|공매도|순매수|순매도|나스닥|다우|S&P|닛케이|폭락|급락|급등|"
             "선물시장|지수선물|선물 시장|시가총액|밸류업|투자자",
    "empresas": "실적|영업이익|순이익|매출|어닝|흑자|인수|합병|M&A|배당|자사주|최고경영자|CEO|파산|기업|삼성전자|"
                "삼성바이오|삼성SDI|SK하이닉스|하이닉스|현대차|현대자동차|LG전자|LG에너지솔루션|LG화학|기아차|"
                "네이버|카카오|포스코|셀트리온|한화에어로스페이스|한화오션|한화그룹",
    "banca": "은행|시중은행|대출|예금|KB국민|국민은행|신한|하나은행|우리은행|금융지주|가계부채|연체율",
    "energia": "유가|원유|석유|국제유가|천연가스|LNG|OPEC|오펙|에너지|전력|전기요금|원전|원자력|석탄|신재생|태양광|"
               "휘발유|경유|정유|브렌트|WTI|한전|한국전력",
    "comercio": "무역|수출|수입|무역수지|통상|공급망|WTO|무역전쟁|무역 전쟁|무역분쟁|교역|관세청",
    "aranceles": "관세|보복관세|상호관세|관세율|무역전쟁|무역 전쟁|반덤핑",
    "elecciones": "선거|대선|총선|지방선거|투표|개표|출구조사|당선|낙선|후보자|대선 후보|여론조사|보궐선거|경선",
    "politica": "국회|대통령|정부|여당|야당|국무총리|총리|내각|탄핵|국민의힘|더불어민주당|민주당|조국혁신당|"
                "윤석열|이재명|한동훈|트럼프|시진핑|푸틴|젤렌스키|대통령실|장관|국정|법안|예산안|계엄|정치|사퇴|"
                "시위|집회",
    "guerra": "전쟁|교전|군사|미사일|공습|폭격|침공|휴전|정전협정|나토|NATO|하마스|헤즈볼라|후티|핵무기|드론 공격|"
              "미사일 공격|무력 공격|대규모 공격|(?<!사이버 )(?<!해킹 )공격(?![수진력적형])|포격|전투|북한군|러시아군|"
              "이스라엘군|군대|병력|인질",
    "geopolitica": "지정학|긴장|외교|정상회담|회담|유엔|안보리|안보|영토|대만해협|남중국해|동맹|조약|대사관|외교부|"
                   "외무장관|양자 회담|양자회담|양자 협상|양자협상|양자 관계|양자관계|G7|G20|브릭스|북한|미중 갈등|"
                   "미중갈등|한미동맹|한미 동맹",
    "sanciones": "제재|대북제재|금수|수출통제|수출 통제|블랙리스트|자산 동결|자산동결|엔티티 리스트",
    "catastrofe": "지진|쓰나미|태풍|허리케인|사이클론|토네이도|홍수|폭우|침수|산사태|산불|화재|화산|분화|가뭄|폭염|"
                  "팬데믹|감염병|폭발|재난|재해|참사|대정전|정전 사태",
    "tecnologia": "인공지능|AI|반도체|AI 칩|칩 수출|기술|테크|사이버 공격|해킹|해커|소프트웨어|데이터센터|양자|로봇|"
                  "스마트폰|엔비디아|HBM|파운드리",
    "cripto": "가상자산|가상화폐|암호화폐|비트코인|이더리움|솔라나|리플|스테이블코인|블록체인|업비트|빗썸|알트코인|"
              "코인 시장",
    "regulacion": "규제|금융위|금융위원회|금감원|금융감독원|공정위|공정거래위원회|과징금|벌금|소송|법원|대법원|"
                  "헌법재판소|헌재|금지|세무조사|압수수색|검찰 조사|당국 조사|조사 착수|SEC",
    "inmobiliario": "부동산|아파트|집값|주택|전세(?![계기])|월세|주담대|주택담보대출|분양|재건축|재개발|PF|프로젝트파이낸싱|임대",
}
NOISE["ko"] = {
    "deportes": "축구|야구|농구|올림픽|월드컵|손흥민|이강인|KBO|K리그|프로야구|우승|골프|배구|국가대표팀|경기에서",
    "famosos": "연예|연예인|배우|아이돌|열애|결혼|이혼|BTS|블랙핑크|방탄소년단|K팝",
    "horoscopo": "별자리|운세|타로|사주",
    "recetas": "레시피|요리|만드는 법|디저트",
    "tiempo": "날씨|기온|비 소식|눈 소식|기상청|한파|미세먼지",
    "loterias": "로또|복권|당첨번호",
    "cultura": "영화|드라마|앨범|콘서트|뮤지컬|전시회|박물관|소설|예능|컴백",
    "ocio": "맛집|패션|뷰티|다이어트|반려동물|레저|캠핑",
}

# --- Árabe (sin «ال» inicial: el envoltorio admite و، ف، ب، ل، ك y ال pegados, y sufijos cortos) ---
TERMS["ar"] = {
    "macro": r"اقتصاد|ناتج المحلي|ناتج محلي|ركود|نمو اقتصادي|ميزانية|موازنة|عجز|دين العام|دين عام|سندات|"
             r"عائدات السندات|صندوق النقد|بنك الدولي|عملة|دولار|ريال(?! مدريد)|درهم|جنيه|تصنيف ائتماني",
    "banco_central": r"بنك المركزي|مصرف المركزي|بنوك المركزية|أسعار الفائدة|سعر الفائدة|خفض الفائدة|رفع الفائدة|"
                     r"تثبيت الفائدة|احتياطي الفيدرالي|فيدرالي|باول|سياسة النقدية",
    "inflacion": r"تضخم|أسعار المستهلك|غلاء|ارتفاع الأسعار|انكماش",
    "empleo": r"بطالة|وظائف|توظيف|سوق العمل|أجور|رواتب|إضراب|تسريح|عمال|نقابات",
    "bolsa": r"بورصة|أسهم|سهم|سوق الأسهم|مؤشر|تداول|وول ستريت|ناسداك|داو جونز|سوق دبي|سوق أبوظبي|سوق المالية|"
             r"تاسي|اكتتاب|طرح عام|مستثمرين",
    "empresas": r"أرباح|إيرادات|نتائج مالية|صافي الربح|استحواذ|اندماج|توزيعات|رئيس التنفيذي|إفلاس|شركة|شركات|"
                r"أرامكو|سابك|إعمار|طيران الإمارات|موانئ دبي|أدنوك",
    "banca": r"بنك|بنوك|مصرف|مصارف|قروض|ودائع|ائتمان",
    "energia": r"نفط|خام|برنت|أوبك|أوبك\+|غاز|غاز طبيعي|غاز مسال|طاقة|كهرباء|طاقة نووية|محطة نووية|فحم|متجددة|"
               r"بنزين|ديزل|برميل|براميل|مصفاة|أرامكو",
    "comercio": r"تجارة|تجاري|صادرات|واردات|ميزان تجاري|حرب (?:ال)?تجارية|اتفاقية تجارية|سلاسل الإمداد|"
                r"منظمة التجارة|شحن|موانئ|قناة السويس|جمارك",
    # «رسوماً جمركية» lleva tanwín y «الرسوم الجمركية» lleva el artículo también en la segunda palabra.
    "aranceles": r"رسوم(?:اً|ًا|ا)? (?:ال)?جمركية|(?:ال)?جمركية|تعريفات (?:ال)?جمركية|تعريفات|رسوم على الواردات|"
                 r"حرب (?:ال)?تجارية|حرب (?:ال)?رسوم|إغراق",
    "elecciones": r"انتخاب|انتخابات|انتخابي|اقتراع|ناخبين|ناخبون|استطلاع|مرشح|مرشحين|استفتاء|صناديق الاقتراع|"
                  r"جولة الإعادة",
    # «رئيس» con la «ي» del sufijo es «principal» (الرئيسي): se excluye.
    "politica": r"حكومة|برلمان|مجلس النواب|مجلس الشعب|كنيست|رئيس الوزراء|وزير|وزراء|رئيس(?!ي)|بيت الأبيض|كرملين|ترامب|"
                r"بوتين|نتنياهو|أردوغان|بن سلمان|محمد بن زايد|سياسة|سياسي|احتجاجات|مظاهرات|انقلاب|قانون|مرسوم|"
                r"استقالة",
    "guerra": r"حرب|عسكري|جيش|قوات|صواريخ|صاروخ|غارة|غارات|قصف|هجوم|هجمات|اجتياح|غزو|وقف إطلاق النار|هدنة|ناتو|"
              r"حماس|حزب الله|حوثي|حوثيين|أسلحة نووية|مسيرات|طائرات مسيرة|اشتباكات|معارك|ضربات|ضربة عسكرية|"
              r"ضربة جوية|رهائن",
    "geopolitica": r"جيوسياسي|توتر|توترات|دبلوماسي|دبلوماسية|أمم المتحدة|مجلس الأمن|سيادة|حدود|تحالف|معاهدة|"
                   r"سفارة|وزير الخارجية|ثنائي|بريكس|مضيق هرمز|بحر الأحمر|كوريا الشمالية",
    "sanciones": r"عقوبات|حظر|قائمة سوداء|تجميد أصول|تجميد الأصول|قيود على التصدير",
    "catastrofe": r"زلزال|زلازل|تسونامي|إعصار|أعاصير|فيضان|فيضانات|سيول|حرائق|حريق|بركان|ثوران|جفاف|موجة حر|"
                  r"جائحة|وباء|انفجار|كارثة|كوارث|انهيار أرضي|انقطاع الكهرباء",
    "tecnologia": r"ذكاء اصطناعي|ذكاء الاصطناعي|رقائق|أشباه الموصلات|تكنولوجيا|تقنية|هجوم إلكتروني|سيبراني|"
                  r"اختراق|برمجيات|مراكز البيانات|كمومي|روبوت|هواتف ذكية|إنفيديا",
    "cripto": r"عملات مشفرة|عملات رقمية|عملات المشفرة|عملات الرقمية|بيتكوين|بتكوين|إيثريوم|سولانا|ريبل|"
              r"عملة مستقرة|بلوكتشين|بلوك تشين|أصول رقمية|أصول الرقمية|كريبتو",
    "regulacion": r"تنظيمي|تنظيم (?:ال)?(?:أسواق|سوق|قطاع|عملات)|"
                  r"هيئة الأوراق المالية|هيئة السوق|رقابة|رقابي|غرامة|غرامات|تحقيق|محكمة|قضاء|لوائح|"
                  r"تشريع",
    "inmobiliario": r"عقارات|عقاري|إسكان|سكن|مساكن|رهن عقاري|إيجار|إيجارات|مطورين|تطوير عقاري",
}
NOISE["ar"] = {
    "deportes": r"كرة القدم|دوري|مباراة|منتخب|أهداف|كأس العالم|ريال مدريد|برشلونة|نصر|أهلي|زمالك|"
                r"دوري أبطال|لاعب|مدرب",
    "famosos": r"فنان|فنانة|نجمة|مشاهير|زواج|طلاق",
    "horoscopo": r"أبراج|برج الحمل|حظك اليوم",
    "recetas": r"وصفة|وصفات|طبخ|مطبخ|حلويات|مكونات",
    "tiempo": r"طقس|أمطار|درجات الحرارة|أرصاد",
    "loterias": r"يانصيب|لوتو",
    "cultura": r"فيلم|أفلام|مسلسل|ألبوم|حفل غنائي|مهرجان|متحف|معرض فني|رواية|سينما",
    "ocio": r"مطعم|مطاعم|موضة|أزياء|رجيم|حمية|حيوانات أليفة",
}

# --- Hebreo (el envoltorio admite ו، ה، ב، ל، מ، ש، כ pegados y sufijos cortos) ---
TERMS["he"] = {
    "macro": r'כלכלה|כלכלי|תמ"ג|תמ״ג|צמיחה|מיתון|תקציב|גירעון|חובות|חוב ציבורי|חוב לאומי|אג"ח|אג״ח|אגרות חוב|תשואות|שקל|דולר|'
             r"דירוג אשראי|קרן המטבע",
    "banco_central": r"בנק ישראל|בנק המרכזי|ריבית|פד|פדרל ריזרב|הורדת ריבית|העלאת ריבית|מדיניות מוניטרית|"
                     r"אמיר ירון|פאוול",
    "inflacion": r"אינפלציה|מדד המחירים|מדד המחירים לצרכן|יוקר המחיה|התייקרות|עליית מחירים",
    "empleo": r"אבטלה|תעסוקה|מובטלים|פיטורים|שביתה|שביתות|הסתדרות|שכר|שכר מינימום|שוק העבודה",
    "bolsa": r'בורסה|מניות|מניה|ת"א 35|ת״א 35|ת"א 125|ת״א 125|וול סטריט|נאסד"ק|נאסד״ק|נאסדק|מסחר|הנפקה|נפילות|'
             r"משקיעים",
    "empresas": r'רווח|רווחים|הכנסות|דוחות כספיים|דוחות|רבעון|מיזוג|רכישה|דיבידנד|מנכ"ל|מנכ״ל|פשיטת רגל|חברה|'
                r"חברות|טבע תעשיות|אלביט|צ'ק פוינט",
    "banca": r"בנק|בנקים|בנק לאומי|בנק הפועלים|דיסקונט|מזרחי טפחות|הלוואות|אשראי|פיקדונות",
    "energia": r'נפט|גז|גז טבעי|אנרגיה|חשמל|מאגר לווייתן|מאגר תמר|אופ"ק|אופ״ק|דלק|בנזין|סולר|חבית|ברנט',
    "comercio": r"סחר|יצוא|יבוא|ייצוא|ייבוא|מאזן סחר|מלחמת ה?סחר|הסכם סחר|שרשרת אספקה|שרשראות אספקה|נמל|נמלים|"
                r"מכס",
    "aranceles": r"מכסים|מכס|מכסי|מלחמת ה?סחר|מלחמת ה?מכסים|היטל|היטלים|מכסי מגן",
    "elecciones": r"בחירות|קלפי|סקר|סקרים|מצביעים|הצבעה|מועמד|מועמדים|משאל עם|פריימריז",
    "politica": r"ממשלה|ממשלת|כנסת|ראש הממשלה|השר|שר האוצר|שר הביטחון|שרת|נתניהו|לפיד|גנץ|בנט|ליכוד|קואליציה|"
                r"אופוזיציה|חוק|הצעת חוק|פוליטי|פוליטיקה|טראמפ|פוטין|בית הלבן|הפגנה|הפגנות|מחאה|התפטרות",
    "guerra": r'מלחמה|מלחמת|צבא|צה"ל|צה״ל|טילים|טיל|תקיפה|תקיפות|הפצצה|הפצצות|פלישה|הפסקת אש|עזה|חמאס|'
              r"חיזבאללה|חות'ים|נשק גרעיני|כטב\"ם|כטב״ם|רחפן|לחימה|קרבות|יירוט|אזעקות|חטופים",
    "geopolitica": r'גיאופוליטי|מתיחות|דיפלומטי|דיפלומטיה|פסגה|או"ם|או״ם|מועצת הביטחון|ריבונות|גבול|סיפוח|'
                   r"ברית|הסכם|שגרירות|שר החוץ|נורמליזציה|הסכמי אברהם",
    "sanciones": r"סנקציות|סנקציה|עיצומים|אמברגו|רשימה שחורה|הקפאת נכסים",
    "catastrofe": r"רעידת אדמה|רעש אדמה|צונאמי|הוריקן|טייפון|ציקלון|טורנדו|שיטפון|שיטפונות|שריפה|שריפות|הר געש|"
                  r"התפרצות|בצורת|גל חום|מגפה|פנדמיה|פיצוץ|אסון|הפסקת חשמל",
    "tecnologia": r"בינה מלאכותית|שבבים|שבב|מוליכים למחצה|טכנולוגיה|הייטק|סייבר|מתקפת סייבר|האקרים|תוכנה|"
                  r"מחשוב קוונטי|רובוט|סמארטפון|אנבידיה",
    "cripto": r"קריפטו|ביטקוין|את'ריום|אתריום|סולנה|ריפל|מטבעות דיגיטליים|מטבע דיגיטלי|בלוקצ'יין|סטייבלקוין",
    "regulacion": r'רגולציה|רגולטור|רשות ניירות ערך|רשות התחרות|קנס|קנסות|חקירה|בית משפט|בג"ץ|בג״ץ|איסור|'
                  r"תקנות|פיקוח",
    "inmobiliario": r'נדל"ן|נדל״ן|דיור|דירות|דירה|משכנתא|משכנתאות|שכירות|מחירי הדירות|קבלנים|יזמים',
}
NOISE["he"] = {
    "deportes": r"כדורגל|כדורסל|ליגת העל|מכבי|הפועל|ספורט|נבחרת|אליפות|שוער",
    "famosos": r"סלב|סלבס|סלבריטאים|מפורסמים|חתונה|גירושים|זוגיות|הריון",
    "horoscopo": r"הורוסקופ|אסטרולוגיה|מזלות",
    "recetas": r"מתכון|מתכונים|בישול|אפייה|קינוח",
    "tiempo": r"מזג האוויר|מזג אוויר|גשם|גשמים|שרב",
    "loterias": r"לוטו|מפעל הפיס|הגרלה",
    "cultura": r"סרט|סרטים|סדרה|אלבום|הופעה|פסטיבל|מוזיאון|תערוכה|תיאטרון|אירוויזיון",
    "ocio": r"טיול|טיולים|חופשה|מסעדה|מסעדות|אופנה|יופי|דיאטה|חיות מחמד",
}

# --- Ruso ---
TERMS["ru"] = {
    "macro": r"ввп|рецесси\w*|экономик\w*|экономическ\w*|бюджет\w*|дефицит\w*|госдолг\w*|облигаци\w*|офз|"
             r"доходност\w*|рубл\w*|курс доллара|курс рубля|доллар\w*|юан\w*|мвф|всемирн\w* банк\w*|"
             r"кредитн\w* рейтинг\w*|суверенн\w* рейтинг\w*|минфин\w*|промпроизводств\w*|розничн\w* продаж\w*",
    "banco_central": r"центробанк\w*|(?-i:ЦБ)|банк росси\w*|центральн\w* банк\w*|ключев\w* ставк\w*|"
                     r"процентн\w* ставк\w*|ставк\w* (?:цб|фрс|ецб)|(?:повыси|понизи|снизи|сохрани)\w* ставк\w*|"
                     r"ставк\w* рефинансировани\w*|набиуллин\w*|фрс|федрезерв\w*|пауэлл\w*|ецб|"
                     r"денежно-кредитн\w*|монетарн\w*",
    "inflacion": r"инфляци\w*|потребительск\w* цен\w*|индекс потребительских цен|ипц|рост цен|дефляци\w*|"
                 r"подорожани\w*|росстат",
    "empleo": r"безработиц\w*|занятост\w*|рын(?:ок|ке|ка) труда|зарплат\w*|заработн\w* плат\w*|увольнени\w*|"
              r"сокращени\w* штата|забастовк\w*|профсоюз\w*|вакансии|мрот",
    "bolsa": r"бирж\w*|мосбирж\w*|индекс мосбиржи|ртс|акци(?:и|й|ям|ями|ях)|фондов\w* рын\w*|котировк\w*|"
             r"уолл-стрит|торги|торгов\w* сесси\w*|обвал\w*|ралли|фьючерс\w*|инвестор\w*",
    "empresas": r"прибыл\w*|выручк\w*|отч[её]тност\w*|финансов\w* результат\w*|слияни\w*|поглощени\w*|дивиденд\w*|"
                r"байбэк|гендиректор\w*|генеральн\w* директор\w*|банкротств\w*|компани\w*|корпораци\w*|"
                r"газпром\w*|роснефт\w*|лукойл\w*|сбербанк\w*|сбер|новатэк\w*|яндекс\w*|норникел\w*|аэрофлот\w*|"
                r"втб|алроса|северстал\w*|русал\w*",
    "banca": r"банк|банка|банке|банку|банком|банки|банков|банкам|банками|банках|банковск\w*|сбербанк\w*|втб|"
             r"альфа-банк\w*|т-банк\w*|кредит\w*|вклад\w*|депозит\w*",
    # «Газа» con mayúscula es Gaza; el gas va en minúscula.
    "energia": r"нефт\w*|brent|опек\w*|газ|(?-i:газа)|газу|газом|газов\w*|природн\w* газ\w*|спг|энерги\w*|"
               r"тариф\w* на (?:газ|свет|электроэнерги|электричеств)\w*|"
               r"электроэнерги\w*|электричеств\w*|уголь|угля|аэс|атомн\w* электростанц\w*|бензин\w*|дизел\w*|"
               r"баррел\w*|нпз|трубопровод\w*|газопровод\w*|северн\w* пото\w*",
    "comercio": r"торговл\w*|экспорт\w*|импорт\w*|внешнеторгов\w*|торгов\w* войн\w*|торгов\w* соглашени\w*|вто|"
                r"цепочк\w* поставок|таможн\w*",
    # «тариф» suelto es sobre todo el de la luz, el gas o el transporte (ЖКХ): se exige contexto comercial.
    "aranceles": r"пошлин\w*|тариф\w* на (?:импорт|ввоз)\w*|импортн\w* тариф\w*|заградительн\w* тариф\w*|"
                 r"тарифн\w* войн\w*|(?:ответн|взаимн|зеркальн|американск)\w* тариф\w*|"
                 r"тариф\w* (?:трампа|сша|против|в отношении)|(?:пригроз|угрож|грози)\w* (?:\w+ )?тариф\w*|"
                 r"торгов\w* войн\w*|антидемпинг\w*",
    "elecciones": r"выбор(?:ы|ах|ов|ам)|выборн\w*|избират\w*|голосовани\w*|кандидат\w*|референдум\w*|праймериз|"
                  r"второй тур|экзитпол\w*|(?-i:ЦИК)",
    "politica": r"правительств\w*|госдум\w*|парламент\w*|президент\w*|премьер(?!-лиг)|премьер-министр\w*|"
                r"министр\w*|кремл\w*|путин\w*|мишустин\w*|песков\w*|лавров\w*|трамп\w*|зеленск\w*|"
                r"верховн\w* рад\w*|закон\w*|законопроект\w*|указ|указом|указа|указе|политик\w*|политическ\w*|"
                r"оппозици\w*|отставк\w*|протест\w*|митинг\w*|импичмент\w*|переворот\w*",
    "guerra": r"войн\w*|военн\w*|арми\w*|войск\w*|(?-i:ВСУ|СВО)|солдат\w*|ракет\w*|дрон\w*|беспилотник\w*|бпла|"
              r"ракетн\w* удар\w*|удар\w* по|авиаудар\w*|удары|обстрел\w*|бомбардировк\w*|вторжени\w*|"
              r"перемири\w*|прекращени\w* огня|нато|хамас\w*|хезболл\w*|хусит\w*|ядерн\w* оружи\w*|фронт\w*|"
              r"атак\w* (?:дрон|беспилотник|ракет)\w*|заложник\w*",
    "geopolitica": r"геополит\w*|напряж[её]нност\w*|дипломат\w*|саммит\w*|оон|совбез\w*|совет\w* безопасности|"
                   r"суверенитет\w*|границ\w*|аннекси\w*|альянс\w*|договор\w*|посольств\w*|(?-i:МИД)|"
                   r"двусторонн\w*|брикс|g7|g20|больш\w* семерк\w*|больш\w* двадцатк\w*|кндр|северн\w* коре\w*",
    "sanciones": r"санкци\w*|эмбарго|черн\w* списк\w*|заморозк\w* активов|экспортн\w* контрол\w*|"
                 r"потол\w* цен\w* на (?:российск\w* )?нефть",
    "catastrofe": r"землетрясени\w*|цунами|тайфун\w*|ураган\w*|торнадо|наводнени\w*|паводк\w*|лесн\w* пожар\w*|"
                  r"пожар\w*|извержени\w*|вулкан\w*|засух\w*|аномальн\w* жар\w*|пандеми\w*|эпидеми\w*|взрыв\w*|"
                  r"катастроф\w*|стихийн\w* бедстви\w*|блэкаут\w*|отключени\w* электроэнерги\w*",
    "tecnologia": r"искусственн\w* интеллект\w*|(?-i:ИИ)|чип|чипы|чипов|полупроводник\w*|микросхем\w*|технологи\w*|"
                  r"кибератак\w*|хакер\w*|программн\w* обеспечени\w*|софт|дата-центр\w*|"
                  r"центр\w* обработки данных|квантов\w*|робот\w*|смартфон\w*",
    "cripto": r"крипт\w*|криптовалют\w*|биткоин\w*|биткойн\w*|эфириум\w*|солан\w*|стейблкоин\w*|блокчейн\w*|"
              r"цифров\w* рубл\w*|токен\w*|майнинг\w*|цифров\w* валют\w*",
    "regulacion": r"регулир\w*|регулятор\w*|регулировани\w*|(?-i:ФАС)|штраф\w*|расследовани\w*|суд|суда|суде|"
                  r"судом|суды|арбитраж\w*|запрет\w*|законодательств\w*|надзор\w*|роскомнадзор|прокуратур\w*",
    "inmobiliario": r"недвижимост\w*|жиль\w*|жилищн\w*|ипотек\w*|квартир\w*|новостро\w*|застройщик\w*|аренд\w*|"
                    r"девелопер\w*",
}
NOISE["ru"] = {
    "deportes": r"футбол\w*|хокке\w*|кхл|рпл|матч\w*|гол|голы|тренер\w*|чемпионат\w*|олимпи\w*|теннис\w*|"
                r"спартак\w*|зенит\w*|цска|сборн\w*|болельщик\w*",
    "famosos": r"знаменитост\w*|свадьб\w*|развод\w*|беременн\w*|шоу-бизнес\w*|певиц\w*|актрис\w*|блогер\w*",
    "horoscopo": r"гороскоп\w*|астролог\w*|знак\w* зодиака|таро",
    "recetas": r"рецепт\w*|готовить|блюд\w*|выпечк\w*|десерт\w*",
    "tiempo": r"погод\w*|синоптик\w*|осадк\w*|дожд\w*|снег\w*|мороз\w*|температур\w*|гидрометцентр\w*",
    "loterias": r"лотере\w*|джекпот\w*|розыгрыш\w*",
    "cultura": r"фильм\w*|кино\w*|сериал\w*|альбом\w*|концерт\w*|гастрол\w*|фестивал\w*|музе\w*|выставк\w*|"
               r"театр\w*|евровидени\w*",
    "ocio": r"путешестви\w*|отпуск\w*|ресторан\w*|мода|моды|красот\w*|диет\w*|похудени\w*|питом\w*|досуг\w*",
}

# --- Ucraniano ---
TERMS["uk"] = {
    "macro": r"ввп|рецесі\w*|економік\w*|економічн\w*|бюджет\w*|дефіцит\w*|держборг\w*|облігаці\w*|овдп|гривн\w*|"
             r"гривень|долар\w*|мвф|світов\w* банк\w*|кредитн\w* рейтинг\w*|мінфін\w*",
    "banco_central": r"(?-i:НБУ)|нацбанк\w*|національн\w* банк\w*|обліков\w* ставк\w*|ставк\w* нбу|фрс|"
                     r"федрезерв\w*|єцб|монетарн\w*|пишн(?:ий|ого)",
    "inflacion": r"інфляці\w*|споживч\w* цін\w*|індекс споживчих цін|подорожчанн\w*|зростання цін|держстат\w*",
    "empleo": r"безробітт\w*|зайнятіст\w*|зайнятост\w*|ринок праці|ринку праці|зарплат\w*|заробітн\w* плат\w*|"
              r"звільнен\w*|страйк\w*|профспілк\w*|вакансі\w*",
    "bolsa": r"бірж\w*|акці(?:ї|й|ям|ями|ях)|фондов\w* ринк\w*|котирув\w*|торги|індекс\w*|обвал\w*|ф'ючерс\w*|"
             r"інвестор\w*",
    "empresas": r"прибут\w*|виручк\w*|звітніст\w*|злитт\w*|поглинанн\w*|придбанн\w*|дивіденд\w*|гендиректор\w*|"
                r"генеральн\w* директор\w*|банкрутств\w*|компані\w*|корпораці\w*|нафтогаз\w*|укрнафт\w*|"
                r"метінвест\w*|кернел\w*|мхп",
    "banca": r"банк|банку|банки|банків|банками|банках|банком|банківськ\w*|приватбанк\w*|ощадбанк\w*|монобанк\w*|"
             r"кредит\w*|депозит\w*|вклад\w*",
    "energia": r"нафт\w*|brent|опек\w*|газ|газу|газом|газов\w*|природн\w* газ\w*|енерг\w*|електроенерг\w*|"
               r"відключенн\w* світла|графік\w* відключень|тариф\w* на (?:світло|газ|електр)\w*|"
               r"аес|вугілл\w*|бензин\w*|дизел\w*|барел\w*|нпз|"
               r"трубопровід\w*|газопровід\w*|укренерго|енергоатом\w*",
    "comercio": r"торгівл\w*|торговельн\w*|експорт\w*|імпорт\w*|зовнішньоторгов\w*|(?-i:СОТ)|"
                r"ланцюг\w* постачання|митн\w*|зерн\w* коридор\w*",
    # Como en ruso, «тариф» suelto es sobre todo el de la luz o el gas.
    "aranceles": r"мито|мита|мит|митом|митами|митах|тариф\w* на (?:імпорт|ввезенн)\w*|імпортн\w* тариф\w*|"
                 r"загороджувальн\w* тариф\w*|тарифн\w* війн\w*|(?:взаємн|дзеркальн|американськ)\w* тариф\w*|"
                 r"тариф\w* (?:трампа|сша|проти|щодо)|(?:пригроз|погрож|загрож)\w* (?:\w+ )?тариф\w*|"
                 r"торговельн\w* війн\w*|антидемпінг\w*",
    "elecciones": r"вибор(?:и|ах|ів|ам)|виборч\w*|виборц\w*|голосуванн\w*|кандидат\w*|референдум\w*|другий тур|"
                  r"екзитпол\w*|(?-i:ЦВК)",
    "politica": r"уряд\w*|верховн\w* рад\w*|(?-i:Рада)|парламент\w*|президент\w*|прем['’]єр\w*|міністр\w*|"
                r"зеленськ\w*|свириденко|офіс президента|закон\w*|законопроект\w*|указ|указом|політик\w*|"
                r"політичн\w*|опозиці\w*|відставк\w*|протест\w*|мітинг\w*|кремл\w*|путін\w*|трамп\w*|кабмін\w*",
    "guerra": r"війн\w*|військов\w*|армі\w*|(?-i:ЗСУ)|солдат\w*|ракет\w*|дрон\w*|безпілотник\w*|бпла|шахед\w*|"
              r"удар\w* по|ракетн\w* удар\w*|масован\w* удар\w*|удари|обстріл\w*|бомбардуванн\w*|вторгненн\w*|"
              r"контрнаступ\w*|наступальн\w*|перемир['’]я|припиненн\w* вогню|нато|хамас|ядерн\w* збро\w*|фронт\w*|"
              r"бойов\w*|атак\w* (?:дрон|безпілотник|шахед|ракет)\w*|масован\w* атак\w*|окупант\w*|окупаці\w*|"
              r"повітрян\w* тривог\w*|полонен\w*",
    "geopolitica": r"геополіт\w*|напруженіст\w*|дипломат\w*|саміт\w*|оон|радбез\w*|рад\w* безпеки|"
                   r"суверенітет\w*|кордон\w*|анексі\w*|альянс\w*|договір|договор\w*|"
                   r"угод(?:а|и|і|у|ою|ам|ами|ах)?|посольств\w*|"
                   r"(?-i:МЗС)|сибіг\w*|двосторонн\w*|брікс|g7|g20|єс|євросоюз\w*",
    "sanciones": r"санкці\w*|ембарго|чорн\w* списк\w*|заморожуванн\w* активів|експортн\w* контрол\w*|"
                 r"обмеженн\w* цін\w*",
    "catastrofe": r"землетрус\w*|цунамі|тайфун\w*|ураган\w*|повін\w*|паводк\w*|пожеж\w*|виверженн\w*|вулкан\w*|"
                  r"посух\w*|аномальн\w* спек\w*|пандемі\w*|епідемі\w*|вибух\w*|катастроф\w*|стихійн\w* лих\w*|"
                  r"блекаут\w*",
    "tecnologia": r"штучн\w* інтелект\w*|(?-i:ШІ)|чип|чипи|чипів|напівпровідник\w*|технологі\w*|кібератак\w*|"
                  r"хакер\w*|програмн\w* забезпеченн\w*|дата-центр\w*|квантов\w*|роботизац\w*|робототехн\w*|"
                  r"смартфон\w*",
    "cripto": r"крипт\w*|криптовалют\w*|біткоїн\w*|біткойн\w*|ефіріум\w*|стейблкоїн\w*|блокчейн\w*|"
              r"цифров\w* гривн\w*|токен\w*|майнінг\w*",
    "regulacion": r"регулюванн\w*|регулятор\w*|(?-i:НКЦПФР|АМКУ|НАБУ|САП|ВАКС)|штраф\w*|розслідуванн\w*|суд|суду|"
                  r"суді|судом|суди|заборон\w*|законодавств\w*|нагляд\w*|прокуратур\w*",
    "inmobiliario": r"нерухоміст\w*|житл\w*|іпотек\w*|квартир\w*|новобудов\w*|забудовник\w*|оренд\w*|"
                    r"девелопер\w*",
}
NOISE["uk"] = {
    "deportes": r"футбол\w*|матч\w*|гол|голи|тренер\w*|чемпіонат\w*|олімпі\w*|теніс\w*|збірн\w*|динамо|шахтар\w*|"
                r"вболівальник\w*",
    "famosos": r"знаменитост\w*|зірк\w*|весілл\w*|розлучен\w*|вагітн\w*|шоу-бізнес\w*|співачк\w*|акторк\w*",
    "horoscopo": r"гороскоп\w*|астролог\w*|знак\w* зодіаку",
    "recetas": r"рецепт\w*|страв\w*|випічк\w*|десерт\w*",
    "tiempo": r"погод\w*|синоптик\w*|опад\w*|дощ\w*|сніг\w*|мороз\w*|температур\w*|укргідрометцентр\w*",
    "loterias": r"лотере\w*|джекпот\w*|розіграш\w*",
    "cultura": r"фільм\w*|кіно\w*|серіал\w*|альбом\w*|концерт\w*|гастрол\w*|фестивал\w*|музе\w*|виставк\w*|"
               r"театр\w*|євробаченн\w*",
    "ocio": r"подорож\w*|відпустк\w*|ресторан\w*|мода|моди|дієт\w*|схудн\w*|домашні тварини|дозвілл\w*",
}

# Frases que se parecen a un tema pero son otro: se tapan antes de buscar ese tema.
# «Guerra comercial», «huelga» o «el terremoto golpea» no son guerra; «Banco de Japón» no es banca (es banco central);
# «sin embargo» no es un embargo; la tarifa de la luz no es un arancel.
_MASKS = {
    "guerra": re.compile(
        r"(?:trade|tariff|price|currency|chip|tech|bidding|subsidy) wars?|war chest|war for talent|star wars|"
        r"war on (?:drugs|poverty|inflation|talent|prices)|"
        r"(?:workers|union|general|labou?r|rail|dock|port|auto|transit|teachers'?|nurses'?|doctors'?|pilots'?|staff|"
        r"employees'?|machinists'?|writers'?|actors'?|miners'?|drivers'?|dockworkers'?|autoworkers'?|guild) strikes?|"
        r"strikes? (?:action|ballot|vote)|on strike|go(?:es)? on strike|strike price|"
        r"lightning strikes?|strik(?:e|es|ing) (?:a |an )?(?:\w+ )?(?:deal|agreement|pact|accord|balance|tone)|"
        r"(?:earthquake|quake|hurricane|typhoon|storm|tornado|cyclone|tsunami|disaster|tragedy|flood|wildfire|"
        r"blizzard|heat ?wave|pandemic|virus|outbreak)s?(?: [A-Z]\w+)? (?:strikes?|struck)|"
        r"guerras? (?:comercia(?:l|les|is)|arancelarias?|de precios|de divisas|tecnol[oó]gicas?)|"
        r"guerras? de (?:los )?aranceles|guerras? de tarifas|"
        r"guerras? (?:dos pre[çc]os|tarif[aá]rias?)|handelskrieg\w*|zollkrieg\w*|preiskrieg\w*|"
        r"guerres? (?:commerciales?|des prix|tarifaires?|des tarifs|des droits de douane)|"
        r"guerr[ae] (?:dei dazi|dei prezzi|commercial[ei])|"
        r"handelsoorlog\w*|prijsoorlog\w*|贸易战争?|貿易戰爭?|关税战|關稅戰|价格战|價格戰|贸易冲突|貿易衝突|"
        r"貿易戦争|関税戦争|価格戦争|通貨戦争|무역\s?전쟁|관세\s?전쟁|가격\s?전쟁|환율\s?전쟁|"
        r"(?:ال)?حرب (?:ال)?(?:تجارية|رسوم|أسعار)|מלחמת ה?(?:סחר|מכסים|מחירים)|"
        r"торгов\w* войн\w*|ценов\w* войн\w*|валютн\w* войн\w*|тарифн\w* войн\w*|"
        r"торговельн\w* війн\w*|цінов\w* війн\w*|тарифн\w* війн\w*", re.I),
    "banca": re.compile(
        r"central banks?|bank of (?:japan|england|korea|canada|russia|israel|mexico|thailand|france|italy|spain)|"
        r"reserve banks?(?: of \w+)?|people'?s bank of china|swiss national bank|national bank of \w+|world bank|"
        r"bank indonesia|banco central|banco de (?:españa|m[eé]xico|portugal|francia|italia|jap[oó]n|inglaterra)|"
        r"banco de la rep[uú]blica|banco mundial|bundesbank|zentralbank\w*|notenbank\w*|nationalbank|weltbank|"
        r"banque centrale|"
        r"banque de france|banque du japon|banque mondiale|banca centrale|banca d'italia|banca mondiale|"
        r"centrale bank|nederlandsche bank|wereldbank|日本銀行|日銀|中央銀行|中央银行|人民银行|人民銀行|世界银行|"
        r"世界銀行|韓国銀行|韩国银行|韓國銀行|한국은행|중앙은행|일본은행|세계은행|بنك المركزي|مصرف المركزي|"
        r"بنك الدولي|בנק ישראל|הבנק המרכזי|הבנק העולמי|банк росси\w*|центральн\w* банк\w*|центробанк\w*|"
        r"всемирн\w* банк\w*|народн\w* банк\w*|национальн\w* банк\w*|нацбанк\w*|національн\w* банк\w*|"
        r"світов\w* банк\w*", re.I),
    "sanciones": re.compile(r"sin embargo|embargos? de declaraç[aã]o|embargos infringentes", re.I),
    "aranceles": re.compile(
        r"(?:energy|electricity|gas|power|water|feed-in|broadband|mobile|phone|rail|train|bus|transport|taxi|"
        r"utility) tariffs?", re.I),
}

_GROUPS = {lang: (lang, "en") for lang in LANGS}
_GROUPS.update({"en": ("en",), "ru": ("ru", "uk", "en"), "uk": ("uk", "ru", "en")})   # ruso y ucraniano se mezclan

# Falsos amigos: palabras inglesas que en otro idioma son corrientes y significan otra cosa (alemán «war» = «era»,
# francés «premier» = «primero», italiano «modi» = «maneras»...). Con ese idioma, el inglés no las cuenta sueltas.
_FALSE_FRIENDS = {
    "de": "war|gilt|bund|stock|probe|coup|chef|chefs|golf",
    "fr": "premier|coup|stock|stocks|chef|chefs",
    "it": "modi|albanese|crude|stock|stocks|opera|anime",
    "es": "terror|fiscal|boe|stock|stocks|opera|anime",
    "pt": "terror|stock|stocks|opera|anime",
    "nl": "rent|ether|chef|chefs|golf",
}

# Sin idioma conocido se busca solo en los idiomas que comparten escritura con el texto: así las siglas latinas de
# las listas asiáticas (PF, TOB...) no saltan en un titular en inglés.
# Rangos: kana U+3040-30FF y han U+3400-4DBF, U+4E00-9FFF, U+F900-FAFF; hangul U+1100-11FF, U+3130-318F,
# U+AC00-D7AF; árabe U+0600-06FF; hebreo U+0590-05FF; cirílico U+0400-04FF.
_SCRIPTS = {
    "latina": (re.compile(r"[a-z]", re.I), ("en", "es", "pt", "de", "fr", "it", "nl")),
    "han_kana": (re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]"), ("ja", "zh")),
    "hangul": (re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]"), ("ko",)),
    "arabe": (re.compile(r"[؀-ۿ]"), ("ar",)),
    "hebrea": (re.compile(r"[֐-׿]"), ("he",)),
    "cirilica": (re.compile(r"[Ѐ-ӿ]"), ("ru", "uk")),
}

MAX_LEN = 400   # caracteres que se analizan: un titular no necesita más y así se acota el tiempo de las expresiones


def _wrap(lang: str, terms: str, block: str = "") -> str:
    """Pone a las alternativas de un idioma los límites que le corresponden.
    `block`: palabras que no cuentan sueltas (los falsos amigos del inglés)."""
    if lang in _CJK:
        parts = terms.split("|")
        return "|".join(rf"(?<![a-z0-9]){p}(?![a-z0-9])" if re.search(r"[A-Za-z0-9]", p) else p for p in parts)
    if lang == "ar":
        return rf"(?<!\w)(?:وال|بال|فال|كال|لل|ال|و|ف|ب|ل|ك)?(?:{terms})(?:ات|ية|ة|ه|ي|ين|ون|ان|ها|هم)?(?!\w)"
    if lang == "he":
        return rf"(?<!\w)[ובלמשהכ]{{0,3}}(?:{terms})(?:יות|ים|ות|ה|ת|י|ו)?(?!\w)"
    if block:
        return rf"(?<!\w)(?!(?:{block})(?!\w))(?:{terms})(?!\w)"
    return rf"(?<!\w)(?:{terms})(?!\w)"


def _compile(table: dict[str, dict[str, str]], keys, langs, block: str = "") -> dict[str, re.Pattern]:
    """Una expresión por clave con todos los idiomas de `langs`; `block` se aplica solo al inglés."""
    out = {}
    for k in keys:
        parts = [_wrap(lang, table[lang][k], block if lang == "en" else "")
                 for lang in langs if table.get(lang, {}).get(k)]
        if parts:
            out[k] = re.compile("|".join(parts), re.I)
    return out


# Todo se compila una sola vez, al importar: por idioma (con el inglés) y por escritura (para cuando no hay idioma).
_BY_LANG = {lang: _compile(TERMS, TOPICS, g, _FALSE_FRIENDS.get(lang, "")) for lang, g in _GROUPS.items()}
_BY_SCRIPT = {s: _compile(TERMS, TOPICS, langs) for s, (_, langs) in _SCRIPTS.items()}
_NOISE_BY_LANG = {lang: _compile(NOISE, NOISE_REASONS, g, _FALSE_FRIENDS.get(lang, "")) for lang, g in _GROUPS.items()}
_NOISE_BY_SCRIPT = {s: _compile(NOISE, NOISE_REASONS, langs) for s, (_, langs) in _SCRIPTS.items()}


def normalize_lang(lang: str | None) -> str | None:
    """'pt-BR' → 'pt', 'zh-Hant' → 'zh', 'jp' → 'ja'. None si no es uno de los 14 idiomas."""
    if not lang:
        return None
    key = lang.strip().lower().replace("_", "-").split("-")[0]
    key = _ALIASES.get(key, key)
    return key if key in TERMS else None


def _clean(title: str | None) -> str:
    """Unifica anchos (ＦＲＢ → FRB), apóstrofos y espacios, y recorta a MAX_LEN."""
    text = unicodedata.normalize("NFKC", (title or "")[:MAX_LEN * 2])
    text = text.replace("’", "'").replace("ʼ", "'").replace("`", "'")
    return " ".join(text.split())[:MAX_LEN]


def _tables(text: str, key: str | None, by_lang: dict, by_script: dict) -> list[dict[str, re.Pattern]]:
    """Las expresiones del idioma (con el inglés) o, sin idioma, las de cada escritura presente en el texto."""
    if key:
        return [by_lang[key]]
    return [by_script[s] for s, (rx, _) in _SCRIPTS.items() if rx.search(text)]


def _topics(text: str, key: str | None) -> list[str]:
    """Temas de un texto ya limpio, con el idioma ya normalizado."""
    masked = {t: rx.sub(" ", text) for t, rx in _MASKS.items()}
    found = {t for table in _tables(text, key, _BY_LANG, _BY_SCRIPT) for t, rx in table.items()
             if rx.search(masked.get(t, text))}
    found |= {IMPLIES[t] for t in found if t in IMPLIES}
    return [t for t in TOPICS if t in found]


def _noise(text: str, key: str | None) -> str | None:
    """Motivo del ruido de un texto ya limpio, sin mirar los temas."""
    tables = _tables(text, key, _NOISE_BY_LANG, _NOISE_BY_SCRIPT)
    for kind, reason in NOISE_REASONS.items():
        if any(kind in t and t[kind].search(text) for t in tables):
            return reason
    return None


def topics(title: str, lang: str | None = None) -> list[str]:
    """Temas de mercado del titular, en el orden de TOPICS.
    Con idioma busca en ese idioma y en inglés (sin los falsos amigos ingleses de ese idioma). Sin idioma, o con uno
    desconocido, busca en los idiomas que comparten escritura con el texto."""
    text = _clean(title)
    return _topics(text, normalize_lang(lang)) if text else []


def is_market_relevant(topics: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """True si hay al menos un tema de mercado (los de TOPICS); ignora cualquier otra etiqueta."""
    return any(t in _TOPIC_SET for t in (topics or ()))


def noise_reason(title: str, lang: str | None = None) -> str | None:
    """Motivo en español si el titular es ruido seguro (deportes, famosos, horóscopo, recetas, tiempo, loterías,
    cultura u ocio). Si hay cualquier tema de mercado, nunca es ruido: devuelve None."""
    text = _clean(title)
    key = normalize_lang(lang)
    if not text or _topics(text, key):
        return None
    return _noise(text, key)


def analyze(title: str, lang: str | None = None) -> dict:
    """Todo junto, listo para el `patch` de los eventos captured/filtered: {topics, relevant, noise}.
    Limpia el texto y busca los temas una sola vez."""
    text = _clean(title)
    key = normalize_lang(lang)
    found = _topics(text, key) if text else []
    noise = _noise(text, key) if text and not found else None
    return {"topics": found, "relevant": is_market_relevant(found), "noise": noise}
