"""
Smog Sheriff: the robot's brain, plus a small web server for the dashboard.

What it does, every second:

    PATROLLING     walk the patrol loop, sniffing the air
         │  PM2.5 jumps well above the city background
         ▼
    INVESTIGATING  head upwind towards stronger readings, step by step
         │  readings stop getting stronger → we're at the source
         ▼
    INSPECTING     take a camera photo, work out what the source is
         │
         ▼
    REPORTING      record the incident and raise a Cyberwave alert,
                   which a person reviews in Cyberwave's Alerts tab
         │
         ▼
    back to PATROLLING

Two ways to run it:

    python agent.py --offline     # no Cyberwave at all, just prints (quick test)
    python agent.py               # live: moves the Go2 twin in Cyberwave

Watch it in the Cyberwave dashboard: the robot moves in the 3D scene, PM
readings show up as sensor charts on the twin, and incidents as alerts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import tomllib
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from air_model import PM10_PER_PM25, Conditions, Source, read_sensor


HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.toml"
PHOTOS_DIR = HERE / "photos"

# Which Cyberwave catalog model looks at the evidence photo.
VISION_MODEL = "openrouter/models/claude-sonnet-55"


# ===========================================================================
# 1. Settings and today's conditions
# ===========================================================================

def load_config() -> dict:
    """Read config.toml."""
    with open(CONFIG_FILE, "rb") as file:
        return tomllib.load(file)


def fetch_json(url: str) -> dict:
    """Download a URL and parse it as JSON (gives up after 10 s)."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def get_conditions(config: dict) -> tuple[Conditions, str]:
    """
    Today's wind and background pollution, plus a note saying where they came from.

    Wind can be live (Open-Meteo); background PM is fixed by default because
    the free model badly underestimates Delhi (see config.toml).
    """
    location = config["location"]
    wind_from = config["weather"]["wind_from_degrees"]
    wind_speed = config["weather"]["wind_speed_m_per_s"]
    pm25 = config["background"]["pm25"]
    pm10 = config["background"]["pm10"]
    notes = []

    coords = f"latitude={location['latitude']}&longitude={location['longitude']}"

    if location.get("use_live_wind"):
        try:
            current = fetch_json(
                f"https://api.open-meteo.com/v1/forecast?{coords}"
                "&current=wind_speed_10m,wind_direction_10m&wind_speed_unit=ms"
            )["current"]
            wind_from, wind_speed = current["wind_direction_10m"], current["wind_speed_10m"]
            notes.append(f"live wind ({current['time']} UTC)")
        except Exception as error:
            notes.append(f"fixed wind (live lookup failed: {error})")
    else:
        notes.append("fixed wind")

    if location.get("use_live_background"):
        try:
            current = fetch_json(
                "https://air-quality-api.open-meteo.com/v1/air-quality"
                f"?{coords}&current=pm2_5,pm10"
            )["current"]
            pm25, pm10 = current["pm2_5"], current["pm10"]
            notes.append("live background PM (Open-Meteo model)")
        except Exception as error:
            notes.append(f"fixed background PM (live lookup failed: {error})")
    else:
        notes.append("typical Delhi winter background PM")

    conditions = Conditions(
        wind_from_degrees=wind_from,
        wind_speed=wind_speed,
        background_pm25=pm25,
        background_pm10=pm10,
    )
    return conditions, " · ".join(notes)


# ===========================================================================
# 2. The robot body: how we move it and look through its camera
# ===========================================================================

class OfflineBody:
    """A pretend robot that only exists in this program. Used for UI previews."""

    def __init__(self, start: tuple[float, float]):
        self.x, self.y = start

    def position(self) -> tuple[float, float]:
        return self.x, self.y

    def move_to(self, x: float, y: float, heading_deg: float | None = None) -> None:
        self.x, self.y = x, y

    def photo(self) -> bytes | None:
        return None  # no camera offline

    def ask_vision_model(self, photo: bytes, prompt: str) -> str | None:
        return None  # no AI offline; the fingerprint is used instead

    def publish_reading(self, reading, status: str) -> None:
        pass  # nothing to send offline

    def raise_alert(self, incident: "Incident") -> None:
        pass


