"""Recursos del laboratorio vivo (docs/07, 11.6), solo con la biblioteca estándar y ctypes (sin psutil).

- Prioridad BELOW_NORMAL (la heredan las tareas) y afinidad a 3 de los 6 núcleos (por defecto, CPU 3, 4 y 5): el bot y
  la IA local tienen siempre libres los demás.
- Job Object: el corredor se mete en él y sus hijos entran solos. Límites: memoria total del laboratorio
  (BOT_LAB_VIVO_MEMORIA_MB, 2.500 MB por defecto), KILL_ON_JOB_CLOSE (si el corredor muere, mueren sus tareas),
  prioridad y afinidad.
- Memoria física libre (GlobalMemoryStatusEx) y CPU de todo el PC (GetSystemTimes) para las guardas.
En Linux (Claude en la nube, solo para las pruebas): os.nice(10) y sin límites."""
import ctypes
import logging
import os
import sys

log = logging.getLogger("lab_vivo.recursos")
WIN = os.name == "nt"
BELOW_NORMAL = 0x4000
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED = 0x1000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFO = 0x0200
JOB_LIMIT_AFFINITY = 0x10
JOB_LIMIT_PRIORITY = 0x20
JOB_LIMIT_JOB_MEMORY = 0x200
JOB_LIMIT_KILL_ON_CLOSE = 0x2000
JOB_EXT_INFO = 9

if WIN:
    from ctypes import wintypes

    class _IOCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IOCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _ProcMem(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)   # instancia propia: sus argtypes no tocan a nadie más
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    # Los HANDLE como HANDLE (64 bits): sin argtypes, ctypes los pasa como int de 32 bits y el seudomanejador del
    # proceso actual (−1, 0xFFFF…FFFF) da «OverflowError: int too long to convert».
    _k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.GetPriorityClass.argtypes = (wintypes.HANDLE,)
    _k32.GetPriorityClass.restype = wintypes.DWORD
    _k32.SetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.c_size_t)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME))


def mascara(cpus) -> int:
    n = os.cpu_count() or 1
    m = 0
    for c in cpus:
        if 0 <= int(c) < n:
            m |= 1 << int(c)
    return m or ((1 << n) - 1)


_JOB = None


def proceso_bajo(cpus=(3, 4, 5), memoria_mb: int | None = None) -> dict:
    """Prioridad baja, afinidad y Job Object para este proceso (el corredor). Devuelve lo que se pudo aplicar."""
    out = {"prioridad": None, "afinidad": None, "job": False}
    if not WIN:
        try:
            os.nice(10)
            out["prioridad"] = "nice 10"
        except (AttributeError, OSError):
            pass
        return out
    global _JOB
    h = _k32.GetCurrentProcess()
    if _k32.SetPriorityClass(h, BELOW_NORMAL):
        out["prioridad"] = "por debajo de lo normal"
    mk = mascara(cpus)
    if _k32.SetProcessAffinityMask(h, ctypes.c_size_t(mk)):
        out["afinidad"] = [i for i in range(64) if mk >> i & 1]
    if memoria_mb:
        try:
            job = _k32.CreateJobObjectW(None, None)
            info = _ExtLimit()
            info.BasicLimitInformation.LimitFlags = (JOB_LIMIT_JOB_MEMORY | JOB_LIMIT_KILL_ON_CLOSE | JOB_LIMIT_PRIORITY
                                                     | JOB_LIMIT_AFFINITY)
            info.BasicLimitInformation.PriorityClass = BELOW_NORMAL
            info.BasicLimitInformation.Affinity = mk
            info.JobMemoryLimit = int(memoria_mb) * 1024 * 1024
            ok = _k32.SetInformationJobObject(job, JOB_EXT_INFO, ctypes.byref(info), ctypes.sizeof(info))
            if ok and _k32.AssignProcessToJobObject(job, h):
                _JOB = job
                out["job"] = True
            else:
                log.warning("Recursos: no pude meter el corredor en su Job Object (error %s): sin límite de memoria",
                            ctypes.get_last_error())
        except Exception as e:   # sin Job Object las guardas de memoria siguen funcionando
            log.warning("Recursos: Job Object no disponible: %s", e)
    return out


