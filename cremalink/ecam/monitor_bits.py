"""
Monitor frame bit tables — the machine's switch and alarm words.

Names come from the official enums in Coffee Link 4.9.6 (``m6.p`` for
switches, ``m6.l`` for alarms); see ``research/protocol.md`` Registers 4.1
and 4.2. Values are the raw bit states — whether a set bit means "fault"
or "present" is given by each label.

The switches word is 2 bytes (bit ``n`` = ``switches[n // 8] >> n % 8``);
the alarms word is 4 bytes assembled from non-contiguous payload bytes by
:class:`~cremalink.parsing.monitor.frame.MonitorFrame`.
"""

from __future__ import annotations

from typing import NamedTuple


class BitDef(NamedTuple):
    """One documented monitor bit."""

    key: str
    label: str
    #: Rare hardware faults / dual-hopper bits: expose, but off by default.
    common: bool = True


#: Switch bits. Bits 11, 12 and 15 are unmapped (cold-milk accessory bits on
#: striker/maestosa models). Bit 3 (waste container) is exposed by the
#: dedicated ``is_waste_container_missing`` flag instead.
SWITCH_BITS: dict[int, BitDef] = {
    0: BitDef("water_spout", "Water Spout"),
    1: BitDef("motor_up", "Motor Up"),
    2: BitDef("motor_down", "Motor Down"),
    4: BitDef("water_tank_absent", "Water Tank Absent"),
    5: BitDef("knob", "Knob"),
    6: BitDef("water_level_low", "Water Level Low"),
    7: BitDef("coffee_jug_present", "Coffee Jug Present"),
    8: BitDef("milk_carafe_present", "Milk Carafe Present"),
    9: BitDef("choco_tank_present", "Chocolate Tank Present"),
    10: BitDef("clean_knob", "Clean Knob"),
    13: BitDef("door_opened", "Door Opened"),
    14: BitDef("preground_door_opened", "Preground Door Opened"),
}

#: Alarm bits. Bits 0, 1 and 13 (water tank empty / waste container full /
#: tank in position) are exposed by the dedicated ``is_watertank_*`` and
#: ``is_waste_container_full`` flags. Bits 28-31 are unmapped.
ALARM_BITS: dict[int, BitDef] = {
    2: BitDef("descale", "Descale Needed"),
    3: BitDef("replace_water_filter", "Replace Water Filter"),
    4: BitDef("ground_too_fine", "Coffee Ground Too Fine"),
    5: BitDef("beans_empty", "Coffee Beans Empty"),
    6: BitDef("machine_to_service", "Service Required"),
    7: BitDef("heater_probe_failure", "Coffee Heater Probe Failure"),
    8: BitDef("too_much_coffee", "Too Much Coffee"),
    9: BitDef("infuser_motor_not_working", "Infuser Motor Not Working"),
    10: BitDef("steamer_probe_failure", "Steamer Probe Failure"),
    11: BitDef("empty_drip_tray", "Empty Drip Tray"),
    12: BitDef("hydraulic_circuit_problem", "Hydraulic Circuit Problem"),
    14: BitDef("clean_knob", "Clean Knob"),
    15: BitDef("beans_empty_hopper_2", "Coffee Beans Empty (Hopper 2)", False),
    16: BitDef("tank_too_full", "Water Tank Too Full"),
    17: BitDef("bean_hopper_absent", "Bean Hopper Absent", False),
    18: BitDef("grid_presence", "Drip Grid Presence"),
    19: BitDef("infuser_not_in_position", "Infuser Not In Position"),
    20: BitDef("not_enough_coffee", "Not Enough Coffee"),
    21: BitDef("expansion_comm_problem", "Expansion Module Comm Problem", False),
    22: BitDef("expansion_submodules_problem", "Expansion Submodule Problem", False),
    23: BitDef("grinding_unit_1_problem", "Grinding Unit 1 Problem", False),
    24: BitDef("grinding_unit_2_problem", "Grinding Unit 2 Problem", False),
    25: BitDef("condense_fan_problem", "Condense Fan Problem", False),
    26: BitDef("clock_bt_comm_problem", "Clock Board Comm Problem", False),
    27: BitDef("spi_comm_problem", "SPI Comm Problem", False),
}


def decode_bits(word: bytes, table: dict[int, BitDef]) -> dict[str, bool | None]:
    """``{key: bit state}`` for every documented bit of ``word``.

    A bit whose byte is missing (a short frame) maps to ``None`` rather
    than ``False`` — absent data is not a cleared bit.
    """
    return {
        bit.key: (
            bool(word[n // 8] >> (n % 8) & 1) if n // 8 < len(word) else None
        )
        for n, bit in table.items()
    }