class CyberwaveBody:
    """
    The Go2 twin in Cyberwave.

    Movement: we place the twin step by step (edit_position). It works whether
    or not a physics simulation is running, and each step is small, so it looks
    like the robot is walking around the scene.
    """

    def __init__(self, config: dict):
        from cyberwave import Cyberwave  # only needed in live mode

        self.environment = config["cyberwave"]["environment"]
        self.client = Cyberwave(environment_id=self.environment)
        self.robot = self.client.twin(twin_id=config["cyberwave"]["rover_twin"])
        self.background = None  # set by the brain, sent alongside each reading
        self.x, self.y = config["robot"]["start"]
        self.robot.edit_position(x=self.x, y=self.y, z=0.0)
        self.client.mqtt.connect()

    def position(self) -> tuple[float, float]:
        return self.x, self.y

    def move_to(self, x: float, y: float, heading_deg: float | None = None) -> None:
        """
        Place the dog at (x, y), facing `heading_deg` (compass: 0 = north, 90 = east).

        Position and rotation go in ONE update so each step is a single, fast
        API call (~1 s) and the dog visibly turns to face where it's walking.
        """
        state = {"position_x": x, "position_y": y, "position_z": 0.0}
        if heading_deg is not None:
            yaw = 90.0 - heading_deg  # scene yaw is counter-clockwise from east (+x)
            qx, qy, qz, qw = self.robot._euler_to_quaternion(0, 0, yaw)
            state.update(rotation_x=qx, rotation_y=qy, rotation_z=qz, rotation_w=qw)
        try:
            self.client.twins.update_state(self.robot.uuid, state)
        except Exception as error:
            # Cyberwave hiccup (e.g. a temporary 502): skip this step, keep patrolling.
            print(f"   (move skipped, Cyberwave error: {type(error).__name__}); retrying next step")
            time.sleep(2)
            return
        self.x, self.y = x, y

    def photo(self) -> bytes | None:
        """Latest camera frame, if a Cyberwave simulation is running."""
        try:
            return self.robot.get_frame("bytes")
        except Exception as error:
            print(f"   (no camera frame: {type(error).__name__})")
            return None

    def ask_vision_model(self, photo: bytes, prompt: str) -> str | None:
        """Ask a model from Cyberwave's catalog about the photo. Returns its text answer."""
        if getattr(self, "vision_unavailable", False):
            return None
        try:
            result = self.client.models.run(VISION_MODEL, image=photo, prompt=prompt)
            output = getattr(result, "output", None)
            if output is None:
                # Cyberwave queued the run on a cloud node instead of answering now.
                print("   (vision model run was queued; using the air fingerprint for now)")
                return None
            return output if isinstance(output, str) else json.dumps(output)
        except Exception as error:
            print(f"   (vision model unavailable: {str(error).splitlines()[-1][:160]})")
            self.vision_unavailable = True  # don't retry on every incident
            return None

    def publish_reading(self, reading, status: str) -> None:
        """
        Send the sensor reading to the twin as Cyberwave metrics, so it shows up
        as live charts on the robot in the dashboard.
        """
        status_code = {"patrolling": 0, "investigating": 1, "inspecting": 2, "reporting": 3}
        try:
            self.client.mqtt.publish_metrics(self.robot.uuid, {
                "air.pm25": reading.pm25,
                "air.pm10": reading.pm10,
                "air.pm25_background": self.background.background_pm25,
                "wind.from_degrees": self.background.wind_from_degrees,
                "wind.speed": self.background.wind_speed,
                "agent.status": status_code.get(status, 0),
            })
        except Exception as error:
            print(f"   (could not send reading: {error})")

    def raise_alert(self, incident: "Incident") -> None:
        """Create an alert on the robot twin, visible in Cyberwave's Alerts tab."""
        try:
            self.robot.alerts.create(
                name=f"Pollution source: {incident.verdict}",
                description=(
                    f"{incident.reason} PM2.5 {incident.pm25:.0f} vs background "
                    f"{incident.background_pm25:.0f} at ({incident.x:.1f}, {incident.y:.1f})."
                ),
                alert_type="pollution_source",
                severity="critical" if incident.near_school else "warning",
                source_type="simulation",
                category="business",
                metadata={"incident_id": incident.id, "x": incident.x, "y": incident.y},
            )
        except Exception as error:
            print(f"   (could not raise Cyberwave alert: {error})")


# ===========================================================================
# 3. Incidents (what the robot found)
# ===========================================================================

