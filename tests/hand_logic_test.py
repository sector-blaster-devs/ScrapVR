"""Checks for head_tracker.Hands._place / place_joints / hand_shape: a synthetic hand at
known poses (camera-aligned like MediaPipe's world landmarks: x right, y down, z away),
plus two real frames from the headset camera."""
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker"))
import head_tracker as h

# Optional: a folder with your own headset-camera frames f1.png / f2.png (hands in view).
FRAMES = os.environ.get("SCRAPVR_FRAMES", "")
k, dist, calibrated = h.load_intrinsics(h.DEFAULT_CALIBRATION, 1280, 720, 70.0)
print("calibrated lens:", calibrated)


def hand(fingers_open: bool) -> np.ndarray:
    """21 joints in metres (MediaPipe order); palm in the x-y plane facing the camera,
    fingers towards -y, curled fingers come towards the camera (-z)."""
    pts = np.zeros((21, 3))
    mcp_x = {1: 0.025, 5: 0.02, 9: 0.0, 13: -0.018, 17: -0.034}
    mcp_y = {1: -0.03, 5: -0.085, 9: -0.09, 13: -0.085, 17: -0.075}
    lengths = {5: 0.075, 9: 0.085, 13: 0.08, 17: 0.065}
    pts[1] = [mcp_x[1], mcp_y[1], -0.01]
    for base in (5, 9, 13, 17):
        x, y = mcp_x[base], mcp_y[base]
        pts[base] = [x, y, 0.0]
        for j in (1, 2, 3):
            f = j / 3.0
            if fingers_open:
                pts[base + j] = [x, y - lengths[base] * f, -0.005 * j]
            else:
                angle = np.pi * f * 1.1
                r = lengths[base] / np.pi
                pts[base + j] = [x, y - r * np.sin(angle), -r * (1 - np.cos(angle))]
    thumb_dir = np.array([0.6, -0.8, 0.0]) if fingers_open else np.array([-0.3, -0.9, -0.3])
    for j in (2, 3, 4):
        pts[j] = pts[1] + thumb_dir / np.linalg.norm(thumb_dir) * 0.025 * (j - 1)
    return pts - pts[list(h.PALM)].mean(axis=0)


def project(points_cam):
    p, _ = cv2.projectPoints(points_cam, np.zeros(3), np.zeros(3), k, dist)
    return p.reshape(-1, 2)


ok = True


def check(label, condition):
    global ok
    ok = ok and bool(condition)
    print(("PASS " if condition else "FAIL ") + label)


hands = h.Hands(k, dist, dict(h.DEFAULT_GESTURES))
open_hand, fist = hand(True), hand(False)
print("shape open:", np.round(h.hand_shape(open_hand), 2), " fist:", np.round(h.hand_shape(fist), 2))
now = time.time()

t = np.array([0.05, 0.10, 0.40])
r = hands._place({"side": "right", "local": open_hand, "pixels": project(open_hand + t)}, now)
check("open hand at 0.40 m accepted", isinstance(r, dict))
centre = r["points"][list(h.PALM)].mean(axis=0) if isinstance(r, dict) else np.zeros(3)
check(f"palm centre recovered within 1.5 cm ({np.linalg.norm(centre - t) * 100:.1f} cm)",
      isinstance(r, dict) and np.linalg.norm(centre - t) < 0.015)
check("open hand: no grab, no pinch", isinstance(r, dict) and not r["grab"] and not r["pinch"])

r = hands._place({"side": "right", "local": fist, "pixels": project(fist + t + 0.01)}, now + 0.07)
check("fist: grab", isinstance(r, dict) and r["grab"])

deeper = {"side": "right", "local": open_hand, "pixels": project(open_hand + t + np.array([0.0, 0.0, 0.3]))}
r = hands._place(deeper, now + 0.14)
held_distance = np.linalg.norm(r["points"][list(h.PALM)].mean(axis=0)) if isinstance(r, dict) else -1.0
check(f"30 cm depth glitch: hand kept, distance held ({held_distance:.2f} m, was ~0.42)",
      isinstance(r, dict) and abs(held_distance - 0.42) < 0.05)
sideways = {"side": "right", "local": open_hand, "pixels": project(open_hand + t + np.array([0.4, 0.0, 0.0]))}
check("40 cm sideways leap in 70 ms rejected", hands._place(sideways, now + 0.21) == "jump")
check("same place after a pause accepted (fresh hand)", isinstance(hands._place(deeper, now + 1.0), dict))

r = hands._place({"side": "left", "local": open_hand, "pixels": project(open_hand + np.array([0.0, 0.0, 1.5]))}, now)
check("hand at 1.5 m rejected (out of reach)", isinstance(r, str) and r.startswith("range"))

tilt = cv2.Rodrigues(np.array([0.6, 0.3, 0.0]))[0]
for i, at in enumerate((np.array([0.0, 0.05, 0.30]), np.array([-0.08, 0.12, 0.55]))):
    seen = open_hand @ tilt.T + at
    r = hands._place({"side": "left", "local": open_hand, "pixels": project(seen)}, now + 10.0 * (i + 1))
    got = np.linalg.norm(r["points"][list(h.PALM)].mean(axis=0)) if isinstance(r, dict) else -1.0
    want = np.linalg.norm(seen[list(h.PALM)].mean(axis=0))
    check(f"tilted hand at {want:.2f} m placed at {got:.2f} m (within 15%)", abs(got - want) < 0.15 * want)

# Real frames from the headset camera; the old solvePnP put both at 13-16 cm.
for name in ("f1", "f2") if FRAMES else ():
    img = cv2.imread(os.path.join(FRAMES, f"{name}.png"))
    if img is None:
        print(f"skip real frame {name}: not found")
        continue
    live = h.Hands(k, dist, dict(h.DEFAULT_GESTURES))
    found = []
    for _ in range(3):
        found = live.landmarks(img)
    for hand_found in found:
        joints = live.place_joints(hand_found["local"], hand_found["pixels"])
        px = hand_found["pixels"]
        inside = (px[:, 0] >= 0) & (px[:, 0] < 1280) & (px[:, 1] >= 0) & (px[:, 1] < 720)
        back = project(joints)
        error = float(np.mean(np.linalg.norm(back[inside] - px[inside], axis=1)))
        distance = float(np.linalg.norm(joints[list(h.PALM)].mean(axis=0)))
        check(f"real frame {name}: hand at {distance:.2f} m, {int(inside.sum())} joints in view on the picture "
              f"({error:.2f} px)", h.HAND_RANGE_M[0] <= distance <= 0.70 and error < 1.0)

print("HAND LOGIC", "PASS" if ok else "FAIL")
