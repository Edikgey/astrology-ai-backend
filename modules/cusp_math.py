"""Cusp interval membership shared with the existing natal calculator.

Callers validate cusp availability; this preserves the natal boundary convention.
"""


def house_for_longitude(longitude, cusps):
    absolute_degree = longitude % 360
    for i, cusp in enumerate(cusps):
        if abs((absolute_degree - cusp + 180) % 360 - 180) < 1e-10:
            return i + 1
    for i in range(12):
        start, end = cusps[i], cusps[(i + 1) % 12]
        if start > end:
            if start <= absolute_degree or absolute_degree < end:
                return i + 1
        elif start <= absolute_degree < end:
            return i + 1
    return 12  # Existing natal fallback; synastry rejects invalid cusps first.
