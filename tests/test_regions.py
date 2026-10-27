from datetime import date, datetime, timezone

from bot.regions import MARKETS, closes_before_open, ordered_by_close, session


def test_tokio_abierta_pausa_y_cerrada():
    t = MARKETS["tokio"]
    assert session(t, datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc))["state"] == "abierta"   # 10:00 JST
    assert session(t, datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc))["state"] == "pausa"     # 12:00 JST
    assert session(t, datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc))["state"] == "cerrada"   # 16:00 JST


def test_fin_de_semana_y_proxima_apertura():
    s = session(MARKETS["nuevayork"], datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc))   # sábado
    assert s["state"] == "cerrada" and s["next_open_utc"] == datetime(2026, 10, 26, 13, 30, tzinfo=timezone.utc)


def test_preapertura_y_cambio_de_hora_europeo():
    # Lunes 26-10-2026: Londres ya en horario de invierno, abre a las 08:00 UTC.
    s = session(MARKETS["londres"], datetime(2026, 10, 26, 7, 30, tzinfo=timezone.utc))
    assert s["state"] == "preapertura" and s["next_open_utc"] == datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


def test_asia_cierra_antes_de_que_abra_europa_pero_europa_no_antes_que_nueva_york():
    d = date(2026, 10, 23)
    assert closes_before_open(MARKETS["tokio"], MARKETS["frankfurt"], d)
    assert not closes_before_open(MARKETS["frankfurt"], MARKETS["nuevayork"], d)
    order = [m.key for m in ordered_by_close(d)]
    assert order.index("tokio") < order.index("madrid") < order.index("nuevayork")
