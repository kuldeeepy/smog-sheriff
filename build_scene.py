"""
Build the Smog Sheriff 3D scene in Cyberwave from config.toml.

The map (ground, roads, buildings, pollution piles) is described once in
config.toml. This script turns it into Cyberwave scene objects ("procedural
primitives") and saves them on the environment, replacing what was there.
The robot twin is not touched.

Run:   CYBERWAVE_API_KEY=... python build_scene.py
"""

import tomllib
from pathlib import Path

from cyberwave import Cyberwave
from cyberwave.rest.models.environment_create_schema import EnvironmentCreateSchema

CONFIG = tomllib.loads((Path(__file__).parent / "config.toml").read_text())

# One clear colour per kind of place, so everything is easy to tell apart.
COLOURS = {
    "ground": "#d6d3c4",   # dusty light ground
    "road": "#334155",     # dark asphalt
    "market": "#ea580c",   # orange
    "shops": "#3b82f6",    # blue
    "school": "#facc15",   # yellow (sensitive place)
    "homes": "#e7c9a0",    # beige
    "site": "#64748b",     # grey hoarding walls
    "park": "#22c55e",     # green
    "block": "#a8a29e",    # neutral grey
    "garbage_burning": "#7f1d1d",    # dark red-brown garbage heap
    "construction_dust": "#d6a75c",  # sand
}

NO_ROTATION = {"w": 1.0, "x": 0.0, "y": 0.0, "z": 0.0}


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def flat(name, x, y, w, d, colour, z=0.0, thickness=0.02):
    """A flat plate on the ground: used for the ground, roads and parks."""
    return {
        "id": slug(name), "name": name, "visible": True, "locked": False, "fixed_base": True,
        "template_key": "cyberwave/ground", "primitive_type": "ground", "template_version": "1.0.0",
        "parameters": {"color": colour, "width": w, "depth": d, "thickness": thickness, "material": "concrete"},
        "size": {"x": w, "y": d, "z": thickness},
        "position": {"x": x, "y": y, "z": z + thickness / 2},
        "rotation": NO_ROTATION,
    }


def box(name, x, y, w, d, h, colour):
    """A solid block: buildings and piles. Fixed in place so physics won't move it."""
    return {
        "id": slug(name), "name": name, "visible": True, "locked": False, "fixed_base": True,
        "template_key": "cyberwave/box", "primitive_type": "box", "template_version": "1.0.0",
        "parameters": {"color": colour, "width": w, "depth": d, "height": h, "material": "matte"},
        "size": {"x": w, "y": d, "z": h},
        "position": {"x": x, "y": y, "z": h / 2},
        "rotation": NO_ROTATION,
    }


def walls(name, x, y, w, d, h, colour):
    """An open fenced rectangle: the building site's hoarding."""
    return {
        "id": slug(name), "name": name, "visible": True, "locked": False, "fixed_base": True,
        "template_key": "cyberwave/wall_path", "primitive_type": "wall_path", "template_version": "1.0.0",
        "parameters": {
            "color": colour, "width": 0.2, "thickness": 0.2, "height": h, "closed": True,
            "material": "concrete",
            "points": [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]],
        },
        "size": {"x": w, "y": d, "z": h},
        "position": {"x": x, "y": y, "z": h / 2},
        "rotation": NO_ROTATION,
    }


def add_visual(obj: dict) -> dict:
    """
    Attach the 'compiled' look of the object (shape, colour, boxes) that the
    Cyberwave viewer draws. The dashboard normally fills this in itself; when we
    write the scene through the API we have to supply it.
    """
    p, pos, size = obj["parameters"], obj["position"], obj["size"]
    floor_z = pos["z"] - size["z"] / 2
    if obj["primitive_type"] == "wall_path":
        w, d, h, t = size["x"], size["y"], size["z"], p["thickness"]
        boxes = [
            {"yaw": 0.0, "size": {"x": w, "y": t, "z": h}, "center": {"x": 0, "y": -d / 2, "z": h / 2}},
            {"yaw": 1.5708, "size": {"x": d, "y": t, "z": h}, "center": {"x": w / 2, "y": 0, "z": h / 2}},
            {"yaw": 3.1416, "size": {"x": w, "y": t, "z": h}, "center": {"x": 0, "y": d / 2, "z": h / 2}},
            {"yaw": -1.5708, "size": {"x": d, "y": t, "z": h}, "center": {"x": -w / 2, "y": 0, "z": h / 2}},
        ]
        backend = "compound_boxes"
    else:
        boxes = [{"size": dict(size), "center": {"x": 0.0, "y": 0.0, "z": size["z"] / 2}}]
        backend = "inline_primitive"
    obj["compiled"] = {
        "pose": {"position": {"x": pos["x"], "y": pos["y"], "z": floor_z}, "rotation": NO_ROTATION},
        "bbox": {
            "min": {"x": pos["x"] - size["x"] / 2, "y": pos["y"] - size["y"] / 2, "z": floor_z},
            "max": {"x": pos["x"] + size["x"] / 2, "y": pos["y"] + size["y"] / 2, "z": floor_z + size["z"]},
        },
        "visual": {"boxes": boxes, "color": p["color"], "shape": obj["primitive_type"],
                   "backend": backend, "material": p["material"], "box_count": len(boxes)},
        "parameters": p, "template_key": obj["template_key"],
        "template_version": "1.0.0", "canonical_kind": obj["primitive_type"], "warnings": [],
    }
    return obj


def build_primitives() -> list[dict]:
    m = CONFIG["map"]
    objects = [flat("Ground", 0, 0, m["width"], m["depth"], COLOURS["ground"])]

    # Roads sit just above the ground so they're always visible on top of it.
    for r in CONFIG.get("roads", []):
        objects.append(flat(r["name"], r["x"], r["y"], r["w"], r["d"], COLOURS["road"], z=0.02))

    for b in CONFIG.get("buildings", []):
        colour = COLOURS[b["kind"]]
        if b["kind"] == "park":
            objects.append(flat(b["name"], b["x"], b["y"], b["w"], b["d"], colour, z=0.02))
        elif b["kind"] == "site":
            objects.append(walls(b["name"], b["x"], b["y"], b["w"], b["d"], b["h"], colour))
        else:
            objects.append(box(b["name"], b["x"], b["y"], b["w"], b["d"], b["h"], colour))

    # The pollution sources get a visible pile, so you can see what the robot found.
    piles = {"garbage_burning": ("Garbage pile", 2.0, 0.8), "construction_dust": ("Sand pile", 3.0, 1.2)}
    for s in CONFIG.get("sources", []):
        label, size, height = piles[s["type"]]
        objects.append(box(label, s["x"], s["y"], size, size, height, COLOURS[s["type"]]))

    return objects


def main() -> None:
    env_id = CONFIG["cyberwave"]["environment"]
    cw = Cyberwave(environment_id=env_id)

    env = cw.environments.get(env_id)
    settings = dict(env.settings or {})
    settings["procedural_primitives"] = [add_visual(o) for o in build_primitives()]

    cw.environments.api.src_app_api_environments_update_environment(
        env_id,
        EnvironmentCreateSchema(name=env.name, description=env.description, settings=settings),
    )
    print(f"Scene updated: {len(settings['procedural_primitives'])} objects on a "
          f"{CONFIG['map']['width']:.0f} x {CONFIG['map']['depth']:.0f} m map.")


if __name__ == "__main__":
    main()