@dataclass
class Incident:
    id: str
    time: float
    x: float
    y: float
    source_type: str       # "garbage_burning", "construction_dust" or "unknown"
    verdict: str           # short human-readable label
    confidence: float      # 0..1
    reason: str
    pm25: float
    pm10: float
    background_pm25: float
    near_school: bool
    photo_url: str | None = None
    status: str = "pending"   # "pending", "approved" or "rejected"


def guess_source_from_air(pm25: float, pm10: float, conditions: Conditions) -> tuple[str, float, str]:
    """
    Guess the source type from the PM10 : PM2.5 mix of the *extra* pollution.

    Burning makes mostly fine particles (ratio near 1.3); dust makes mostly
    coarse ones (ratio near 4). We split the difference at 2.5.
    """
    extra_pm25 = max(pm25 - conditions.background_pm25, 1.0)
    extra_pm10 = max(pm10 - conditions.background_pm10, 0.0)
    ratio = extra_pm10 / extra_pm25

    burning, dust = PM10_PER_PM25["garbage_burning"], PM10_PER_PM25["construction_dust"]
    if ratio < (burning + dust) / 2:
        return "garbage_burning", 0.7, f"PM2.5-heavy plume (PM10:PM2.5 ≈ {ratio:.1f}), typical of burning."
    return "construction_dust", 0.7, f"PM10-heavy plume (PM10:PM2.5 ≈ {ratio:.1f}), typical of dust."


VERDICTS = {
    "garbage_burning": "Open garbage burning",
    "construction_dust": "Uncovered construction dust",
    "unknown": "Unidentified pollution source",
}


# ===========================================================================
# 4. The brain
# ===========================================================================

