"""Criterios v1 del laboratorio vivo (docs/07, apartados 8 y 10), fijados el 29-10-2026 ANTES de medir nada.

Solo se cambian con una versión nueva, con el visto bueno del usuario y apuntada en «eventos». Nunca se aplican hacia
atrás para aprobar algo. El sha va en cada resultado (pruebas.criterios guarda la versión).
El 1 % diario es una META que se informa (10.3), no un criterio: no aparece aquí."""
import hashlib
import json

VERSION = 1
FECHA = "2026-10-29"

CRITERIOS = {
    "admision": {
        "max_variantes": 6,
        "max_filtros": 3,
        "min_senales_dia_dentro": 0.25,      # A9: señales por día natural dentro de muestra
        "min_texto_logica": 40,              # A10: mecanismo, predicción y falsación
        "cobertura_min": 0.5,                # A6: una fuente que cubre menos del 50 % del periodo solo como exploratoria
        "recorte_cortes": 20,                # A8: prueba del recorte
        "recorte_tramo_d": 60,
        "velocidad_max_s": 60.0,             # A8: con los 3 años
    },
    "rapida": {"min_ops_dentro": 120, "R_dentro_min": 0.0},                      # R1, R2
    "completa": {"min_ops_fuera": 100, "min_semanas_fuera": 20,                  # K1
                 "R_fuera_min": 0.0,                                             # K2
                 "mitades_positivas": True,                                      # K3
                 "wf_min_previas": 50,                                           # K4: avance trimestral
                 "freq_frecuente": 1.0, "freq_selectiva": 0.25},                 # K5
    "robustez": {"base_p_max": 0.05, "base_sorteos": 1000,                       # B1
                 "sin_mayores": 5,                                               # B2
                 "minoria_min": 0.10,                                            # B1: modo de la base emparejada
                 "fdr_q": 0.10,                                                  # B7
                 "dsr_min": 0.95,                                                # B8
                 "hac_retardo_d": 7},
    "avance": {"min_ops": 40, "min_dias_distintos": 15, "min_dias": 28,          # V2 (R63 del usuario)
               "p_max": 0.05,                                                    # V3 (Holm acumulado)
               "caida_max_pct": 6.0,                                             # V4
               "descarte_min_ops": 40, "descarte_p": 0.05, "no_azar_ops": 200,
               "exploratorias_max": 3, "explo_min_ops": 100, "explo_min_dias": 45, "explo_p_max": 0.01},
    "previas": {"n_lab_efectivo": 381, "n_resto": 1917},                          # 9.4: N_dsr = N efectivo + 381 + 1.917
}


def sha() -> str:
    return hashlib.sha1(json.dumps(CRITERIOS, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


SHA = sha()


def info() -> dict:
    return {"version": VERSION, "fecha": FECHA, "sha": SHA}


def c(etapa: str, clave: str):
    return CRITERIOS[etapa][clave]
