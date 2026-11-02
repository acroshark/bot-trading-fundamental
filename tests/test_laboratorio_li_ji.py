"""Li y Ji (laboratorio.effective_trials) con series repetidas: f(λ) = [λ ≥ 1] + (λ − ⌊λ⌋) salta en cada entero ≥ 2,
y con k series iguales el autovalor es justo k. Sin el redondeo, la máquina lo dejaba en k − ε según la CPU (en Linux
con numpy 2.3.4, 2,9999999999999996 con tres copias: N = 2 en vez de 1). Con series reales no cambia nada."""
import numpy as np
import pytest

from bot import laboratorio as L


def _sin_redondeo(X) -> float:
    X = np.asarray(X, float)
    Z = (X - X.mean(1, keepdims=True)) / X.std(1, keepdims=True)
    lam = np.clip(np.linalg.eigvalsh((Z.T @ Z) / Z.shape[1]), 0, None)
    return float(max(1.0, ((lam >= 1) + (lam - np.floor(lam))).sum()))


@pytest.mark.parametrize("k", [2, 3, 5, 8])
def test_k_copias_son_una_prueba(k):
    a = np.random.default_rng(k).normal(0, 1, 400)
    X = np.array([a * (1 + j) + j for j in range(k)])        # la misma serie, escalada y desplazada: correlación 1
    assert L.effective_trials(X) == pytest.approx(1.0, abs=1e-6)


def test_dos_copias_y_una_independiente():
    rng = np.random.default_rng(9)
    a, b = rng.normal(0, 1, 400), rng.normal(0, 1, 400)
    assert L.effective_trials(np.array([a, a * 2, b])) == pytest.approx(2.0, abs=0.1)


def test_con_series_reales_da_lo_mismo_que_sin_redondeo():
    rng = np.random.default_rng(4)
    base = rng.normal(0, 1, (1, 300))
    for X in (rng.normal(0, 1, (20, 300)), np.repeat(base, 10, 0) + rng.normal(0, 0.3, (10, 300))):
        assert L.effective_trials(X) == pytest.approx(_sin_redondeo(X), abs=1e-7)
