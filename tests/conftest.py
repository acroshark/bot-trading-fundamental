"""Las carpetas temporales de las pruebas (tmp_path, tempfile) van a M:, nunca a C:.
En Linux (Claude en la nube) no hay disco M:: van al temporal del sistema."""
import os
import tempfile
from pathlib import Path

import pytest

if os.name == "nt":
    TMP = Path(os.environ.get("BOT_TEST_TMP", r"M:\BotFundamental-runtime\tmp"))
else:   # las pruebas que leen BOT_TEST_TMP (test_backtest, test_predict) usan la misma carpeta
    os.environ.setdefault("BOT_TEST_TMP", str(Path(tempfile.gettempdir()) / "bot-pruebas"))
    TMP = Path(os.environ["BOT_TEST_TMP"])
TMP.mkdir(parents=True, exist_ok=True)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(TMP)
for var in ("TMP", "TEMP", "TMPDIR"):
    os.environ[var] = str(TMP)
tempfile.tempdir = str(TMP)
# Las pruebas antiguas comprueban el comportamiento sin las reglas del usuario (break-even, dinámico, stops cortos);
# las reglas tienen sus propias pruebas, que las activan con monkeypatch.
os.environ.setdefault("BOT_USER_RULES", "0")


@pytest.fixture(autouse=True)
def _comisiones_por_defecto():
    """El perfil de comisiones es estado del proceso (bot/comisiones.py): cada prueba empieza y acaba con el de por
    defecto (OKX EU con cuenta X-Perps) y sin comisión de la cuenta real."""
    from bot import comisiones
    comisiones.reiniciar()
    yield
    comisiones.reiniciar()
