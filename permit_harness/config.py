"""Every physical constant and legal limit the harness uses, with its source.

The math in rules.py is plain Python. The numbers below are the only assumptions.
Change them here, nowhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# --- Environment ---------------------------------------------------------
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB = os.getenv("MONGODB_DB", "infra_design_permits")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Cheap model designs, strong model reviews. Both via OpenRouter.
DESIGNER_MODEL = os.getenv("DESIGNER_MODEL", "anthropic/claude-sonnet-4.5")
REVIEWER_MODEL = os.getenv("REVIEWER_MODEL", "anthropic/claude-sonnet-4.5")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "1536"))
# Atlas Automated Embeddings (Voyage AI inside Atlas, M10+). When on, the repo never computes an
# embedding: Atlas embeds `lessons.text` on write and embeds the query text on $vectorSearch.
ATLAS_AUTO_EMBED = os.getenv("ATLAS_AUTO_EMBED", "true").lower() in {"1", "true", "yes"}
ATLAS_EMBED_MODEL = os.getenv("ATLAS_EMBED_MODEL", "voyage-3.5")

# --- Units ---------------------------------------------------------------
FT = 0.3048  # metres per foot
ACRE_M2 = 4046.8564224

# --- Facility target -----------------------------------------------------
TARGET_IT_MW = 40.0  # what the operator asked for
MIN_CAPACITY_FRACTION = 0.80  # below this the design is "useless" -> +20 penalty

# --- Equipment catalogue (the designer must place all of it) -------------
@dataclass(frozen=True)
class EquipmentSpec:
    kind: str
    count: int
    width_m: float
    length_m: float
    height_m: float
    note: str


EQUIPMENT: list[EquipmentSpec] = [
    EquipmentSpec("data_hall", 1, 60.0, 100.0, 15.0, "Single-storey hall, ~40 MW IT at 6.7 kW/m2"),
    EquipmentSpec("generator", 10, 4.0, 12.0, 5.0, "2 MW diesel standby gensets in enclosures"),
    EquipmentSpec("cooling", 6, 6.0, 12.0, 5.0, "Air-cooled chiller / dry cooler banks"),
    EquipmentSpec("substation", 1, 40.0, 40.0, 8.0, "Utility substation, needs road frontage"),
    EquipmentSpec("parking", 1, 40.0, 60.0, 0.0, "~100 spaces, impervious"),
]

# --- Noise (R1, R2) ------------------------------------------------------
# Caterpillar 3516B 2 MW standby genset in a sound-attenuated enclosure is
# typically quoted around 85 dBA at 7 m (manufacturer spec sheets range 82-88).
GENERATOR_DBA_AT_REF = 85.0
GENERATOR_REF_DISTANCE_M = 7.0
# Air-cooled chiller banks (e.g. Trane/Carrier 500-ton units) ~ 80 dBA at 10 m.
COOLING_DBA_AT_REF = 80.0
COOLING_REF_DISTANCE_M = 10.0
# Design options the agent can choose (real products, real cost trade-offs):
# critically-silenced genset enclosures reach ~70 dBA at 7 m; low-noise fan
# packages on dry coolers reach ~70 dBA at 10 m.
GENERATOR_DBA_BY_ENCLOSURE = {"standard": GENERATOR_DBA_AT_REF, "critical_silenced": 70.0}
COOLING_DBA_BY_OPTION = {"standard": COOLING_DBA_AT_REF, "low_noise": 70.0}
# Solid acoustic screen wall around the cooling yard: 8 dB insertion loss is a conservative
# figure for a barrier that breaks line of sight (FHWA barrier guidance: 5 dB minimum, 10+ typical).
COOLING_BARRIER_DB = 8.0
# Generator yard screening wall / berm (what Loudoun County and NoVA HOAs actually require):
# breaks line of sight from homes and gives ~5 dB on the gensets.
GENERATOR_SCREEN_DB = 5.0
BUILDING_SHIELDING_DB = 10.0  # flat credit if the data hall blocks line of sight (our assumption)
NIGHT_LIMIT_RESIDENTIAL_DBA = 50.0  # N.J.A.C. 7:29-1.2, 10pm-7am at residential property line
DAY_LIMIT_RESIDENTIAL_DBA = 65.0  # N.J.A.C. 7:29-1.2, 7am-10pm
LIMIT_COMMERCIAL_DBA = 65.0  # commercial/industrial receiving property, any time
# Standby generators are tested during the day. Emergency operation is exempt
# from the noise code (N.J.A.C. 7:29-1.4), so they are a DAY source only.
# Cooling runs 24/7 and is the night source.
GENERATORS_RUN_AT_NIGHT = False

# --- Wetlands (R3) -------------------------------------------------------
# N.J.A.C. 7:7A-3.3 transition areas: 150 ft exceptional, 50 ft intermediate, 0 ft ordinary.
BUFFER_EXCEPTIONAL_M = 150 * FT
BUFFER_INTERMEDIATE_M = 50 * FT
BUFFER_ORDINARY_M = 0.0
# NJDEP LU/LC LABEL20 -> resource value class (our mapping; exceptional needs a T&E
# species finding we cannot make from land cover alone, so nothing maps there by default).
WETLAND_CLASS_BY_LABEL: dict[str, str] = {
    "DECIDUOUS WOODED WETLANDS": "intermediate",
    "CONIFEROUS WOODED WETLANDS": "intermediate",
    "MIXED WOODED WETLANDS": "intermediate",
    "DECIDUOUS SCRUB/SHRUB WETLANDS": "intermediate",
    "CONIFEROUS SCRUB/SHRUB WETLANDS": "intermediate",
    "HERBACEOUS WETLANDS": "intermediate",
    "FRESHWATER TIDAL MARSHES": "exceptional",
    "SALINE MARSH (LOW MARSH)": "exceptional",
    "SALINE MARSH (HIGH MARSH)": "exceptional",
    "MANAGED WETLAND IN MAINTAINED LAWN GREENSPACE": "ordinary",
    "MANAGED WETLAND IN BUILT-UP MAINTAINED REC AREA": "ordinary",
    "DISTURBED WETLANDS (MODIFIED)": "ordinary",
}
BUFFER_BY_CLASS = {
    "exceptional": BUFFER_EXCEPTIONAL_M,
    "intermediate": BUFFER_INTERMEDIATE_M,
    "ordinary": BUFFER_ORDINARY_M,
}

# --- Air (R4, R5) --------------------------------------------------------
# A 2 MW diesel genset burns roughly 140 gal/hr at full load; at 137,000 Btu/gal
# that is ~19 MMBtu/hr heat input.
GENERATOR_HEAT_INPUT_MMBTU_HR = 19.0
GP005A_MAX_COMBINED_HEAT_INPUT = 100.0  # NJDEP General Permit GP-005A cap (MMBtu/hr, combined)
GP005A_MAX_TEST_HOURS_PER_ENGINE = 100  # hr/yr of non-emergency running
# EPA Tier 2 nonroad CI >560 kW: NOx+NMHC 6.4 g/kWh. Emergency engines are usually
# Tier 2; use 6.4 g/kWh as NOx (conservative).
GENERATOR_NOX_G_PER_KWH = 6.4
# EPA Tier 4 Final (>560 kW) NOx 0.67 g/kWh, i.e. with SCR aftertreatment.
GENERATOR_NOX_BY_TIER = {"tier2": 6.4, "tier4f": 0.67}
GENERATOR_KW = 2000.0
# Potential-to-emit convention for emergency engines: 500 hr/yr (EPA 1995 guidance).
PTE_HOURS_PER_YEAR = 500
NOX_MAJOR_SOURCE_TPY = 25.0  # NJ non-attainment: 25 tons/yr triggers Title V major source

# --- Water (R6) ----------------------------------------------------------
# Evaporative cooling ~ 1.8 gal per kWh of IT load (industry rule of thumb);
# closed-loop air-cooled ~ 0.1 gal/kWh (makeup only).
WATER_GAL_PER_KWH = {"evaporative": 1.8, "air_cooled": 0.1}
WATER_ALLOCATION_PERMIT_GPD = 100_000  # NJDEP Water Allocation Permit threshold
WATER_ALLOCATION_PERMIT_GPD_HIGHLANDS = 50_000

# --- Stormwater (R7) -----------------------------------------------------
MAJOR_DEV_DISTURBANCE_ACRES = 1.0  # N.J.A.C. 7:8-1.2
MAJOR_DEV_NEW_IMPERVIOUS_ACRES = 0.25
STORMWATER_BASIN_FRACTION = 0.10  # reserve basin area = 10% of impervious (our assumption)

# --- Zoning (R8) ---------------------------------------------------------
RESIDENTIAL_SETBACK_M = 200 * FT  # demo value, set per municipality

# --- Penalties -----------------------------------------------------------
PENALTY_HARD = 10
PENALTY_MEDIUM = 5
PENALTY_SOFT = 1
PENALTY_CAPACITY = 20

# --- Harness control -----------------------------------------------------
STALL_ROUNDS = 2  # best penalty unchanged this many rounds -> deterministic policy takes one round
