"""
ECAM usage statistics — native ``0xA2`` pages and cloud counters.

Native path (``statistics_source: "native"`` maps): the machine exposes a
sparse ``<id:2B> <u32>`` table through ``0xA2`` paged reads on
``data_request`` / ``data_response``. Paging rules (live-verified):
start at id 0 with ``count = 10``; a page shorter than ``count`` is the
end of the table; a timeout is **never** end-of-table — retry the same
``start_id`` with ``count - 1`` (floor 1).

Cloud path (``statistics_source: "cloud_counters"`` maps — striker
machines without LAN): logical counters resolve through
:data:`CLOUD_COUNTER_CANDIDATES` name lists against the Ayla datapoint
cache, parsed per the reference rules: plain ints pass through, JSON
per-recipe objects sum to a total and keep their breakdown, anything
unparseable becomes ``None`` (unknown — never invented).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from cremalink.ecam.answers import decode_a2_page

__all__ = [
    "CLOUD_COUNTER_CANDIDATES",
    "MEASUREMENT_WATER_LITERS",
    "STAT_LABELS",
    "StatisticEntry",
    "StatisticsReport",
    "counter_breakdown",
    "decode_a2_page",
    "interpret",
    "parse_counter_value",
    "parse_water_volume_liters",
    "resolve_cloud_counters",
]


#: Official labels for the A2 sparse ids, verified against the machine's
#: own statistics display (``z7.w$a`` categories / display-match corpus).
#: ``a2_id -> (label, unit)``; unit ``"l"`` means ``value = raw / 2000``
#: (half-millilitres → litres per ``z7.u.a``).
STAT_LABELS: dict[int, tuple[str, str]] = {
    105: ("descales", "count"),
    106: ("water_total", "l"),
    108: ("filters", "count"),
    115: ("milk_cleans", "count"),
    3000: ("total_black", "count"),
    3001: ("total_milk_coffee", "count"),
    3002: ("total_other", "count"),
    3003: ("total_milk_only", "count"),
    3004: ("total_espressos", "count"),
    3005: ("espresso", "count"),
    3006: ("coffee", "count"),
    3007: ("long_coffee", "count"),
    3008: ("doppio", "count"),
    3009: ("americano", "count"),
    3010: ("cappuccino", "count"),
    3011: ("latte_macchiato", "count"),
    3012: ("caffe_latte", "count"),
    3013: ("flat_white", "count"),
    3014: ("espresso_macchiato", "count"),
    3015: ("hot_milk", "count"),
    3016: ("cappuccino_doppio", "count"),
    3017: ("cappuccino_mix", "count"),
    3018: ("hot_water", "count"),
    3019: ("tea", "count"),
    3020: ("coffee_pot", "count"),
    3021: ("choco", "count"),
    3025: ("tea_iced", "count"),
    43010: ("total_beverages", "count"),
}

#: A2 ids reported in litres (raw half-millilitres → ``raw / 2000``).
_WATER_IDS = frozenset(
    stat_id for stat_id, (_, unit) in STAT_LABELS.items() if unit == "l"
)

MEASUREMENT_WATER_LITERS = "water_liters"


@dataclass(frozen=True)
class StatisticEntry:
    """One interpreted A2 statistic."""

    a2_id: int
    raw: int
    label: str | None
    value: int | float
    unit: str | None


@dataclass
class StatisticsReport:
    """Result of one statistics fetch."""

    source: str  # "native" | "cloud_counters"
    entries: dict[int, StatisticEntry] = field(default_factory=dict)
    complete: bool = False
    fetched_at: float = field(default_factory=time.time)
    #: logical cloud counters when source == "cloud_counters"
    cloud_counters: dict[str, int | float | None] = field(default_factory=dict)
    #: per-counter JSON breakdowns (Eletta aggregated counters)
    breakdowns: dict[str, dict] = field(default_factory=dict)


def interpret(
    entries: Iterable[tuple[int, int]],
    *,
    source: str = "native",
    complete: bool = False,
    fetched_at: float | None = None,
) -> StatisticsReport:
    """Fold ``(a2_id, raw)`` pairs into a labelled :class:`StatisticsReport`.

    Unlabeled ids pass through with ``label=None`` — they are real
    machine data, just not display-named.
    """
    report = StatisticsReport(
        source=source,
        complete=complete,
        fetched_at=time.time() if fetched_at is None else fetched_at,
    )
    for a2_id, raw in entries:
        label, unit = STAT_LABELS.get(a2_id, (None, "count"))
        value: int | float = round(raw / 2000, 3) if a2_id in _WATER_IDS else raw
        report.entries[a2_id] = StatisticEntry(
            a2_id=a2_id, raw=raw, label=label, value=value, unit=unit
        )
    return report


# --- Cloud counters path -------------------------------------------------

#: Logical counter → candidate Ayla datapoint names; the first name
#: present on the device wins (Soul ``d700_*`` vs Eletta ``d701_*`` are
#: not the same counters — see the reference integration's const.py).
CLOUD_COUNTER_CANDIDATES: dict[str, list[str]] = {
    "total_beverages": ["d700_tot_bev_b", "d701_tot_bev_b"],
    "total_other_beverages": ["d702_tot_bev_other"],
    "total_espresso": ["d704_tot_bev_espressi"],
    "total_milk_drinks": ["d701_tot_bev_bw"],
    "total_milk_only": ["d703_tot_bev_w"],
    "espresso": ["d705_tot_id1_espr"],
    "coffee": ["d706_tot_id2_coffee"],
    "long_coffee": ["d707_tot_id3_long"],
    "doppio": ["d708_tot_id5_doppio_p"],
    "americano": ["d709_id6_americano"],
    "cappuccino": ["d710_tot_id7_capp"],
    "latte_macchiato": ["d711_id8_lattmacc"],
    "caffe_latte": ["d712_id9_cafflatt"],
    "flat_white": ["d713_id10_flatwhite"],
    "espresso_macchiato": ["d714_id11_esprmacc"],
    "hot_milk": ["d715_id12_hotmilk"],
    "cappuccino_doppio": ["d716_id13_cappdoppio_p"],
    "cappuccino_reverse": ["d717_id15_caprev"],
    "hot_water": ["d718_id16_hotwater"],
    "tea": ["d719_id22_tea"],
    "coffee_pot": ["d720_tot_id23_coffee_pot"],
    "cortado": ["d727_id24_cortado"],
    "long_black": ["d728_id25_long_black"],
    "mug_to_go": ["d729_id26_travel_mug"],
    "brew_over_ice": ["d730_tot_id27_brew_over_ice"],
    "grounds_counter": ["d551_cnt_coffee_fondi"],
    "descales": ["d552_cnt_calc_tot"],
    "water_total_quantity": ["d553_water_tot_qty"],
    "filters": ["d554_cnt_filter_tot"],
    "water_filter_quantity": ["d555_water_filter_qty"],
    "descale_status": ["d825_descale_status"],
    "water_hardness": ["d556_water_hardness"],
}

#: Logical counters that carry millilitres (converted to litres).
_COUNTER_MEASUREMENTS: dict[str, str] = {
    "water_total_quantity": MEASUREMENT_WATER_LITERS,
    "water_filter_quantity": MEASUREMENT_WATER_LITERS,
}


def _looks_like_json_object(val_str: str) -> bool:
    return val_str.startswith("{") and val_str.endswith("}")


def parse_counter_value(val: Any) -> int | None:
    """Return the integer state for a counter value, or ``None``.

    Plain integers pass through; a JSON object is summed over its
    integer sub-values; anything else yields ``None`` (unknown).
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    val_str = str(val).strip()
    if _looks_like_json_object(val_str):
        try:
            data = json.loads(val_str)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        total = 0
        for sub in data.values():
            try:
                total += int(sub)
            except (ValueError, TypeError):
                pass
        return total
    try:
        return int(val_str)
    except (TypeError, ValueError):
        return None


