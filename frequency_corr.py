"""Shared DDS calibration. References, errors and input frequencies are in Hz."""

import csv
from pathlib import Path

import numpy as np


CORRECTION_PATH = Path(__file__).resolve().parent / "AD9910_GUI" / "frequency_correction.txt"


def freq_corr(f_input, refer, error):
    """Apply the original linear fit and round to Hz; preserve zero Hz safely."""
    refer = np.asarray(refer, dtype=float)
    error = np.asarray(error, dtype=float)
    values = np.asarray(f_input, dtype=float)
    if (refer.ndim != 1 or error.shape != refer.shape or refer.size < 2
            or not np.all(np.isfinite(refer)) or not np.all(np.isfinite(error))
            or np.any(refer < 0) or np.any(np.diff(refer) <= 0)):
        raise ValueError("Calibration requires matching finite errors and increasing references.")
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError("Frequency must be finite and nonnegative.")
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            k, b = np.polyfit(refer, error, 1)
            denominator = k * values + b + values
            if not np.all(np.isfinite(denominator)) or np.any((values != 0) & (denominator <= 0)):
                raise ValueError("Frequency correction denominator must be positive and finite.")
            corrected = np.zeros_like(values)
            np.divide(values ** 2, denominator, out=corrected, where=values != 0)
            return np.round(corrected)
    except (FloatingPointError, np.linalg.LinAlgError) as exc:
        raise ValueError("Frequency correction could not be calculated safely.") from exc


def load_correction(path=CORRECTION_PATH):
    """Read reference row 0 and error rows keyed by 1-based DDS ID."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    if not rows or rows[0][0].strip() != "0":
        raise ValueError("Calibration first row must start with reference ID 0.")
    refer = np.asarray([float(value) for value in rows[0][1:]])
    errors = {}
    for row in rows[1:]:
        channel = int(row[0])
        if not 1 <= channel <= 20 or channel in errors:
            raise ValueError("Calibration DDS IDs must be unique and within 1..20.")
        error = np.asarray([float(value) for value in row[1:]])
        freq_corr(0, refer, error)
        errors[channel] = error
    if not errors:
        raise ValueError("Calibration contains no DDS error rows.")
    return refer, errors


def corrected_frequency(f_input, channel, maximum, path=CORRECTION_PATH):
    """Return a safe serial frequency; never clamp or silently skip calibration."""
    if not 0 <= f_input <= maximum:
        raise ValueError(f"Input frequency must be 0..{maximum} Hz.")
    refer, errors = load_correction(path)
    if channel not in errors:
        raise ValueError(f"Missing calibration for DDS {channel}.")
    result = float(freq_corr(f_input, refer, errors[channel]))
    if not np.isfinite(result) or not 0 <= result <= maximum:
        raise ValueError(f"Corrected frequency {result:g} Hz is outside 0..{maximum} Hz.")
    return int(result)
