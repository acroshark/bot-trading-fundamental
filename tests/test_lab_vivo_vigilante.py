"""El vigilante del laboratorio vivo (scripts/lab-vivo.ps1, docs/07, 16) y el lock del corredor (11.7): un
corredor.lock viejo cuyo PID ha reutilizado Windows para otro proceso NO es un corredor vivo. El vigilante nunca lo mata
(taskkill /T podría tumbar el bot, llama-server o explorer) y el corredor lo reemplaza. Se comprueba con -Prueba (dice
qué haría, sin arrancar ni parar nada) sobre una carpeta temporal y un proceso de mentira."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bot.lab_vivo import ajustes, recursos
from bot.lab_vivo.corredor import ORDEN, Corredor
from test_lab_vivo_comun import lab, reglas  # noqa: F401

RAIZ = Path(__file__).resolve().parents[1]
PS1 = RAIZ / "scripts" / "lab-vivo.ps1"
WIN = os.name == "nt"


def _vigilante(carpeta: Path) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1), "-Prueba",
                        "-Lab", str(carpeta)], capture_output=True, timeout=120)
    cod = "oem" if WIN else "utf-8"   # Write-Host con la salida redirigida usa la página de códigos de la consola
    return r.stdout.decode(cod, "replace") + r.stderr.decode(cod, "replace")


def _lock(carpeta: Path, pid: int, creado: int | None, ts: int | None = None) -> None:
    d = {"pid": pid, "ts": ts if ts is not None else int(time.time() * 1000)}
    if creado is not None:
        d["creado"] = creado
    (carpeta / "corredor.lock").write_text(json.dumps(d), encoding="utf-8")


def _latido(carpeta: Path, hace_s: int) -> None:
    (carpeta / "estado.json").write_text(json.dumps({"latido": int(time.time() * 1000) - hace_s * 1000}),
                                         encoding="utf-8")


@pytest.fixture
def falso_corredor():
    """Un proceso que duerme con «bot.lab_vivo correr» en su línea de órdenes (como el corredor de verdad)."""
    exe = getattr(sys, "_base_executable", None) or sys.executable   # sin el lanzador del venv: un solo proceso
    p = subprocess.Popen([exe, "-c", "import time; time.sleep(120)", "-m", "bot.lab_vivo", "correr"])
    try:
        for _ in range(50):
            if recursos.inicio_proceso(p.pid):
                break
            time.sleep(0.1)
        yield p
    finally:
        p.kill()
        p.wait(timeout=30)


def test_inicio_proceso_y_mismo_proceso():
    ini = recursos.inicio_proceso(os.getpid())
    assert ini is not None and abs(ini - int(time.time() * 1000)) < 3 * 86_400_000
    assert recursos.mismo_proceso(os.getpid(), ini)
    assert not recursos.mismo_proceso(os.getpid(), ini - 60_000)         # otra hora de creación: otro proceso
    assert recursos.mismo_proceso(os.getpid(), None, ini + 1000)        # lock antiguo, posterior a su creación
    assert not recursos.mismo_proceso(os.getpid(), None, ini - 60_000)  # un proceso creado después del lock
    assert not recursos.mismo_proceso(2 ** 30, ini)                      # no existe


def test_el_corredor_reemplaza_un_lock_viejo_con_el_pid_reutilizado(lab, reglas):  # noqa: F811
    c = Corredor(procesos=1, sin_pausas=True, lanzador=lambda tid: {}, memoria=lambda: 8000.0, cpu=lambda: 1.0,
                 openings_on=lambda d: [])
    otro = os.getppid()                                                 # un proceso vivo que no es el corredor
    ini = recursos.inicio_proceso(otro)
    ajustes.LOCK.write_text(json.dumps({"pid": otro, "ts": 0, "creado": (ini or 0) - 3_600_000}), encoding="utf-8")
    assert c.tomar_lock()                                               # PID vivo, pero creado a otra hora: viejo
    d = json.loads(ajustes.LOCK.read_text(encoding="utf-8"))
    assert d["pid"] == os.getpid() and d["creado"] == recursos.inicio_proceso(os.getpid()) and d["orden"] == ORDEN
    ajustes.LOCK.write_text(json.dumps({"pid": otro, "ts": 0, "creado": ini}), encoding="utf-8")
    assert not c.tomar_lock()                                           # el mismo proceso: vivo de verdad
    c.soltar_lock()
    assert ajustes.LOCK.exists()                                        # no es suyo: no lo suelta


@pytest.mark.skipif(not WIN, reason="el vigilante es de Windows")
def test_vigilante_no_mata_un_pid_reutilizado(tmp_path):
    car = tmp_path / "lab_vivo"
    car.mkdir()
    _latido(car, 20 * 60)                                               # latido viejo: si fuera el corredor, «colgado»
    otro = os.getppid()
    _lock(car, otro, recursos.inicio_proceso(otro))                     # el PID vive y es del momento, pero no es el corredor
    out = _vigilante(car)
    assert "colgado" not in out and "Cerraría" not in out, out
    assert "lock viejo" in out and "Arrancaría el corredor" in out, out


@pytest.mark.skipif(not WIN, reason="el vigilante es de Windows")
def test_vigilante_reconoce_al_corredor_y_su_hora(tmp_path, falso_corredor):
    car = tmp_path / "lab_vivo"
    car.mkdir()
    pid = falso_corredor.pid
    ini = recursos.inicio_proceso(pid)
    _latido(car, 20 * 60)
    _lock(car, pid, ini)
    out = _vigilante(car)
    assert "colgado" in out and f"Cerraría el corredor (colgado, PID {pid})" in out, out
    _lock(car, pid, ini - 60_000)                                       # el mismo PID con otra hora: lock viejo
    out = _vigilante(car)
    assert "Cerraría" not in out and "Arrancaría el corredor" in out, out
    _lock(car, pid, None, ts=ini - 60_000)                              # lock antiguo, anterior al proceso: viejo
    assert "Cerraría" not in _vigilante(car)
    _latido(car, 10)
    _lock(car, pid, ini)
    out = _vigilante(car)                                               # vivo y con latido: no hace nada
    assert "Cerraría" not in out and "Arrancaría" not in out, out
    (car / "parar").write_text("", encoding="utf-8")
    out = _vigilante(car)                                               # con «parar», lo cerraría si no sale solo
    assert f"Cerraría el corredor (no salió solo con el archivo parar, PID {pid})" in out, out
    assert falso_corredor.poll() is None                                # -Prueba no toca nada
