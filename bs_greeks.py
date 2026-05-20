"""
Black-Scholes Greeks for European options.

Used by YahooQuotesProvider — Yahoo Finance returns implied volatility but
not Greeks, so we compute delta / gamma / theta / vega locally from IV +
spot + strike + time-to-expiry.

This is a small, dependency-free implementation. For ATM and near-the-money
options it agrees with broker-supplied Greeks within ~2%. Accuracy drops
on illiquid wings where the input IV is itself unreliable.

Conventions match what IBKR and TastyTrade return so the UI doesn't have
to special-case the source:
    delta     — dimensionless, signed (calls > 0, puts < 0)
    gamma     — per $1 of underlying
    theta     — per CALENDAR DAY (negative for long options)
    vega      — per 1.00 (100-point) change in volatility; multiply by
                0.01 to get the "per 1 vol-point" number some platforms show
"""
from __future__ import annotations

import math


def _norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def compute_greeks(
    spot: float,
    strike: float,
    dte_years: float,
    iv: float,
    is_call: bool,
    r: float = 0.05,
    q: float = 0.0,
) -> dict:
    """
    Black-Scholes-Merton Greeks for a single European option.

    Parameters
    ----------
    spot
        Current price of the underlying.
    strike
        Strike price.
    dte_years
        Time to expiry in years (calendar days / 365).
    iv
        Implied volatility, as a decimal (e.g. 0.25 for 25%).
    is_call
        True for calls, False for puts.
    r
        Risk-free rate (annualized, decimal). Default 5% — close enough
        for short-dated options; the resulting Greeks are insensitive to
        small mis-specifications of r.
    q
        Continuous dividend yield (annualized, decimal). Default 0.
        For most equity-option positions on liquid US names, the error
        from leaving this at 0 is well under 1%.

    Returns
    -------
    dict
        ``{"delta": …, "gamma": …, "theta": …, "vega": …}``. Any input
        that makes the math ill-defined (zero or negative DTE / IV /
        spot / strike) results in all four values being ``None``.
    """
    blank = {"delta": None, "gamma": None, "theta": None, "vega": None}

    try:
        spot       = float(spot)
        strike     = float(strike)
        dte_years  = float(dte_years)
        iv         = float(iv)
    except (TypeError, ValueError):
        return blank

    if spot <= 0 or strike <= 0 or dte_years <= 0 or iv <= 0:
        return blank

    sigma_sqrt_T = iv * math.sqrt(dte_years)
    if sigma_sqrt_T == 0:
        return blank

    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * dte_years) / sigma_sqrt_T
    d2 = d1 - sigma_sqrt_T

    pdf_d1 = _norm_pdf(d1)
    disc_r = math.exp(-r * dte_years)
    disc_q = math.exp(-q * dte_years)

    if is_call:
        delta = disc_q * _norm_cdf(d1)
        theta_annual = (
            -(spot * pdf_d1 * iv * disc_q) / (2.0 * math.sqrt(dte_years))
            - r * strike * disc_r * _norm_cdf(d2)
            + q * spot * disc_q * _norm_cdf(d1)
        )
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta_annual = (
            -(spot * pdf_d1 * iv * disc_q) / (2.0 * math.sqrt(dte_years))
            + r * strike * disc_r * _norm_cdf(-d2)
            - q * spot * disc_q * _norm_cdf(-d1)
        )

    gamma = (disc_q * pdf_d1) / (spot * sigma_sqrt_T)
    # Vega returned per 1.00 (100-point) change in vol — broker convention.
    vega  = spot * disc_q * pdf_d1 * math.sqrt(dte_years)
    # Theta returned per calendar day — broker convention.
    theta = theta_annual / 365.0

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega":  vega,
    }