@dataclass
class Brain:
    config: dict
    body: OfflineBody | CyberwaveBody
    conditions: Conditions
    conditions_note: str
    sources: list[Source]
    mode: str

    status: str = "patrolling"
    status_text: str = "Starting patrol"
    heading_deg: float = 0.0
    trail: list = field(default_factory=list)
    readings: list = field(default_factory=list)
    incidents: list = field(default_factory=list)

    # Internal bookkeeping
    _next_waypoint: int = 0
    _steps_investigating: int = 0
    _no_improvement: int = 0
    _best: tuple = (0.0, 0.0, 0.0)   # (x, y, pm25) strongest reading so far

    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- small helpers ----------------------------------------------------

    def sniff(self, x: float, y: float):
        """Read the (stand-in) PM sensor at a point."""
        return read_sensor(x, y, self.sources, self.conditions)

    def step_towards(self, target_x: float, target_y: float) -> None:
        """Move one step (step_m) towards a target point."""
        x, y = self.body.position()
        dx, dy = target_x - x, target_y - y
        distance = math.hypot(dx, dy)
        step = min(self.config["robot"]["step_m"], distance)
        if distance > 0:
            self.heading_deg = math.degrees(math.atan2(dx, dy)) % 360
            self.body.move_to(x + dx / distance * step, y + dy / distance * step, self.heading_deg)

    def near_known_incident(self, x: float, y: float, radius: float = 4.0) -> bool:
        """True if we've already reported a source close to this point."""
        return any(math.hypot(i.x - x, i.y - y) < radius for i in self.incidents)

    def smoke_from_known_source(self, x: float, y: float) -> bool:
        """
        True if this smell is probably coming from a source we already reported:
        a known incident lies upwind of us (within ~35° of the wind) and not too far.
        Stops the robot from re-investigating the same plume over and over.
        """
        upwind = self.conditions.wind_from_degrees
        for i in self.incidents:
            dx, dy = i.x - x, i.y - y
            distance = math.hypot(dx, dy)
            if distance < 4.0:
                return True
            bearing = math.degrees(math.atan2(dx, dy)) % 360
            off_wind = abs((bearing - upwind + 180) % 360 - 180)
            if distance < 35.0 and off_wind < 35.0:
                return True
        return False

    def near_school(self, x: float, y: float, margin: float = 3.0) -> bool:
        """True if the point is within `margin` metres of the school."""
        for b in self.config.get("buildings", []):
            if b["kind"] == "school":
                if abs(x - b["x"]) <= b["w"] / 2 + margin and abs(y - b["y"]) <= b["d"] / 2 + margin:
                    return True
        return False

    # ---- one tick of the main loop ---------------------------------------

    def tick(self) -> None:
        x, y = self.body.position()
        reading = self.sniff(x, y)
        excess = reading.pm25 - self.conditions.background_pm25

        with self.lock:
            self.trail.append([round(x, 2), round(y, 2), reading.pm25])
            self.readings.append({"t": time.time(), "pm25": reading.pm25, "pm10": reading.pm10})
            del self.trail[:-400], self.readings[:-120]
        self.body.publish_reading(reading, self.status)

        if self.status == "patrolling":
            self.patrol(x, y, excess)
        elif self.status == "investigating":
            self.investigate(x, y, reading.pm25)

    def patrol(self, x: float, y: float, excess: float) -> None:
        """Walk the loop; switch to investigating on a big enough spike."""
        trigger = self.config["investigation"]["trigger_above_background"]
        if excess > trigger and not self.smoke_from_known_source(x, y):
            self.status = "investigating"
            self.status_text = f"Spike! PM2.5 is {excess:.0f} above background — heading upwind"
            self._steps_investigating = 0
            self._no_improvement = 0
            self._best = (x, y, self.smell(x, y, samples=5))  # averaged, not one noisy read
            print(f"⚠️  {self.status_text}")
            return

        waypoints = self.config["robot"]["waypoints"]
        target_x, target_y = waypoints[self._next_waypoint]
        if math.hypot(target_x - x, target_y - y) < 0.3:
            self._next_waypoint = (self._next_waypoint + 1) % len(waypoints)
            target_x, target_y = waypoints[self._next_waypoint]
        self.status_text = f"Patrolling — heading to WP{self._next_waypoint + 1}"
        self.step_towards(target_x, target_y)

    def smell(self, x: float, y: float, samples: int = 3) -> float:
        """Average a few sensor reads at a point, to smooth out sensor noise."""
        return sum(self.sniff(x, y).pm25 for _ in range(samples)) / samples

    def investigate(self, x: float, y: float, pm25_here: float) -> None:
        """
        Follow the smoke upwind, like a sniffer dog.

        Each step: sniff a little ahead in a few directions around "straight
        upwind" and step towards the strongest smell. Smoke is strongest at
        its source and fades sharply once you walk past it, so we keep going
        until the smell has clearly dropped off (we overshot), then go back to
        the strongest spot we found. That spot is the source.
        """
        self._steps_investigating += 1
        step = self.config["robot"]["step_m"]
        upwind = self.conditions.wind_from_degrees  # smoke comes FROM here

        # Sniff ahead in a fan around the upwind direction.
        candidates = []
        for offset in (0, -30, 30, -60, 60):
            angle = math.radians(upwind + offset)
            px, py = x + step * math.sin(angle), y + step * math.cos(angle)
            candidates.append((self.smell(px, py, samples=5), px, py))
        best_pm, best_x, best_y = max(candidates)

        # Remember the strongest spot so far.
        here = self.smell(x, y, samples=5)
        if here > self._best[2]:
            self._best = (x, y, here)

        # Passed the source? The smell here is well below the strongest we've had.
        background = self.conditions.background_pm25
        best_excess = self._best[2] - background
        passed_source = here - background < 0.5 * best_excess
        self._no_improvement = self._no_improvement + 1 if passed_source else 0

        self.status_text = f"Investigating — following the plume (PM2.5 {pm25_here:.0f})"
        self.step_towards(best_x, best_y)

        # Walk at least a few steps first, so one noisy reading can't end the search.
        reached_source = self._no_improvement >= 2 and self._steps_investigating >= 5
        gave_up = self._steps_investigating >= self.config["investigation"]["max_steps"]
        if reached_source or gave_up:
            self.inspect(gave_up)

    def inspect(self, gave_up: bool) -> None:
        """At the source: photo + identify + report."""
        self.status = "inspecting"
        x, y, pm25 = self._best
        self.body.move_to(x, y)

        # Already reported this one? Don't raise a second alert; keep patrolling.
        if self.near_known_incident(x, y):
            print(f"   (already reported a source near ({x:.1f}, {y:.1f}); back to patrol)")
            self.status = "patrolling"
            return

        reading = self.sniff(x, y)
        self.status_text = "Inspecting — taking a photo and identifying the source"
        print(f"🔎 {self.status_text} at ({x:.1f}, {y:.1f})")

        source_type, confidence, reason = guess_source_from_air(reading.pm25, reading.pm10, self.conditions)
        if gave_up:
            source_type, confidence, reason = "unknown", 0.3, "Lost the plume before pinpointing it."

        incident_id = f"inc-{len(self.incidents) + 1}"
        photo_url = None
        photo = self.body.photo()
        if photo:
            PHOTOS_DIR.mkdir(exist_ok=True)
            (PHOTOS_DIR / f"{incident_id}.jpg").write_bytes(photo)
            photo_url = f"/photos/{incident_id}.jpg"

            answer = self.body.ask_vision_model(photo, vision_prompt(reading, self.conditions, source_type))
            source_type, confidence, reason = merge_ai_answer(answer, source_type, confidence, reason)

        incident = Incident(
            id=incident_id, time=time.time(), x=round(x, 2), y=round(y, 2),
            source_type=source_type, verdict=VERDICTS[source_type],
            confidence=round(confidence, 2), reason=reason,
            pm25=reading.pm25, pm10=reading.pm10,
            background_pm25=self.conditions.background_pm25,
            near_school=self.near_school(x, y), photo_url=photo_url,
        )

        self.status = "reporting"
        self.status_text = f"Reported: {incident.verdict} — waiting for approval"
        with self.lock:
            self.incidents.append(incident)
        self.body.raise_alert(incident)
        print(f"🚨 {incident.verdict} at ({x:.1f}, {y:.1f}), confidence {confidence:.0%}")

        # Keep patrolling while a person reviews the report.
        self.status = "patrolling"



