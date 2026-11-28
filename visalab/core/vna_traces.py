"""Пользовательские трассы ВАЦ на стороне ПК: описание (``VNATraceSpec``) и расчёт из сырых
комплексных ``SDATA`` (формат, электрическая задержка, сглаживание).

Не зависит от Qt и от слоёв ниже — относится к общему словарю :mod:`visalab.core`.
"""
from __future__ import annotations

import cmath
import math
import re
from dataclasses import dataclass
from typing import Any

from .trace import Trace

__all__ = [
    "VNA_FORMATS",
    "VNA_FORMAT_LABELS",
    "VNA_FORMAT_UNITS",
    "VNATraceSpec",
    "format_sparam_trace",
]

VNA_FORMATS: tuple[str, ...] = ("MLOG", "PHASE", "UPHASE", "GDELAY", "SWR", "LIN_MAG", "REAL", "IMAG")
VNA_FORMAT_LABELS: tuple[tuple[str, str], ...] = (
    ("MLOG", "MLOG"),
    ("PHAS", "PHASE"),
    ("UPH", "UPHASE"),
    ("GDEL", "GDELAY"),
    ("SWR", "SWR"),
    ("MLIN", "LIN_MAG"),
    ("REAL", "REAL"),
    ("IMAG", "IMAG"),
)
VNA_FORMAT_UNITS: dict[str, str] = {
    "MLOG": "dB",
    "PHASE": "deg",
    "UPHASE": "deg",
    "GDELAY": "s",
    "SWR": "",
    "LIN_MAG": "",
    "REAL": "",
    "IMAG": "",
}
_SHORT_FMT = {code: short for short, code in VNA_FORMAT_LABELS}
_SPARAM_RE = re.compile(r"^S([1-4])([1-4])$")


@dataclass(frozen=True)
class VNATraceSpec:
    """Настройки одной пользовательской трассы ВАЦ (вычисляется локально из сырых комплексных ``SDATA``)."""

    id: str
    channel: int = 1
    sparam: str = "S11"
    fmt: str = "MLOG"
    enabled: bool = True
    edel_s: float = 0.0          # электрическая задержка на ПК, с: S'(f) = S(f) * exp(j * 2*pi*f*tau)
    smooth: bool = False         # сглаживание скользящим средним на ПК
    smooth_aper: float = 1.5     # апертура сглаживания, % точек (0.5..25 %)
    label: str = ""              # своя подпись (пусто -> авто "1·S41 MLOG")

    def __post_init__(self) -> None:
        ch = max(1, min(200, int(self.channel)))
        sp = str(self.sparam or "S11").strip().upper()
        if not _SPARAM_RE.match(sp):
            sp = "S11"
        fmt = str(self.fmt or "MLOG").strip().upper()
        if fmt not in VNA_FORMATS:
            fmt = "MLOG"
        edel = float(self.edel_s)
        if not math.isfinite(edel):
            edel = 0.0
        aper = max(0.5, min(25.0, float(self.smooth_aper) if math.isfinite(float(self.smooth_aper)) else 1.5))
        object.__setattr__(self, "channel", ch)
        object.__setattr__(self, "sparam", sp)
        object.__setattr__(self, "fmt", fmt)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "edel_s", edel)
        object.__setattr__(self, "smooth", bool(self.smooth))
        object.__setattr__(self, "smooth_aper", aper)
        object.__setattr__(self, "label", str(self.label or "").strip())

    def short_format(self) -> str:
        return _SHORT_FMT.get(self.fmt, self.fmt)

    def unit(self) -> str:
        return VNA_FORMAT_UNITS.get(self.fmt, "")

    def display_label(self) -> str:
        if self.label:
            return self.label
        return f"{self.channel}·{self.sparam} {self.short_format()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "sparam": self.sparam,
            "fmt": self.fmt,
            "enabled": self.enabled,
            "edel_s": self.edel_s,
            "smooth": self.smooth,
            "smooth_aper": self.smooth_aper,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_id: str = "t1") -> "VNATraceSpec":
        return cls(
            id=str(data.get("id") or fallback_id),
            channel=int(data.get("channel", 1)),
            sparam=str(data.get("sparam", "S11")),
            fmt=str(data.get("fmt", "MLOG")),
            enabled=bool(data.get("enabled", True)),
            edel_s=float(data.get("edel_s", 0.0)),
            smooth=bool(data.get("smooth", False)),
            smooth_aper=float(data.get("smooth_aper", 1.5)),
            label=str(data.get("label", "")),
        )


