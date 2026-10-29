"""Firma de las peticiones privadas de OKX (REST y login del WebSocket) y guarda de los secretos (docs/06, apartado 2).

- La firma es Base64(HMAC-SHA256(SecretKey, hora + MÉTODO + ruta?consulta + cuerpo)), con la hora en ISO 8601 con
  milisegundos («2020-12-08T09:08:57.715Z»). El login del WebSocket firma «segundos + GET + /users/self/verify».
- Claves guarda la clave, el secreto y la frase de paso sin enseñarlos nunca: su repr y su str salen enmascarados, no se
  pueden pasar a JSON ni copiar con pickle, y cada valor queda apuntado para que enmascarar() lo tape en cualquier texto.
- enmascarar() tapa los secretos conocidos y todo lo que se les parece (cabeceras OK-ACCESS-*, variables OKX_API_*,
  campos apiKey/secretKey/passphrase/sign, UUID, hexadecimales largos y firmas en Base64). FiltroSecretos hace lo mismo
  con cada línea del registro (logging), también con el texto de las excepciones.
Este módulo no hace peticiones: solo calcula firmas y limpia textos."""
import base64
import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone

MASCARA = "***"
NOMBRES = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")
_conocidos: set[str] = set()   # valores de los secretos leídos en este proceso (solo en memoria; nunca se escriben)

# Lo que se parece a un secreto aunque no sea uno de los conocidos: un campo con nombre de secreto (entre comillas, el
# valor entero; sin comillas, hasta el primer separador), UUID, hexadecimales largos y firmas en Base64.
_NOMBRE = (r"(OK-ACCESS-(?:KEY|SIGN|PASSPHRASE)|OKX_API_(?:KEY|SECRET|PASSPHRASE)|api[_-]?key|secret[_-]?key|secreto|"
           r"secret|passphrase|frase[_-]?de[_-]?paso|\bsign\b)")
_CAMPO_COMILLAS = re.compile(r"(?i)" + _NOMBRE + r"(\\?[\"']?\s*[:=]\s*)(\\?[\"'])(.*?)(\3)")
_CAMPO = re.compile(r"(?i)" + _NOMBRE + r"(\\?[\"']?\s*[:=]\s*\\?[\"']?)([^\s\"',;}\]\\]+)")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_B64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])")


def hora_iso(ms: int) -> str:
    """Hora de OK-ACCESS-TIMESTAMP: UTC en ISO 8601 con milisegundos y «Z»."""
    ms = int(ms)
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def texto_a_firmar(hora: str, metodo: str, ruta: str, cuerpo: str = "") -> str:
    """hora + MÉTODO + ruta con su consulta + cuerpo (vacío en los GET), exactamente como se envían."""
    return f"{hora}{metodo.upper()}{ruta}{cuerpo or ''}"


def firmar(secreto: str, mensaje: str) -> str:
    """Base64(HMAC-SHA256(secreto, mensaje))."""
    mac = hmac.new(secreto.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def firma_login_ws(secreto: str, segundos: int | str) -> str:
    """Firma del login del WebSocket privado: segundos Unix + GET + /users/self/verify."""
    return firmar(secreto, f"{segundos}GET/users/self/verify")


class Claves:
    """Clave, secreto y frase de paso de OKX. Nunca se enseñan: repr y str enmascarados, sin JSON ni pickle."""
    __slots__ = ("_clave", "_secreto", "_frase")

    def __init__(self, clave: str | None, secreto: str | None, frase: str | None):
        self._clave = (clave or "").strip()
        self._secreto = (secreto or "").strip()
        self._frase = (frase or "").strip()
        for v in (self._clave, self._secreto, self._frase):
            if len(v) >= 4:
                _conocidos.add(v)

    def faltan(self) -> list[str]:
        """Nombres (nunca valores) de las que faltan o están vacías."""
        return [n for n, v in zip(NOMBRES, (self._clave, self._secreto, self._frase)) if not v]

    def completas(self) -> bool:
        return not self.faltan()

    def huella(self) -> str:
        """Resumen corto para saber si el archivo ha cambiado, sin poder sacar de él ningún valor."""
        h = hashlib.sha256("\x00".join((self._clave, self._secreto, self._frase)).encode("utf-8")).hexdigest()
        return h[:8]

    def cabeceras(self, hora: str, metodo: str, ruta: str, cuerpo: str = "") -> dict:
        """Cabeceras de una petición privada (docs/06, apartado 2). No se registran nunca."""
        return {"OK-ACCESS-KEY": self._clave,
                "OK-ACCESS-SIGN": firmar(self._secreto, texto_a_firmar(hora, metodo, ruta, cuerpo)),
                "OK-ACCESS-TIMESTAMP": hora,
                "OK-ACCESS-PASSPHRASE": self._frase}

    def login_ws(self, segundos: int) -> dict:
        """Argumentos del login del WebSocket privado (para cuando se use; hoy el modo real va por REST)."""
        return {"apiKey": self._clave, "passphrase": self._frase, "timestamp": str(segundos),
                "sign": firma_login_ws(self._secreto, segundos)}

    def __repr__(self) -> str:
        estado = "completas" if self.completas() else "faltan " + ", ".join(self.faltan())
        return f"Claves({estado}, valores {MASCARA})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("Las claves de OKX no se copian ni se guardan")

    def __eq__(self, other) -> bool:
        return isinstance(other, Claves) and self.huella() == other.huella()

    def __hash__(self) -> int:
        return hash(self.huella())


def enmascarar(texto, extra=()) -> str:
    """Tapa los secretos conocidos y lo que se les parece. Acepta cualquier cosa (la pasa a texto)."""
    s = texto if isinstance(texto, str) else str(texto)
    for v in sorted({*_conocidos, *(x for x in extra if x and len(x) >= 4)}, key=len, reverse=True):
        if v in s:
            s = s.replace(v, MASCARA)
    s = _CAMPO_COMILLAS.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + MASCARA + m.group(5), s)
    s = _CAMPO.sub(lambda m: m.group(1) + m.group(2) + MASCARA, s)
    s = _UUID.sub(MASCARA, s)
    s = _HEX.sub(MASCARA, s)
    return _B64.sub(MASCARA, s)


def limpiar(obj):
    """Copia de un mensaje (dict, lista o valor) con todos sus textos enmascarados: lo que va al bus, a la base y a la
    web pasa por aquí."""
    if isinstance(obj, str):
        return enmascarar(obj)
    if isinstance(obj, dict):
        return {(enmascarar(k) if isinstance(k, str) else k): limpiar(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [limpiar(v) for v in obj]
    if isinstance(obj, Claves):
        return repr(obj)
    return obj


class FiltroSecretos(logging.Filter):
    """Filtro de registro: el mensaje ya formateado y el texto de la excepción salen enmascarados."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = enmascarar(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = enmascarar(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            if record.stack_info:
                record.stack_info = enmascarar(record.stack_info)
        except Exception:   # un registro que no se puede formatear no sale con sus argumentos crudos
            record.msg, record.args, record.exc_info = "registro del modo real no mostrable", (), None
        return True


def proteger_registro(logger: logging.Logger) -> logging.Logger:
    """Pone FiltroSecretos en el logger (una sola vez)."""
    if not any(isinstance(f, FiltroSecretos) for f in logger.filters):
        logger.addFilter(FiltroSecretos())
    return logger