def counter_breakdown(val: Any) -> dict | None:
    """Return the per-recipe JSON breakdown of a counter, else ``None``."""
    if not val or isinstance(val, (int, bool)):
        return None
    val_str = str(val).strip()
    if _looks_like_json_object(val_str):
        try:
            data = json.loads(val_str)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def parse_water_volume_liters(val: Any) -> float | None:
    """Convert a millilitre counter value to litres."""
    milliliters = parse_counter_value(val)
    if milliliters is None:
        return None
    return round(milliliters / 1000, 3)


def resolve_cloud_counters(properties: dict[str, Any]) -> dict[str, int | float | None]:
    """Resolve logical counters against the property/dp cache.

    ``properties`` maps datapoint name → value (or a ``{"value": …}``
    wrapper). First present candidate wins; absent candidates produce no
    key — an unresolved counter is "absent", not ``None`` (FR-016d).
    """
    out: dict[str, int | float | None] = {}
    for logical, candidates in CLOUD_COUNTER_CANDIDATES.items():
        for name in candidates:
            if name not in properties:
                continue
            raw = properties[name]
            if isinstance(raw, dict):
                raw = raw.get("value")
            if _COUNTER_MEASUREMENTS.get(logical) == MEASUREMENT_WATER_LITERS:
                out[logical] = parse_water_volume_liters(raw)
            else:
                out[logical] = parse_counter_value(raw)
            break
    return out


def resolve_cloud_breakdowns(properties: dict[str, Any]) -> dict[str, dict]:
    """Per-counter JSON breakdowns, keyed by logical counter."""
    out: dict[str, dict] = {}
    for logical, candidates in CLOUD_COUNTER_CANDIDATES.items():
        for name in candidates:
            if name not in properties:
                continue
            raw = properties[name]
            if isinstance(raw, dict):
                raw = raw.get("value")
            breakdown = counter_breakdown(raw)
            if breakdown is not None:
                out[logical] = breakdown
            break
    return out
