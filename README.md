# Smog Sheriff 🚨

**A robot dog that finds the hidden polluters in a Delhi neighbourhood.**

A Unitree Go2 patrols a colony on [Cyberwave](https://cyberwave.com). When its air sensor picks up
pollution well above the city's background level, it follows the smoke **upwind** to the
source, identifies what it is (garbage burning or construction dust), and raises a
**Cyberwave alert** with the evidence so a person can act on it. Alerts near a school are
marked critical.

Everything runs on Cyberwave's platform through its Python SDK: the 3D scene, the robot
twin, live telemetry, and alerts.

## What's real and what's simulated

| Real | Simulated |
|---|---|
| Today's wind at the location (Open-Meteo, free) | The hidden pollution sources and their plumes |
| Robot twin, 3D scene, metrics, alerts (Cyberwave) | The robot's PM2.5 / PM10 sensor |
| Typical Delhi winter background PM (≈300 µg/m³) | |

## How it works

```
config.toml     the map (roads, buildings, school), patrol route, hidden sources
build_scene.py  turns the map into the Cyberwave 3D scene
air_model.py    what a PM sensor would read at any point, given wind + sources
agent.py        the robot's brain: patrol → smell a spike → walk upwind →
                overshoot the source → go back to the strongest spot →
                identify by the PM10/PM2.5 fingerprint → raise a Cyberwave alert
```

Every step the robot publishes `air.pm25`, `air.pm10`, wind and its status to its twin as
Cyberwave metrics, so they appear as live data in the dashboard.

### Source fingerprints

| Source | PM10 : PM2.5 |
|---|---|
| Garbage burning | ≈ 1.3 (mostly fine smoke) |
| Construction dust | ≈ 4 (coarse dust) |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CYBERWAVE_API_KEY=your_key      # never commit this

python build_scene.py     # build the neighbourhood in your Cyberwave environment
python agent.py           # start the robot (live in Cyberwave)
python agent.py --offline # or try the brain locally, no Cyberwave needed
```

Set your robot twin and environment in `config.toml` under `[cyberwave]`.

## Change the place

Edit `config.toml`: location and coordinates (for live wind), the map, the patrol route
and the hidden `[[sources]]`. No code changes needed.
