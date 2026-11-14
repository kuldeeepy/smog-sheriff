"""
Air model: works out what a PM sensor would read at any point in the scene.

This file is pure maths. It knows nothing about Cyberwave or robots, so it can
be tested on its own (run `python air_model.py`).

How the reading at a point is built:

    reading = city background  +  smoke from each hidden source  +  sensor noise

Smoke from a source drifts *downwind* in a cone that widens as it travels,
and gets weaker the further it goes. Upwind of a source you only smell it if
you're very close. This is a simplified picture of a real smoke plume: good
enough for a robot to follow, not a scientific dispersion model.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Pollution "fingerprints"
#
# Different polluters produce a different mix of fine (PM2.5) and coarse
# (PM10) particles. For every 1 unit of extra PM2.5 a source produces, this
# is how much extra PM10 comes with it.
#
#   Burning  -> mostly fine smoke particles, so PM10 is only a bit higher.
#   Dust     -> mostly coarse particles, so PM10 is much higher than PM2.5.
# ---------------------------------------------------------------------------
PM10_PER_PM25 = {
    "garbage_burning": 1.3,
    "construction_dust": 4.0,
}


@dataclass
class Source:
    """One hidden polluter in the scene."""

    name: str
    type: str          # "garbage_burning" or "construction_dust"
    x: float           # metres east of scene centre
    y: float           # metres north of scene centre
    strength: float    # extra PM2.5 (µg/m³) right next to the source


@dataclass
class Conditions:
    """Today's weather and city-wide pollution."""

    wind_from_degrees: float   # where the wind comes FROM (0 = north, 90 = east)
    wind_speed: float          # metres per second
    background_pm25: float     # normal city-wide PM2.5, µg/m³
    background_pm10: float     # normal city-wide PM10, µg/m³


@dataclass
class Reading:
    """What the sensor reports at one moment."""

    pm25: float
    pm10: float


def wind_heading(conditions: Conditions) -> tuple[float, float]:
    """
    Return a unit vector pointing in the direction the smoke travels.

    Weather reports give the direction the wind comes FROM, so the smoke
    travels the opposite way. Example: a wind "from the north-west" (315°)
    carries smoke towards the south-east.
    """
    blowing_towards = math.radians(conditions.wind_from_degrees + 180)
    # Compass angles start at north and go clockwise, so east = sin, north = cos.
    return math.sin(blowing_towards), math.cos(blowing_towards)


def smoke_from_source(source: Source, x: float, y: float, conditions: Conditions) -> float:
    """
    Extra PM2.5 that one source adds at point (x, y).

    We split the distance from the source to the point into two parts:
      - downwind:  how far along the wind direction the point is
      - sideways:  how far off to the side of the smoke's path it is
    """
    east, north = wind_heading(conditions)
    dx, dy = x - source.x, y - source.y

    downwind = dx * east + dy * north
    sideways = -dx * north + dy * east

    # Stronger wind spreads smoke further but thins it out.
    dilution = max(conditions.wind_speed, 0.5)

    if downwind > 0:
        # Inside the drifting plume: the cone gets wider with distance...
        plume_width = 0.5 + 0.35 * downwind
        how_centred = math.exp(-(sideways ** 2) / (2 * plume_width ** 2))
        # ...and the smoke gets weaker the further it has travelled.
        how_far = 1.0 / (1.0 + 0.4 * downwind) ** 1.5
        return source.strength * how_centred * how_far / dilution

    # Upwind of the source: you only notice it when standing almost on top of it.
    distance = math.hypot(dx, dy)
    return source.strength * math.exp(-(distance ** 2) / 2.0) / dilution


def read_sensor(
    x: float,
    y: float,
    sources: list[Source],
    conditions: Conditions,
    noise: float = 0.05,
) -> Reading:
    """
    What a PM sensor at (x, y) would read right now.

    `noise` adds a little random wobble (5% by default), because real
    sensors never give perfectly smooth numbers.
    """
    extra_pm25 = 0.0
    extra_pm10 = 0.0

    for source in sources:
        smoke = smoke_from_source(source, x, y, conditions)
        extra_pm25 += smoke
        extra_pm10 += smoke * PM10_PER_PM25.get(source.type, 1.0)

    pm25 = conditions.background_pm25 + extra_pm25
    pm10 = conditions.background_pm10 + extra_pm10

    def wobble(value: float) -> float:
        return max(0.0, value * random.gauss(1.0, noise))

    return Reading(pm25=round(wobble(pm25), 1), pm10=round(wobble(pm10), 1))


# ---------------------------------------------------------------------------
# Quick self-check: print a small map of PM2.5 readings across the scene.
# Run:  python air_model.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    demo_sources = [
        Source("Garbage fire", "garbage_burning", x=-5, y=3, strength=900),
        Source("Sand pile", "construction_dust", x=4, y=-4, strength=250),
    ]
    demo_conditions = Conditions(
        wind_from_degrees=315, wind_speed=2.0, background_pm25=300, background_pm10=450
    )

    print("PM2.5 across the scene (north at top, wind from the north-west):\n")
    for y in range(8, -9, -2):
        row = []
        for x in range(-10, 11, 2):
            reading = read_sensor(x, y, demo_sources, demo_conditions, noise=0)
            row.append(f"{reading.pm25:5.0f}")
        print(f"y={y:+3d} " + " ".join(row))
    print("       " + " ".join(f"x={x:+3d}" for x in range(-10, 11, 2)))