def vision_prompt(reading, conditions: Conditions, air_guess: str) -> str:
    """What we ask the vision model."""
    return (
        "You are checking evidence from a pollution-patrol robot in Delhi. "
        f"Its air sensor reads PM2.5 {reading.pm25:.0f} and PM10 {reading.pm10:.0f} µg/m³ "
        f"(city background {conditions.background_pm25:.0f} / {conditions.background_pm10:.0f}). "
        f"The air fingerprint suggests: {air_guess}. "
        "Look at the photo from the robot's camera. Reply with JSON only: "
        '{"source_type": "garbage_burning" | "construction_dust" | "unknown", '
        '"confidence": 0-1, "reason": "one short sentence"}. '
        "Use the photo to support or question the sensor evidence, not to replace it."
    )


def merge_ai_answer(answer: str | None, source_type: str, confidence: float, reason: str):
    """Combine the vision model's answer with the air fingerprint."""
    if not answer:
        return source_type, confidence, reason + " (No AI photo check available.)"
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    try:
        ai = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        ai = {}
    ai_type = ai.get("source_type")
    if ai_type not in VERDICTS:
        return source_type, confidence, reason + " (AI photo check was inconclusive.)"
    if ai_type == source_type:
        return source_type, max(confidence, float(ai.get("confidence", confidence))), f"{reason} Photo agrees: {ai.get('reason', '')}"
    return source_type, confidence * 0.6, f"{reason} Photo disagrees ({ai.get('reason', '')}) — needs human review."


# ===========================================================================
# 5. Start everything
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Smog Sheriff robot brain")
    parser.add_argument("--offline", action="store_true", help="run without Cyberwave (quick test)")
    args = parser.parse_args()

    config = load_config()
    conditions, note = get_conditions(config)
    sources = [Source(**s) for s in config.get("sources", [])]

    if args.offline:
        body, mode = OfflineBody(tuple(config["robot"]["start"])), "offline"
    else:
        if not os.environ.get("CYBERWAVE_API_KEY"):
            sys.exit("Set CYBERWAVE_API_KEY first, or use --offline.")
        body, mode = CyberwaveBody(config), "cyberwave"

    body.background = conditions
    brain = Brain(config=config, body=body, conditions=conditions,
                  conditions_note=note, sources=sources, mode=mode)

    print(f"📍 {config['location']['name']}  ·  mode: {mode}")
    print(f"🌬️  wind from {conditions.wind_from_degrees:.0f}° at {conditions.wind_speed:.1f} m/s  ·  {note}")
    print()

    pause = config["cyberwave"]["seconds_between_readings"]
    try:
        while True:
            brain.tick()
            time.sleep(pause)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
