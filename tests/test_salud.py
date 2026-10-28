"""Salud del proceso y salud.csv (hallazgo n.º 40), sin red y con un reloj de mentira."""
import asyncio
import csv
import ctypes

import pytest

from bot import salud
from bot.salud import Salud, apuntar, fila, proceso

HEALTH = {"en_marcha_s": 3600, "local_ai": {"queue": 12, "done": 300, "dropped": 4}, "db": {"mb": 812.4, "wal_mb": 3.1},
          "feeds": {"totals": {"feeds": 5714, "ok": 5600, "failing": 114}}, "clients": 1,
          "reloj": {"desfase_ms": 212}, "proceso": {"mem_mb": 546.2, "privada_mb": 706.0, "handles": 1793, "hilos": 9}}


def test_proceso_lee_memoria_y_handles():
    p = proceso()
    assert set(p) == {"mem_mb", "privada_mb", "handles", "hilos"} and p["hilos"] >= 1
    assert p["mem_mb"] is None or p["mem_mb"] > 0


def test_linux_desde_proc(tmp_path):
    (tmp_path / "fd").mkdir()
    for i in range(3):
        (tmp_path / "fd" / str(i)).write_text("")
    (tmp_path / "status").write_text("Name:\tpython\nVmRSS:\t  546000 kB\nRssAnon:\t  400000 kB\n")
    assert salud._linux(tmp_path) == {"mem_mb": 559.1, "privada_mb": 409.6, "handles": 3}
    assert salud._linux(tmp_path / "no-existe") is None


def test_windows_con_una_api_de_mentira():
    class K32:
        def GetCurrentProcess(self):
            return -1

        def GetProcessHandleCount(self, h, ref):
            ref._obj.value = 1793
            return 1

    class Psapi:
        def GetProcessMemoryInfo(self, h, ref, cb):
            c = ref._obj
            assert cb == ctypes.sizeof(c) == c.cb == 80   # PROCESS_MEMORY_COUNTERS_EX en 64 bits
            c.WorkingSetSize, c.PrivateUsage = 546_200_000, 706_000_000
            return 1

    assert salud._windows(K32(), Psapi()) == {"mem_mb": 546.2, "privada_mb": 706.0, "handles": 1793}


def test_fila_y_csv(tmp_path):
    row = fila(HEALTH, {"ms": 1.5, "max_ms": 40.0}, ahora=1_800_000_000)
    assert row["hora_madrid"] == "2027-01-15 09:00:00" and row["mem_mb"] == 546.2 and row["handles"] == 1793
    assert (row["ia_cola"], row["ia_sin_triar"], row["db_mb"], row["wal_mb"]) == (12, 4, 812.4, 3.1)
    assert (row["feeds_ok"], row["feeds_total"], row["reloj_desfase_ms"], row["retraso_bucle_max_ms"]) == (5600, 5714, 212, 40.0)
    path = tmp_path / "logs" / "salud.csv"
    apuntar(path, row)
    apuntar(path, fila({}, {}, ahora=1_800_000_300))   # sin datos: celdas vacías, no «None»
    with path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert rows[0] == list(salud.COLUMNAS) and len(rows) == 3
    assert rows[1][2] == "546,2" and rows[1][4] == "1793" and set(rows[2][1:]) == {""}


async def test_bucle_mide_el_retraso_y_apunta_cada_5_min(tmp_path):
    t = [0.0]

    async def sleep(s):
        t[0] += s + 0.25   # cada espera de 1 s se pasa 250 ms: algo bloquea el bucle
        if t[0] > salud.CADA_S * 2 + 5:
            raise asyncio.CancelledError

    s = Salud(lambda: HEALTH, tmp_path / "salud.csv", clock=lambda: t[0], sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert s.retraso == {"ms": 250.0, "max_ms": 250.0}
    with (tmp_path / "salud.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert len(rows) == 2 and rows[0]["retraso_bucle_ms"] == "250,0" and rows[0]["handles"] == "1793"