def memoria_libre_mb() -> float | None:
    if WIN:
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(st)
        if _k32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys / 1024 / 1024
        return None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def memoria_proceso_mb(pid: int) -> float | None:
    """Memoria privada de un proceso (para estado.json)."""
    if not WIN:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED | 0x0010, False, int(pid))
    if not h:
        return None
    try:
        pm = _ProcMem()
        pm.cb = ctypes.sizeof(pm)
        if _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pm), pm.cb):
            return pm.PrivateUsage / 1024 / 1024
        return None
    finally:
        _k32.CloseHandle(h)


class Cpu:
    """CPU de todo el PC entre llamadas (GetSystemTimes)."""

    def __init__(self):
        self.prev = self._tiempos()

    @staticmethod
    def _tiempos():
        if not WIN:
            try:
                with open("/proc/stat") as f:
                    x = [int(v) for v in f.readline().split()[1:]]
                return (x[3] + x[4], sum(x))
            except (OSError, ValueError, IndexError):
                return None
        idle, kern, user = (wintypes.FILETIME() for _ in range(3))
        if not _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None
        f = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime   # noqa: E731
        return (f(idle), f(kern) + f(user))   # kernel incluye idle

    def pct(self) -> float | None:
        cur = self._tiempos()
        if cur is None or self.prev is None:
            self.prev = cur
            return None
        di, dt = cur[0] - self.prev[0], cur[1] - self.prev[1]
        self.prev = cur
        return round(100.0 * (1 - di / dt), 1) if dt > 0 else None


def pid_vivo(pid: int | None) -> bool:
    if not pid:
        return False
    if WIN:
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


EPOCA_FILETIME_MS = 11_644_473_600_000   # de 1601-01-01 (FILETIME) a 1970-01-01, en ms


def inicio_proceso(pid: int | None) -> int | None:
    """Hora de creación de un proceso, en ms UTC desde 1970 (GetProcessTimes; en Linux, /proc/<pid>/stat), o None si
    no existe o no se puede leer. Con el PID y su hora de creación se sabe si es el MISMO proceso: Windows reutiliza
    los PID tras un reinicio o una caída, y un PID solo no basta (docs/07, 11.7 y 16)."""
    if not pid:
        return None
    if WIN:
        h = _k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return int(ft // 10_000 - EPOCA_FILETIME_MS)
        finally:
            _k32.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as f:
            campos = f.read().rsplit(")", 1)[1].split()
        ticks = int(campos[19])                  # starttime: el campo 22 de stat (tras el nombre, el índice 19)
        with open("/proc/stat", encoding="utf-8") as f:
            btime = next(int(x.split()[1]) for x in f if x.startswith("btime"))
        return int((btime + ticks / os.sysconf("SC_CLK_TCK")) * 1000)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


TOLERANCIA_INICIO_MS = 2000


def mismo_proceso(pid: int | None, creado: int | None = None, antes_de: int | None = None) -> bool:
    """¿El PID es el proceso que se apuntó? Vive y (con «creado») se creó entonces, ± 2 s; sin «creado» (un lock
    antiguo), se creó antes de «antes_de» (la hora del lock): un PID reutilizado es de un proceso posterior. Si vive
    pero no se puede leer su hora de creación, se da por el mismo (lo prudente: no arrancar otro ni tocarlo)."""
    if not pid_vivo(pid):
        return False
    ini = inicio_proceso(pid)
    if ini is None:
        log.warning("Recursos: el PID %s vive pero no puedo leer su hora de creación: lo doy por el mismo", pid)
        return True
    if creado:
        return abs(ini - int(creado)) <= TOLERANCIA_INICIO_MS
    if antes_de:
        return ini <= int(antes_de) + TOLERANCIA_INICIO_MS
    return True


def terminar(pid: int) -> bool:
    """Mata un proceso (TerminateProcess en Windows)."""
    if WIN:
        h = _k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(_k32.TerminateProcess(h, 1))
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(int(pid), 9)
        return True
    except OSError:
        return False


def bloquear_red() -> None:
    """La guarda de las tareas: socket.socket falla antes de importar las familias (6.1: prohibida la red)."""
    import socket

    class _SinRed(socket.socket):
        def __init__(self, *a, **k):
            raise OSError("lab_vivo: la red está prohibida en las tareas y en las familias")

    socket.socket = _SinRed
    if hasattr(socket, "create_connection"):
        def _no(*a, **k):
            raise OSError("lab_vivo: la red está prohibida en las tareas y en las familias")
        socket.create_connection = _no


def python() -> str:
    """El intérprete de las tareas: el de este proceso (con pythonw, su python.exe: las tareas escriben su registro)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe
