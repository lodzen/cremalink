"""Recipe-parameter metadata shared by the brew-with-overrides paths.

The machine models every tunable recipe parameter as a numeric value
bounded by a per-recipe ``<param><min><def><max>`` triple (the ``0xB0``
descriptor; protocol.md §14.3). This module is the single source of truth
for which of those parameters are user-adjustable controls and how raw
wire units map to display units — the scaling mirrors the app's
``RecipeData`` getters (``m()``/siblings; research.md R11): coffee/water
raw = **ml directly** (the ``0xB0`` ``defValue`` is returned unscaled
when the ingredients list exists — the ×0.5 ``halfMlToMl`` path is only
the legacy no-ingredients fallback), milk raw = tenths of a second.

Pure data/helpers: no I/O, no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Bean-system (BeanAdapt) beverage-id range — ``m6.a`` enum, matches
#: ``catalog.BEAN_SYSTEM_IDS`` (kept here so builder/device/catalog can
#: share one definition without circular imports).
BEAN_SYSTEM_ID_MIN = 0xC8  # 200
BEAN_SYSTEM_ID_MAX = 0xCD  # 205


def is_bean_beverage(beverage_id: int) -> bool:
    """True for BeanAdapt beverages — they brew with ``action | 0x80``."""
    return BEAN_SYSTEM_ID_MIN <= beverage_id <= BEAN_SYSTEM_ID_MAX


@dataclass(frozen=True)
class ParamMeta:
    """Display metadata for one user-adjustable recipe parameter id.

    ``to_display`` converts a raw wire value to display units;
    ``step_raw`` is the raw-step equivalent of one display step
    (coffee/water: 1 raw = 1 ml; milk: 10 raw = 1 s).
    """

    param_id: int
    key: str
    unit: str | None
    to_display: float
    step_raw: int
    adjustable: bool = True


#: User-adjustable recipe parameters — the controls the app's brew
#: sheet actually offers (``MainBeveragesFragment.E1()`` → ``V0``/``U0``/
#: ``Y0``/``c1``/``b1``; research.md R12). On a product-coded machine
#: (``EcamMachine.N()`` — every Wi-Fi family incl. the Soul) those are
#: coffee amount, aroma, milk amount and hot-water amount plus the
#: tea-temperature picker, which exists only for recipes carrying the
#: ``THE_TEMP(13)`` ingredient (Soul: tea only). BeanAdapt (id ``0xC8``)
#: gets the coffee-amount control alone.
#:
#: Not controls on these machines, so no entity here:
#: ``TEMP(0)`` — the ``b1`` temp picker looks for id 13 on ``N()``
#: machines (and for id 0 only on machines without a product code);
#: ``BLEND(4)`` and ``MILK_FROTH(11)`` — only in the ``!N()`` branch
#: (and blend additionally needs ``nGrinders > 1``; Soul: 0);
#: ``INVERSION(12)`` — a stored recipe flag selecting
#: ``PREPARE_BEVERAGE_INVERSION`` (``RecipeData.T()`` in
#: ``n0()``/brew dispatch), set at recipe creation, never a slider;
#: tag ``27`` is **INDEX_LENGTH** (management — rides the wire at its
#: stored value via ``E0``; the earlier "temperature" guess came from
#: ``catalog.TAG_NAMES``, superseded — R11).
PARAMS: dict[int, ParamMeta] = {
    1: ParamMeta(1, "coffee", "ml", 1.0, 1),
    2: ParamMeta(2, "taste", None, 1.0, 1),
    9: ParamMeta(9, "milk", "s", 0.1, 10),
    13: ParamMeta(13, "the_temp", None, 1.0, 1),
    15: ParamMeta(15, "water", "ml", 1.0, 1),
}

#: Recipe-management tags that ride the wire at stored values but are
#: never user controls. ``24``/``25``/``30`` fail the ``E0`` predicate
#: anyway, ``8`` is dropped by the app's ``O()`` builder itself, and
#: ``27``/``28`` are the INDEX_LENGTH/ACCESSORIO descriptor fields.
MANAGEMENT_PARAMS = frozenset({27, 28})

#: ``0xB0`` tag carrying the coffee amount (``m6.i.COFFEE``).
TAG_COFFEE = 1

#: ``0xB0`` tag carrying the intensity/aroma level.
TAG_TASTE = 2

#: ``0xB0`` tag carrying the milk amount (``m6.i.MILK``).
TAG_MILK = 9

#: ``0xB0`` tag carrying the hot-water amount (``m6.i.HOT_WATER``).
TAG_WATER = 15


def to_display(param_id: int, raw: int) -> float:
    """Raw wire value → display units (identity for unknown ids)."""
    meta = PARAMS.get(param_id)
    return raw * meta.to_display if meta else float(raw)


def to_raw(param_id: int, display: float) -> int:
    """Display units → raw wire value (identity for unknown ids)."""
    meta = PARAMS.get(param_id)
    if meta is None or meta.to_display == 1.0:
        return int(round(display))
    return int(round(display / meta.to_display))


def adjustable_params(
    bounds: Mapping[int, tuple[int, int, int]],
    values: Mapping[int, int] | None = None,
    beverage_id: int | None = None,
) -> dict[int, ParamMeta]:
    """Adjustable ``ParamMeta`` subset for a recipe.

    Mirrors the app's brew-sheet builder ``MainBeveragesFragment.E1()``
    for a product-coded machine (``EcamMachine.N()`` — research.md R12):
    a control exists when its ingredient is in the recipe (``pid in
    bounds``) and — for the quantity controls — the stored amount is
    positive (``k() > 0``/``u() > 0``/``C() > 0``, hiding unconfigured
    recipes like Mug To-Go and empty custom slots whose amount is 0).
    Aroma additionally needs a positive coffee amount (``U0``). BeanAdapt
    (id ``0xC8``) gets the coffee-amount control alone — the app's
    ``id == 200`` special case — because its other fields are configured
    in the BeanAdapt flow, not at brew time. ``values`` are the recipe's
    current amounts (profile read); the descriptor default is used for a
    missing entry. ``min < max`` still gates every control — a slider
    needs a real range.
    """
    if not bounds:
        return {}
    live = values or {}

    def _current(pid: int) -> int:
        return live.get(pid, bounds.get(pid, (0, 0, 0))[1])

    def _usable(pid: int) -> bool:
        return pid in bounds and bounds[pid][0] < bounds[pid][2]

    enabled: set[int] = (
        {TAG_COFFEE} if _usable(TAG_COFFEE) and _current(TAG_COFFEE) > 0 else set()
    )
    if not is_bean_beverage(beverage_id or -1):
        if _usable(TAG_TASTE) and _current(TAG_COFFEE) > 0:
            enabled.add(TAG_TASTE)
        if _usable(TAG_MILK) and _current(TAG_MILK) > 0:
            enabled.add(TAG_MILK)
        if _usable(TAG_WATER) and _current(TAG_WATER) > 0:
            enabled.add(TAG_WATER)
        if _usable(13):  # THE_TEMP — tea temperature picker (R12)
            enabled.add(13)
    return {pid: PARAMS[pid] for pid in enabled if pid in PARAMS}
