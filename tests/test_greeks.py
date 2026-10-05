import math

import numpy as np

from gex_service.greeks import MIN_T_YEARS, bs_gamma, years_from_days


def test_bs_gamma_known_value():
    # S=100, K=100, T=1y, sigma=0.2, r=0.05, q=0 -> d1=0.35, gamma = pdf(0.35)/(100*0.2) = 0.018762...
    gamma = float(bs_gamma(100.0, 100.0, 1.0, 0.2, r=0.05))
    expected = math.exp(-0.35**2 / 2) / math.sqrt(2 * math.pi) / (100 * 0.2)
    assert abs(gamma - expected) < 1e-9


def test_bs_gamma_symmetric_for_calls_and_puts_and_peaks_atm():
    strikes = np.array([80.0, 90.0, 100.0, 110.0, 120.0])
    gamma = bs_gamma(100.0, strikes, 30 / 365, 0.25, r=0.0)
    assert gamma.argmax() == 2
    assert gamma[0] < gamma[1] < gamma[2]


def test_bs_gamma_handles_invalid_inputs():
    gamma = bs_gamma(100.0, np.array([100.0, 0.0, 100.0]), 0.1, np.array([0.2, 0.2, 0.0]))
    assert gamma[0] > 0
    assert gamma[1] == 0.0
    assert gamma[2] == 0.0


def test_time_floor():
    assert float(years_from_days(0.0)) == MIN_T_YEARS
    assert float(years_from_days(365.0)) == 1.0
    # Gamma at the floor stays finite
    assert np.isfinite(bs_gamma(100.0, 100.0, 0.0, 0.2)).all()
