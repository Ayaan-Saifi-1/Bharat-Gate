"""BharatGate Data Module.

Provides prebaked multi-turn replay scenarios for 0ms demo exploration and benchmarking.
"""

from bharat_gate.data.prebaked_replays import (
    PrebakedScenario,
    SCENARIO_1,
    SCENARIO_2,
    SCENARIO_3,
    get_all_scenarios,
    get_scenario_by_id,
)

__all__ = [
    "PrebakedScenario",
    "SCENARIO_1",
    "SCENARIO_2",
    "SCENARIO_3",
    "get_all_scenarios",
    "get_scenario_by_id",
]