def format_sparam_trace(raw: Trace, spec: VNATraceSpec) -> Trace:
    """Преобразовать сырую комплексную трассу ``SDATA`` по настройкам пользовательской трассы ``spec``."""
    xs = raw.x
    n = len(xs)
    if n == 0:
        return Trace((), (), raw.x_unit, spec.unit(), spec.display_label(), raw.timestamp,
                     {**(raw.meta or {}), "format": spec.fmt, "trace_id": spec.id}, uniform=raw.uniform)

    if raw.is_complex:
        vals = [complex(r, i) for r, i in zip(raw.y, raw.im)]
    else:
        vals = [complex(r, 0.0) for r in raw.y]

    # 1. Электрическая задержка (компенсация линейного набега фазы кабеля / оснастки)
    if spec.edel_s != 0.0 and raw.x_unit == "Hz":
        two_pi_tau = 2.0 * math.pi * spec.edel_s
        vals = [z * cmath.exp(1j * two_pi_tau * f) for z, f in zip(vals, xs)]

    # 2. Пересчёт в выбранный формат
    fmt = spec.fmt
    if fmt == "MLOG":
        out = [20.0 * math.log10(max(abs(z), 1e-15)) for z in vals]
    elif fmt == "LIN_MAG":
        out = [abs(z) for z in vals]
    elif fmt in ("PHASE", "UPHASE"):
        ph = [math.degrees(cmath.phase(z)) for z in vals]
        if fmt == "UPHASE" and len(ph) > 1:
            unwrapped = [ph[0]]
            off = 0.0
            for a, b in zip(ph, ph[1:]):
                d = b - a
                if d > 180.0:
                    off -= 360.0
                elif d < -180.0:
                    off += 360.0
                unwrapped.append(b + off)
            out = unwrapped
        else:
            out = ph
    elif fmt == "GDELAY":
        ph = [cmath.phase(z) for z in vals]
        out = []
        for i in range(n):
            j0, j1 = max(0, i - 1), min(n - 1, i + 1)
            df = xs[j1] - xs[j0]
            if df != 0.0:
                dphi = math.remainder(ph[j1] - ph[j0], 2.0 * math.pi)
                out.append(-dphi / (2.0 * math.pi * df))
            else:
                out.append(0.0)
    elif fmt == "SWR":
        out = []
        for z in vals:
            mag = min(abs(z), 1.0 - 1e-9)
            out.append((1.0 + mag) / (1.0 - mag))
    elif fmt == "REAL":
        out = [z.real for z in vals]
    else:  # IMAG
        out = [z.imag for z in vals]

    # 3. Сглаживание (скользящее среднее по апертуре % точек)
    if spec.smooth and n >= 3:
        half = max(1, round(spec.smooth_aper / 100.0 * n)) // 2 or 1
        smoothed: list[float] = []
        for i in range(n):
            lo, hi = max(0, i - half), min(n, i + half + 1)
            seg = out[lo:hi]
            smoothed.append(sum(seg) / len(seg))
        out = smoothed

    meta = {
        **(raw.meta or {}),
        "channel": spec.channel,
        "sparam": spec.sparam,
        "format": fmt,
        "edel_s": spec.edel_s,
        "smooth": spec.smooth,
        "smooth_aper": spec.smooth_aper,
        "trace_id": spec.id,
    }
    return Trace(xs, tuple(out), raw.x_unit, spec.unit(), spec.display_label(),
                 raw.timestamp, meta, uniform=raw.uniform)
