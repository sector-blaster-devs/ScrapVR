"""ScrapVR tracker: head position and hands from a webcam mounted on the headset.

The webcam watches an ArUco marker (make_markers.py; on paper or a tablet screen) in front
of you and sends where the marker is, in the camera's own axes, to the receiver (the PC
program rendering the VR view) as JSON on UDP 16061, next to the phone's gyro packets.
docs/PROTOCOL.md has every message.

  {"type": "scrapvr_head_pos", "tx": .., "ty": .., "tz": .., "tracked": true, "tilt": 0.0}

("tilt", in every packet, is --tilt-down: how far the camera mount points down from
your eye line, in degrees. The receiver applies it, so re-aiming needs only a restart.)

tx = right, ty = down, tz = forward from the lens, metres. Only the marker's POSITION is
used: a single flat marker seen nearly head-on has an ambiguous tilt (the two IPPE
solutions), so its rotation is unreliable while its position is not. The receiver
combines this with the phone gyro's rotation, then subtracts the lens-to-eyes offset, to
get where your eyes are (eye_in_room() below is the same maths, for the self-test).
While the marker is out of view it sends "tracked": false; hold the last position.

Hands, from the same frames (MediaPipe HandLandmarker, models/hand_landmarker.task):

  {"type": "scrapvr_hands", "hands": [{"side": "right", "pinch": false, "grab": false,
                                      "p": [x0, y0, z0, ... x20, y20, z20]}]}

21 MediaPipe joints per hand in the same camera axes, metres; an empty list = no hands.

  python head_tracker.py --calibrate         # once: the ChArUco board, saves camera_lenovo510.json
  python head_tracker.py --marker-mm 150     # the measured edge of the printed marker
  python head_tracker.py --selftest          # no camera: checks the maths on a rendered marker
  python head_tracker.py --calibrate-hands   # once per person: open / fist / pinch -> hands_calibration.json

Preview window: r = recenter its own readout (which assumes the head is not turning;
the receiver's has the gyro), q = quit. Install with `pip install -r requirements.txt`
in a venv, and fetch the hand model once with `python tools/download_models.py`.
"""
import argparse
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import threading
import time

import cv2
import numpy as np

from make_markers import MARKER_DICT, MARKER_ID, make_board

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CALIBRATION = os.path.join(HERE, "camera_lenovo510.json")
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
LOST_AFTER_S = 0.3        # no marker for this long = tracking lost
MAX_JUMP_M = 0.3          # a bigger move between two frames (33 ms) is a bad detection
SMOOTHING = 0.5           # exponential smoothing of the marker position, 0 = none
CALIBRATION_VIEWS = 25

def camera_to_head(tilt_down_deg: float) -> np.ndarray:
    """Camera axes (x right, y down, z forward) in head axes (x right, y up, z back),
    pitched down by the mount. --tilt-down goes to the receiver in every packet."""
    t = math.radians(-tilt_down_deg)
    pitch = np.array([[1.0, 0.0, 0.0], [0.0, math.cos(t), -math.sin(t)], [0.0, math.sin(t), math.cos(t)]])
    return pitch @ np.diag([1.0, -1.0, -1.0])


CAMERA_TO_HEAD = camera_to_head(0.0)   # the self-test's straight mount
# The eyes relative to the lens, in head axes; the receiver must use the same.
EYES_FROM_LENS = np.array([0.0, -0.06, 0.08])


# ── Camera ────────────────────────────────────────────────────────────────────

class Camera:
    """The webcam through ffmpeg's DirectShow input with the MJPG format forced. OpenCV 5's
    DirectShow backend ignores CAP_PROP_FOURCC and falls back to YUY2, which this camera
    only does at 7.5 fps in 720p (and its MSMF backend hangs on open); MJPG gives the full
    30. BGR frames (the hand model needs colour), and only the newest one, so a slow
    moment never builds up latency."""

    def __init__(self, name: str, width: int, height: int):
        self.width, self.height = width, height
        self._proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-vcodec", "mjpeg",
             "-video_size", f"{width}x{height}", "-framerate", "30", "-rtbufsize", "64M",
             "-i", f"video={name}", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self._latest = None
        self._seq = 0
        self._taken = 0
        self._closed = False
        self._cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        size = self.width * self.height * 3
        while True:
            buf = bytearray()
            while len(buf) < size:
                chunk = self._proc.stdout.read(size - len(buf))
                if not chunk:
                    with self._cond:
                        self._closed = True
                        self._cond.notify_all()
                    return
                buf += chunk
            frame = np.frombuffer(bytes(buf), np.uint8).reshape(self.height, self.width, 3)
            with self._cond:
                self._latest = frame
                self._seq += 1
                self._cond.notify_all()

    def read(self, timeout: float = 2.0):
        """The newest BGR frame not returned before, or None (no frame in time / closed)."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != self._taken or self._closed, timeout)
            if self._seq == self._taken:
                return None
            self._taken = self._seq
            return self._latest

    def failed(self) -> str:
        """ffmpeg's error once it has exited (wrong name, camera busy), else ''."""
        if self._proc.poll() is None:
            return ""
        return self._proc.stderr.read().decode("utf-8", "replace").strip() or f"ffmpeg exited {self._proc.returncode}"

    def release(self) -> None:
        self._proc.kill()


def load_intrinsics(path: str, width: int, height: int, hfov_deg: float):
    """(K, dist, calibrated). Without a calibration for this resolution, a pinhole guess
    from the horizontal FOV: sideways/up moves stay about right, distance does not."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("width") == width and data.get("height") == height:
            return np.array(data["K"], dtype=np.float64), np.array(data["dist"], dtype=np.float64), True
    focal = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    k = np.array([[focal, 0, width / 2.0], [0, focal, height / 2.0], [0, 0, 1]], dtype=np.float64)
    return k, np.zeros(5), False


# ── Geometry ──────────────────────────────────────────────────────────────────

def marker_object_points(marker_m: float) -> np.ndarray:
    """Marker frame = room frame: x right, y up, z out of the wall towards you."""
    s = marker_m / 2.0
    return np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], dtype=np.float64)


UNDISTORT_CRITERIA = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-12)


def undistort_rays(pixels: np.ndarray, k: np.ndarray, dist: np.ndarray) -> np.ndarray:
    """Pixels -> normalised image coordinates (x/z, y/z). OpenCV's default (5 iterations,
    also used inside solvePnP) does not converge for this wide lens (k3 = 1.44): up to
    60 px off near the frame edge. 200 iterations round-trips the whole frame to 0.00 px."""
    return cv2.undistortPoints(pixels.reshape(-1, 1, 2).astype(np.float64), k, dist,
                               None, None, None, UNDISTORT_CRITERIA).reshape(-1, 2)


def marker_in_camera(corners: np.ndarray, k: np.ndarray, dist: np.ndarray, marker_m: float):
    """The marker centre in camera axes (metres), or None. The corners are undistorted
    here (see undistort_rays) and solved as an ideal pinhole camera."""
    rays = undistort_rays(corners.reshape(4, 2), k, dist)
    ok, _rvec, tvec = cv2.solvePnP(marker_object_points(marker_m), rays, np.eye(3), None,
                                   flags=cv2.SOLVEPNP_IPPE_SQUARE)
    return tvec.reshape(3) if ok else None


def head_rotation(yaw: float, pitch: float) -> np.ndarray:
    """Head axes in the room: yaw = clockwise turn from facing the marker, pitch = up.
    Godot: Basis(Vector3.UP, -yaw) * Basis(Vector3.RIGHT, pitch)."""
    cy, sy = math.cos(-yaw), math.sin(-yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    return ry @ rx


def eye_in_room(marker_cam: np.ndarray, yaw: float, pitch: float, cam_to_head: np.ndarray = CAMERA_TO_HEAD) -> np.ndarray:
    """Where the eyes are, relative to the marker. The camera sits at -R*t from the
    marker; the eyes are EYES_FROM_LENS from the camera, turned with the head."""
    head = head_rotation(yaw, pitch)
    return -(head @ cam_to_head @ marker_cam) + head @ EYES_FROM_LENS


# ── Hands ─────────────────────────────────────────────────────────────────────

HAND_MODEL = os.path.join(HERE, "models", "hand_landmarker.task")
HAND_CALIBRATION = os.path.join(HERE, "hands_calibration.json")
HAND_HZ = 20.0                 # hand inference rate; the marker still runs on every frame
DEPTH_SMOOTHING = 0.7          # the palm-size distance is the noisy part: smoothed harder than x/y
PALM_LENGTH_M = 0.09           # wrist to middle knuckle of an adult hand: sets the hand's scale
HAND_RANGE_M = (0.10, 0.90)    # wrist distance from the lens a real hand can have
HAND_MAX_JUMP_M = 0.25         # between two updates (~70 ms) a hand moves less than this
HAND_TRACK_GAP_S = 0.3         # unseen longer than this = a fresh hand, no jump check
FINGERTIPS = (8, 12, 16, 20)
PALM = (0, 5, 9, 13, 17)
# Gestures are ratios to the hand's own palm length (wrist to middle knuckle), so they do
# not depend on MediaPipe's assumed hand size: spread = mean fingertip distance from the
# palm centre, gap = thumb tip to index tip. Defaults until --calibrate-hands has run.
DEFAULT_GESTURES = {"grab_on": 0.70, "grab_off": 0.85, "pinch_on": 0.25, "pinch_off": 0.40}


def hand_shape(local: np.ndarray):
    """(spread, gap) as ratios to the palm length; see DEFAULT_GESTURES."""
    palm_length = max(float(np.linalg.norm(local[9] - local[0])), 1e-6)
    centre = local[list(PALM)].mean(axis=0)
    spread = float(np.mean([np.linalg.norm(local[t] - centre) for t in FINGERTIPS]))
    gap = float(np.linalg.norm(local[4] - local[8]))
    return spread / palm_length, gap / palm_length


def load_gestures(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {key: float(value) for key, value in json.load(f)["thresholds"].items()}
    return dict(DEFAULT_GESTURES)


class Hands:
    """MediaPipe's hand landmarker. Its world landmarks are a good hand SHAPE but not a
    physical scale: solvePnP between them and the picture fitted a hand on the touchpad
    40 px+ off, at 13 cm instead of ~45. So the distance comes from how big the palm looks
    through the calibrated lens (PALM_LENGTH_M), and each joint sits on its own pixel's
    ray at that distance plus MediaPipe's relative depth: the joints always line up with
    the picture. A hand is accepted within arm's reach and when it has not jumped; its
    position is smoothed. Pinch and grab use the palm-relative shape, with hysteresis so
    they do not flicker."""

    def __init__(self, k: np.ndarray, dist: np.ndarray, gestures: dict):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self._mp = mp
        self._landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=HAND_MODEL),
            running_mode=vision.RunningMode.VIDEO, num_hands=2))
        self._k, self._dist = k, dist
        self._gestures = gestures
        self._tracks = {}          # side -> {"t", "time", "pinch", "grab"}
        self._start = time.time()
        self.rejected = {}         # reason -> count, reset by whoever reports it

    def landmarks(self, bgr: np.ndarray) -> list:
        """Raw detections with a unique side each: [{"side", "local", "pixels"}]."""
        height, width = bgr.shape[:2]
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        result = self._landmarker.detect_for_video(image, int((time.time() - self._start) * 1000))
        found = []
        for shape, picture, handedness in zip(result.hand_world_landmarks, result.hand_landmarks, result.handedness):
            # MediaPipe labels hands as if the picture were a mirrored selfie; this camera
            # looks out from the head, so its "Left" is your right hand.
            found.append({"side": "right" if handedness[0].category_name == "Left" else "left",
                          "local": np.array([[p.x, p.y, p.z] for p in shape], dtype=np.float64),
                          "pixels": np.array([[p.x * width, p.y * height] for p in picture], dtype=np.float64)})
        if len(found) == 2:
            # Two hands: an outward-facing head camera sees your left hand on the left. More
            # reliable than the label, and the two can never end up with the same side.
            found.sort(key=lambda hand: hand["pixels"][0][0])
            found[0]["side"], found[1]["side"] = "left", "right"
        return found

    def detect(self, bgr: np.ndarray) -> list:
        """One dict per accepted hand: side, points (21 x 3, camera axes, metres), pinch,
        grab, pixels, spread, gap. Rejections are counted in self.rejected by reason."""
        now = time.time()
        hands = []
        for hand in self.landmarks(bgr):
            placed = self._place(hand, now)
            if isinstance(placed, str):   # the reason, e.g. "range 0.06m"
                kind = placed.split()[0]
                count, _ = self.rejected.get(kind, (0, ""))
                self.rejected[kind] = (count + 1, placed)
            else:
                hands.append(placed)
        for side in [s for s, track in self._tracks.items() if now - track["time"] > HAND_TRACK_GAP_S]:
            del self._tracks[side]   # gone long enough: it comes back as a fresh hand
        return hands

    def _place(self, hand: dict, now: float):
        local, pixels, side = hand["local"], hand["pixels"], hand["side"]
        points = self.place_joints(local, pixels)
        if points is None:
            return "degenerate"
        wrist = float(np.linalg.norm(points[0]))
        if not HAND_RANGE_M[0] <= wrist <= HAND_RANGE_M[1]:
            return f"range {wrist:.2f}m"
        t = points[list(PALM)].mean(axis=0)
        track = self._tracks.get(side)
        fresh = track is None or now - track["time"] > HAND_TRACK_GAP_S
        if not fresh:
            # Every joint sits on its own ray, so scaling them all changes only the hand's
            # distance. A distance jump is the palm-size estimate glitching, not the hand
            # moving: hold the previous distance. Then smooth distance hard, x/y lightly.
            previous = float(np.linalg.norm(track["t"]))
            distance = float(np.linalg.norm(t))
            if abs(distance - previous) > HAND_MAX_JUMP_M:
                distance = previous
            distance = previous * DEPTH_SMOOTHING + distance * (1.0 - DEPTH_SMOOTHING)
            scale = distance / max(float(np.linalg.norm(t)), 1e-6)
            points = points * scale
            t = t * scale
            if np.linalg.norm(t - track["t"]) > HAND_MAX_JUMP_M:
                return "jump"   # a sideways leap this big is a misdetection
            smoothed = track["t"] * SMOOTHING + t * (1.0 - SMOOTHING)
            points = points + (smoothed - t)
            t = smoothed
        spread, gap = hand_shape(local)
        g = self._gestures
        was_grab = False if fresh else track["grab"]
        was_pinch = False if fresh else track["pinch"]
        grab = spread < (g["grab_off"] if was_grab else g["grab_on"])
        pinch = not grab and gap < (g["pinch_off"] if was_pinch else g["pinch_on"])
        self._tracks[side] = {"t": t, "time": now, "pinch": pinch, "grab": grab}
        return {"side": side, "points": points, "pinch": pinch, "grab": grab,
                "pixels": pixels, "spread": spread, "gap": gap}

    def place_joints(self, local: np.ndarray, pixels: np.ndarray):
        """21 joints in camera axes (metres), or None. Distance from the palm's apparent
        size: of its length (wrist to middle knuckle) and width (index to little knuckle),
        whichever looks larger is the less foreshortened, so it gives the nearer, truer
        distance. Each joint then sits on its own pixel's ray at that distance plus its
        relative depth from MediaPipe (world z grows away from the camera)."""
        model_length = float(np.linalg.norm(local[9] - local[0]))
        if model_length < 1e-6:
            return None
        real = local * (PALM_LENGTH_M / model_length)
        rays = undistort_rays(pixels, self._k, self._dist)
        seen_length = float(np.linalg.norm(rays[9] - rays[0]))
        seen_width = float(np.linalg.norm(rays[5] - rays[17]))
        if seen_length < 1e-4 and seen_width < 1e-4:
            return None
        candidates = []
        if seen_length >= 1e-4:
            candidates.append(PALM_LENGTH_M / seen_length)
        if seen_width >= 1e-4:
            candidates.append(float(np.linalg.norm(real[5] - real[17])) / seen_width)
        distance = min(candidates)
        depth = distance + (real[:, 2] - real[list(PALM), 2].mean())
        return np.column_stack([rays[:, 0] * depth, rays[:, 1] * depth, depth])


class HandWorker:
    """Runs Hands on the newest frame in its own thread, at most HAND_HZ, so hand
    inference never holds up the marker, which runs on every frame."""

    def __init__(self, hands: Hands, hz: float):
        self.hands = hands
        self._period = 1.0 / hz
        self._frame = None
        self._result = None
        self._seq = 0
        self._taken = 0
        self._cond = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, bgr: np.ndarray) -> None:
        with self._cond:
            self._frame = bgr
            self._cond.notify()

    def take(self):
        """The newest result not taken before, or None."""
        with self._cond:
            if self._seq == self._taken:
                return None
            self._taken = self._seq
            return self._result

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._frame is not None)
                frame, self._frame = self._frame, None
            started = time.time()
            result = self.hands.detect(frame)
            with self._cond:
                self._result = result
                self._seq += 1
            time.sleep(max(0.0, self._period - (time.time() - started)))


# ── Mount: the webcam relative to the eyes, measured ─────────────────────────

MOUNT_CALIBRATION = os.path.join(HERE, "mount_calibration.json")
PHONE_CAMERA_PORT = 16062
MOUNT_PAIRS = 12               # still pairs to collect
MOUNT_MIN_PAIRS = 8
MOUNT_MAX_SPREAD_DEG = 1.5     # the pairs must agree this well to be accepted
MOUNT_MAX_SPREAD_MM = 15.0
MOUNT_STILL_DEG, MOUNT_STILL_MM = 0.5, 5.0   # head still between two webcam views
# The S9 in its own portrait axes (x right, y up, z out of the screen). Its rear camera
# sits ~62 mm above the screen centre (Galaxy S9 spec drawing); the eyes are ~72 mm in
# front of the camera (8.5 mm phone + ~64 mm headset optics). The one assumed number
# left in the mount, about +-1.5 cm.
EYES_FROM_S9_CAMERA = np.array([0.0, -0.062, 0.072])


def phone_address():
    """The S9 is the USB tether's gateway: the default route on the RNDIS adapter."""
    script = ("$i = (Get-NetAdapter | Where-Object InterfaceDescription -like '*NDIS*' | Select-Object -First 1).ifIndex; "
              "if ($i) { (Get-NetRoute -InterfaceIndex $i -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue).NextHop }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[0] if lines else None


class PhoneCamera:
    """The S9 app's CameraServer over the tether (TCP 16062): its rear camera is the eye view."""

    def __init__(self, host: str):
        self._sock = socket.create_connection((host, PHONE_CAMERA_PORT), timeout=10)
        self._io = self._sock.makefile("rwb")

    def info(self) -> dict:
        self._io.write(b"INFO\n")
        self._io.flush()
        return json.loads(self._io.readline().decode("utf-8"))

    def snap(self):
        self._io.write(b"SNAP\n")
        self._io.flush()
        size = struct.unpack(">i", self._read(4))[0]
        if size <= 0:
            return None
        return cv2.imdecode(np.frombuffer(self._read(size), np.uint8), cv2.IMREAD_COLOR)

    def _read(self, n: int) -> bytes:
        data = self._io.read(n)
        if len(data) != n:
            raise ConnectionError("S9 camera connection closed")
        return data

    def close(self) -> None:
        self._sock.close()


def phone_intrinsics(info: dict):
    """K for the S9's JPEGs: the lens calibration Android reports when it has one, else the
    focal length over the sensor width. (K, dist, from_lens_calibration)."""
    width, height = info["width"], info["height"]
    left, top, right, bottom = info["active"]
    sx, sy = width / (right - left), height / (bottom - top)
    if info.get("intrinsics"):
        fx, fy, cx, cy = info["intrinsics"][:4]
        k = [[fx * sx, 0.0, cx * sx], [0.0, fy * sy, cy * sy], [0.0, 0.0, 1.0]]
    else:
        f = info["focal_mm"] / info["sensor_mm"][0] * info["pixel_array"][0]
        k = [[f * sx, 0.0, width / 2.0], [0.0, f * sy, height / 2.0], [0.0, 0.0, 1.0]]
    return np.array(k), np.zeros(5), bool(info.get("intrinsics"))


def _rot_z(degrees: float) -> np.ndarray:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def device_from_rear_camera(sensor_orientation: int) -> np.ndarray:
    """Columns = the rear camera's axes (x right, y down, z forward in its raw image) in
    the phone's portrait axes. Unrotated, image right = phone right, image down = phone
    down, the lens looks out of the back; Android's SENSOR_ORIENTATION (clockwise turn the
    image needs to be upright) turns that about the lens axis."""
    upright = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    return _rot_z(-sensor_orientation) @ upright


def head_from_device(display_rotation: int) -> np.ndarray:
    """Phone portrait axes -> head axes (x right, y up, z back). Android ROTATION_90 =
    the phone turned 90 deg counter-clockwise, so its top points to your left."""
    return _rot_z(90.0 * display_rotation)


def board_pose(bgr: np.ndarray, k: np.ndarray, dist: np.ndarray, board, detector):
    """(R, t, corners) of the ChArUco board in this camera, or None. Many points over the
    board, so unlike a single marker its tilt is not ambiguous."""
    corners, ids, _, _ = detector.detectBoard(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    if ids is None or len(ids) < 10:
        return None
    obj, img = board.matchImagePoints(corners.reshape(-1, 1, 2), ids.reshape(-1, 1))
    if obj is None or len(obj) < 10:
        return None
    rays = undistort_rays(img.reshape(-1, 2), k, dist)
    points = obj.reshape(-1, 3).astype(np.float64)
    ok, rvec, tvec = cv2.solvePnP(points, rays, np.eye(3), None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    # IPPE's closed form, then polished against every corner (Levenberg-Marquardt).
    rvec, tvec = cv2.solvePnPRefineLM(points, rays, np.eye(3), None, rvec, tvec)
    return cv2.Rodrigues(rvec)[0], tvec.reshape(3), len(obj)


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    return math.degrees(np.linalg.norm(cv2.Rodrigues(a @ b.T)[0]))


def load_mount(path: str):
    """(camera_to_head 3x3, eyes_from_lens 3) from --calibrate-mount, or None."""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return np.array(data["camera_to_head"]), np.array(data["eyes_from_lens"])
    return None


def calibrate_mount(args) -> None:
    """The S9's rear camera looks where the eyes look. Both cameras photograph the board
    on the iPad at the same moment while the headset is still; the board's pose in each
    gives the webcam's rotation and position relative to the eye view."""
    host = args.phone_ip or phone_address()
    if not host:
        raise SystemExit("No USB tether to the S9 (no NDIS adapter with a default route). Pass --phone-ip.")
    # Android 11+ only gives the camera to the app on screen: bring the S9 app to the front
    # (the headset sits still for this, Moonlight is not needed). Best effort over adb.
    try:
        subprocess.run(["adb", "shell", "am", "start", "-n", "studio.roundup.scrapvr/.MainActivity"],
                       capture_output=True, timeout=15)
        time.sleep(2.0)
    except (OSError, subprocess.TimeoutExpired):
        print("adb not available - make sure the ScrapVR Pose app is on the phone's screen.")
    phone = PhoneCamera(host)
    info = phone.info()
    if "error" in info:
        raise SystemExit(f"S9 camera: {info['error']}")
    k_phone, dist_phone, from_lens_data = phone_intrinsics(info)
    print(f"S9 camera {info['width']}x{info['height']}, f {k_phone[0, 0]:.0f} px "
          f"({'lens calibration' if from_lens_data else 'focal length / sensor size'}), "
          f"sensor orientation {info['sensor_orientation']}, display rotation {info['display_rotation']}")
    k_web, dist_web, calibrated = load_intrinsics(args.calibration, args.width, args.height, args.hfov)
    if not calibrated:
        raise SystemExit("Calibrate the webcam first (--calibrate).")
    cap = Camera(args.camera_name, args.width, args.height)
    board = make_board(args.board_square_mm)
    detector = cv2.aruco.CharucoDetector(board)
    pairs = []
    previous = None
    give_up = time.time() + 90.0
    print("Keep the headset still, both cameras facing the chessboard on the iPad.")
    while len(pairs) < MOUNT_PAIRS and time.time() < give_up:
        s9_frame = phone.snap()
        web_frame = cap.read()
        if s9_frame is None or web_frame is None:
            continue
        if args.debug_dir:   # the latest pair, to see what each camera sees
            cv2.imwrite(os.path.join(args.debug_dir, "mount_s9.jpg"), s9_frame)
            cv2.imwrite(os.path.join(args.debug_dir, "mount_webcam.jpg"), web_frame)
        web = board_pose(web_frame, k_web, dist_web, board, detector)
        s9 = board_pose(s9_frame, k_phone, dist_phone, board, detector)
        print(f"  board seen by webcam: {'yes' if web else 'no'}, by S9: {'yes' if s9 else 'no'}"
              + (f" | pairs {len(pairs)}/{MOUNT_PAIRS}" if web and s9 else ""))
        if web is None or s9 is None:
            previous = None
            continue
        still = previous is not None and _angle_deg(web[0], previous[0]) < MOUNT_STILL_DEG \
            and np.linalg.norm(web[1] - previous[1]) * 1000.0 < MOUNT_STILL_MM
        previous = web
        if still:
            relative = s9[0] @ web[0].T                       # webcam axes -> S9 camera axes
            pairs.append((relative, s9[1] - relative @ web[1], s9[0]))
    phone.close()
    cap.release()
    if len(pairs) < MOUNT_MIN_PAIRS:
        raise SystemExit(f"Only {len(pairs)} still pairs with the board in both views - nothing saved.")
    rotation = info["display_rotation"]
    if rotation in (0, 2):
        # The phone reports the rotation of whatever app is in front; in the headset it is
        # landscape. Of the two landscape ways, the right one makes the upright board's
        # "down" point down (board y runs down the board, see the synthetic mount test).
        rotation = min((1, 3), key=lambda r: solve_mount(pairs, r, info["sensor_orientation"])["board_down"][1])
        print(f"Phone reported display rotation {info['display_rotation']} (not landscape); "
              f"the board says the headset holds it as rotation {rotation}.")
    mount = solve_mount(pairs, rotation, info["sensor_orientation"])
    print(f"{len(pairs)} pairs agree within {mount['spread_deg']:.2f} deg / {mount['spread_mm']:.1f} mm")
    if mount["spread_deg"] > MOUNT_MAX_SPREAD_DEG or mount["spread_mm"] > MOUNT_MAX_SPREAD_MM:
        raise SystemExit("The pairs disagree too much (headset moved?) - nothing saved.")
    eyes = mount["eyes_from_lens"]
    print(f"Webcam: tilted {mount['tilt_down_deg']:.1f} deg down, turned {mount['turn_deg']:+.1f} deg sideways; "
          f"eyes from its lens {np.round(eyes * 100, 1)} cm (x right, y up, z back), {np.linalg.norm(eyes) * 100:.1f} cm")
    print(f"Check: the board's 'down' in head axes is {np.round(mount['board_down'], 2)} - with the head and iPad "
          "upright its y should be near -1 (or +1 if the board axis runs up); x near 0.")
    with open(args.mount_calibration, "w", encoding="utf-8") as f:
        json.dump({"camera_to_head": mount["camera_to_head"].round(6).tolist(), "eyes_from_lens": eyes.round(4).tolist(),
                   "tilt_down_deg": round(mount["tilt_down_deg"], 2), "turn_deg": round(mount["turn_deg"], 2),
                   "pairs": len(pairs), "spread_deg": round(mount["spread_deg"], 3),
                   "spread_mm": round(mount["spread_mm"], 1), "board_square_mm": args.board_square_mm,
                   "display_rotation_used": rotation, "s9": info},
                  f, indent=2)
    print(f"Saved {args.mount_calibration}")


def solve_mount(pairs: list, display_rotation: int, sensor_orientation: int) -> dict:
    """From (webcam->S9 rotation, webcam lens in S9 camera axes, board rotation in the S9
    camera) pairs: the webcam's axes in head axes, the eyes from its lens, and checks."""
    u, _, vt = np.linalg.svd(sum(p[0] for p in pairs))     # the mean rotation
    rotation = u @ vt
    offset = np.median(np.array([p[1] for p in pairs]), axis=0)
    head_from_s9 = head_from_device(display_rotation) @ device_from_rear_camera(sensor_orientation)
    camera_to_head = head_from_s9 @ rotation
    eyes_from_lens = head_from_device(display_rotation) @ EYES_FROM_S9_CAMERA - head_from_s9 @ offset
    forward = camera_to_head @ np.array([0.0, 0.0, 1.0])
    return {"camera_to_head": camera_to_head, "eyes_from_lens": eyes_from_lens,
            "tilt_down_deg": math.degrees(math.atan2(-forward[1], -forward[2])),
            "turn_deg": math.degrees(math.atan2(forward[0], -forward[2])),
            "spread_deg": max(_angle_deg(p[0], rotation) for p in pairs),
            "spread_mm": max(float(np.linalg.norm(p[1] - offset)) for p in pairs) * 1000.0,
            "board_down": head_from_s9 @ pairs[-1][2][:, 1]}


# ── Modes ─────────────────────────────────────────────────────────────────────

def track(args) -> None:
    cap = Camera(args.camera_name, args.width, args.height)
    k, dist, calibrated = load_intrinsics(args.calibration, args.width, args.height, args.hfov)
    if not calibrated:
        print(f"No calibration for {args.width}x{args.height}: using a {args.hfov} deg FOV guess. "
              "Run --calibrate for true distances.")
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(MARKER_DICT), _detector_parameters())
    marker_m = args.marker_mm / 1000.0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    # How the webcam sits relative to the eyes, sent with every packet: measured by
    # --calibrate-mount when there is a profile ("cam" + "eyes"), else just --tilt-down.
    mount = {"tilt": args.tilt_down}
    cam_to_head = camera_to_head(args.tilt_down)
    measured = load_mount(args.mount_calibration)
    if measured is not None:
        cam_to_head = measured[0]
        mount.update({"cam": [round(float(v), 6) for v in measured[0].reshape(-1)],
                      "eyes": [round(float(v), 4) for v in measured[1]]})
    print("Mount:", "measured (" + args.mount_calibration + ")" if measured is not None else f"--tilt-down {args.tilt_down}")
    hand_worker = None
    if not args.no_hands:
        gestures = load_gestures(args.hand_calibration)
        print("Hand gestures:", gestures, "(calibrated)" if os.path.exists(args.hand_calibration) else "(defaults)")
        hand_worker = HandWorker(Hands(k, dist, gestures), HAND_HZ)

    smoothed = None
    origin = None
    last_seen = 0.0
    frames = 0
    rejected = 0
    hands = []
    hand_updates = 0
    report_at = time.time() + 1.0
    next_snapshot = 0.0
    print(f"Tracking marker id {MARKER_ID} ({args.marker_mm:.1f} mm) -> udp://{args.host}:{args.port}. r = recenter, q = quit.")
    while True:
        bgr = cap.read()
        if bgr is None:
            error = cap.failed()
            if error:
                # A USB hiccup drops the camera for a moment: report lost and reopen it,
                # so nobody in the headset has to restart anything.
                print(f"Camera dropped ({error.splitlines()[-1]}); reconnecting.")
                _send(sock, target, np.zeros(3), False, mount)
                cap.release()
                time.sleep(1.0)
                cap = Camera(args.camera_name, args.width, args.height)
                last_seen = 0.0
            continue
        frames += 1
        now = time.time()
        if args.snapshot and now >= next_snapshot:
            cv2.imwrite(args.snapshot, bgr)   # what the camera sees, for whoever is not wearing it
            next_snapshot = now + 2.0
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        frame = None if args.no_preview else bgr.copy()
        if hand_worker is not None:
            hand_worker.submit(bgr)
            new_hands = hand_worker.take()
            if new_hands is not None:
                hands = new_hands
                hand_updates += 1
                _send_hands(sock, target, hands, mount)
            if frame is not None:
                for hand in hands:
                    color = (0, 140, 255) if hand["grab"] else (60, 220, 60) if hand["pinch"] else (255, 220, 120)
                    for x, y in hand["pixels"]:
                        cv2.circle(frame, (int(x), int(y)), 4, color, -1)
        corners, ids, _ = detector.detectMarkers(gray)
        found = None
        if ids is not None and MARKER_ID in ids.flatten():
            hit = corners[list(ids.flatten()).index(MARKER_ID)]
            found = marker_in_camera(hit, k, dist, marker_m)
            if found is not None and smoothed is not None and now - last_seen < LOST_AFTER_S \
                    and np.linalg.norm(found - smoothed) > MAX_JUMP_M:
                rejected += 1
                found = None
            if not args.no_preview:
                cv2.aruco.drawDetectedMarkers(frame, [hit])

        if found is not None:
            smoothed = found if smoothed is None or now - last_seen > LOST_AFTER_S \
                else smoothed * SMOOTHING + found * (1.0 - SMOOTHING)
            last_seen = now
            _send(sock, target, smoothed, True, mount)
        elif now - last_seen > LOST_AFTER_S:
            _send(sock, target, np.zeros(3), False, mount)

        tracked = now - last_seen <= LOST_AFTER_S
        readout = "LOST - marker not in view"
        if tracked:
            eye = eye_in_room(smoothed, 0.0, 0.0, cam_to_head)
            if origin is None:
                origin = eye
            rel = (eye - origin) * 100.0
            readout = f"x {rel[0]:+6.1f}  y {rel[1]:+6.1f}  z {rel[2]:+6.1f} cm   marker {np.linalg.norm(smoothed):.2f} m"
        if now >= report_at:
            hand_text = ""
            if hand_worker is not None:
                described = ", ".join(f"{h['side']} {np.linalg.norm(h['points'][0]):.2f} m"
                                      + (" GRAB" if h["grab"] else " PINCH" if h["pinch"] else "")
                                      + f" (spread {h['spread']:.2f}, gap {h['gap']:.2f})" for h in hands)
                reasons = dict(hand_worker.hands.rejected)
                hand_worker.hands.rejected = {}
                rejected_text = ", ".join(f"{count}x {kind} (last: {last})" for kind, (count, last) in reasons.items())
                hand_text = (f" | hands {hand_updates} Hz, rejected {sum(c for c, _ in reasons.values())}"
                             + (f" [{rejected_text}]" if rejected_text else "") + f": {described or 'none'}")
                hand_updates = 0
            print(f"{readout} | {frames} fps | rejected {rejected}{hand_text}")
            frames = 0
            rejected = 0
            report_at = now + 1.0

        if not args.no_preview:
            color = (60, 220, 60) if tracked else (40, 40, 230)
            cv2.putText(frame, readout, (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
            cv2.putText(frame, "r = recenter   q = quit   (head still: no gyro here)"
                        + ("" if calibrated else "   UNCALIBRATED"),
                        (16, args.height - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 230, 230), 1)
            cv2.imshow("ScrapVR tracker", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("r"):
                origin = None
    cap.release()
    cv2.destroyAllWindows()


def measure(args) -> None:
    """Headset still on a table, marker in view: how steady is the measured position?
    Waits up to a minute for the marker, then records args.measure seconds."""
    cap = Camera(args.camera_name, args.width, args.height)
    k, dist, calibrated = load_intrinsics(args.calibration, args.width, args.height, args.hfov)
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(MARKER_DICT), _detector_parameters())
    marker_m = args.marker_mm / 1000.0
    samples, distances = [], []
    frames = 0
    start = None
    give_up = time.time() + 60.0
    print("Waiting for the marker...")
    while True:
        bgr = cap.read()
        if bgr is None:
            error = cap.failed()
            if error:
                raise SystemExit(f"Camera failed: {error}")
            continue
        now = time.time()
        corners, ids, _ = detector.detectMarkers(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        found = None
        if ids is not None and MARKER_ID in ids.flatten():
            found = marker_in_camera(corners[list(ids.flatten()).index(MARKER_ID)], k, dist, marker_m)
        if start is None:
            if found is None:
                if now > give_up:
                    raise SystemExit("The marker never came into view.")
                continue
            start = now
            print(f"Marker seen, measuring {args.measure:.0f} s...")
        frames += 1
        if found is not None:
            samples.append(eye_in_room(found, 0.0, 0.0))
            distances.append(float(np.linalg.norm(found)))
        if now - start >= args.measure:
            break
    cap.release()
    points = np.array(samples) * 1000.0
    smoothed = [points[0]]
    for p in points[1:]:
        smoothed.append(smoothed[-1] * SMOOTHING + p * (1.0 - SMOOTHING))
    smoothed = np.array(smoothed)
    print(f"Frames {frames} ({frames / args.measure:.1f} fps), marker found in {len(samples)} "
          f"({100.0 * len(samples) / max(1, frames):.0f}%), distance {np.mean(distances):.3f} m"
          + ("" if calibrated else " (UNCALIBRATED)"))
    for name, column in (("x", 0), ("y", 1), ("z", 2)):
        print(f"  {name}: jitter {points[:, column].std():.1f} mm raw / {smoothed[:, column].std():.1f} mm smoothed, "
              f"peak-to-peak {np.ptp(points[:, column]):.1f} mm")


def calibrate_hands(args) -> None:
    """Records your open hand, fist and pinch (one hand in view, 4 s each after a 3 s
    get-ready) and saves gesture thresholds between them to args.hand_calibration."""
    cap = Camera(args.camera_name, args.width, args.height)
    k, dist, _ = load_intrinsics(args.calibration, args.width, args.height, args.hfov)
    hands = Hands(k, dist, DEFAULT_GESTURES)
    phases = [("open", "OPEN hand, fingers spread"), ("fist", "FIST"), ("pinch", "PINCH thumb and index together")]
    samples = {}
    for name, prompt in phases:
        print(f"{prompt}: hold one hand in front of the camera. Recording in 3 s, for 4 s.")
        values = []
        started = time.time()
        while time.time() - started < 7.0:
            bgr = cap.read()
            if bgr is None:
                error = cap.failed()
                if error:
                    raise SystemExit(f"Camera failed: {error}")
                continue
            recording = time.time() - started >= 3.0
            found = hands.landmarks(bgr)
            if recording and found:
                values.append(hand_shape(found[0]["local"]))
            frame = bgr.copy()
            for x, y in (found[0]["pixels"] if found else []):
                cv2.circle(frame, (int(x), int(y)), 4, (60, 220, 60), -1)
            cv2.putText(frame, f"{prompt} - {'RECORDING' if recording else 'get ready'} ({len(values)})",
                        (16, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (60, 220, 60) if recording else (0, 200, 255), 2)
            cv2.imshow("ScrapVR - hand calibration", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                raise SystemExit("Hand calibration aborted.")
        if len(values) < 10:
            raise SystemExit(f"Only {len(values)} {name} samples - keep one hand in the camera's view. Nothing saved.")
        samples[name] = np.median(np.array(values), axis=0)
        print(f"  {name}: spread {samples[name][0]:.2f}, gap {samples[name][1]:.2f}")
    cap.release()
    cv2.destroyAllWindows()
    open_spread, open_gap = samples["open"]
    fist_spread = samples["fist"][0]
    pinch_gap = samples["pinch"][1]
    if open_spread - fist_spread < 0.1 or open_gap - pinch_gap < 0.1:
        raise SystemExit("Open, fist and pinch came out too alike - nothing saved. Try again with clearer poses.")
    thresholds = {"grab_on": fist_spread + 0.4 * (open_spread - fist_spread),
                  "grab_off": fist_spread + 0.6 * (open_spread - fist_spread),
                  "pinch_on": pinch_gap + 0.35 * (open_gap - pinch_gap),
                  "pinch_off": pinch_gap + 0.55 * (open_gap - pinch_gap)}
    with open(args.hand_calibration, "w", encoding="utf-8") as f:
        json.dump({"samples": {name: value.tolist() for name, value in samples.items()},
                   "thresholds": {key: round(float(value), 3) for key, value in thresholds.items()}}, f, indent=2)
    print(f"Saved {args.hand_calibration}: {thresholds}")


def calibrate(args) -> None:
    cap = Camera(args.camera_name, args.width, args.height)
    board = make_board()
    detector = cv2.aruco.CharucoDetector(board)
    object_points, image_points = [], []
    last_capture = 0.0
    print(f"Calibration: show the printed board from {CALIBRATION_VIEWS} different angles and distances. q = abort.")
    while len(object_points) < CALIBRATION_VIEWS:
        bgr = cap.read()
        if bgr is None:
            error = cap.failed()
            if error:
                raise SystemExit(f'Camera "{args.camera_name}" failed: {error}')
            continue
        frame = bgr.copy()
        corners, ids, _, _ = detector.detectBoard(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        if ids is not None and len(ids) >= 8:
            # OpenCV 5 returns (N, 2) / (N,); the drawing call asserts on the old (N, 1, 2) / (N, 1).
            corners = corners.reshape(-1, 1, 2)
            ids = ids.reshape(-1, 1)
            cv2.aruco.drawDetectedCornersCharuco(frame, corners, ids)
            if time.time() - last_capture > 0.8:
                obj, img = board.matchImagePoints(corners, ids)
                if obj is not None and len(obj) >= 8:
                    object_points.append(obj)
                    image_points.append(img)
                    last_capture = time.time()
                    print(f"  view {len(object_points)}/{CALIBRATION_VIEWS}")
        cv2.putText(frame, f"views {len(object_points)}/{CALIBRATION_VIEWS} - tilt and move between captures",
                    (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (60, 220, 60), 2)
        cv2.imshow("ScrapVR - lens calibration", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            raise SystemExit("Calibration aborted.")
    cap.release()
    cv2.destroyAllWindows()
    rms, k, dist, _, _ = cv2.calibrateCamera(object_points, image_points, (args.width, args.height), None, None)
    with open(args.calibration, "w", encoding="utf-8") as f:
        json.dump({"width": args.width, "height": args.height, "rms": rms,
                   "K": k.tolist(), "dist": dist.reshape(-1).tolist()}, f, indent=2)
    print(f"Saved {args.calibration} (reprojection error {rms:.3f} px; under 1.0 is good).")


def selftest() -> None:
    """Render the marker for known eye positions and head turns, detect it, and run the
    same maths the receiver runs. Proves corner order, axes, signs and the lens-to-eyes offset."""
    width, height = 1280, 720
    k, dist, _ = load_intrinsics("", width, height, 70.0)
    marker_m = 0.15
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(MARKER_DICT), _detector_parameters())
    tile = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(MARKER_DICT), MARKER_ID, 600)
    tile = cv2.copyMakeBorder(tile, 100, 100, 100, 100, cv2.BORDER_CONSTANT, value=255)
    src = np.array([[100, 100], [700, 100], [700, 700], [100, 700]], dtype=np.float32)
    worst = 0.0
    # (eyes relative to the marker: x right, y up, z towards you), head yaw deg (right), pitch deg (up)
    cases = [([0.0, 0.0, 1.0], 0, 0), ([0.2, 0.0, 1.0], 0, 0), ([0.0, -0.1, 0.8], 0, 10),
             ([-0.15, 0.05, 1.3], 15, -5), ([0.1, 0.1, 0.9], -20, -10), ([0.0, 0.0, 1.0], 25, 0)]
    for eye, yaw_deg, pitch_deg in cases:
        eye = np.array(eye)
        yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
        head = head_rotation(yaw, pitch)
        camera_pos = eye - head @ EYES_FROM_LENS
        room_from_camera = head @ CAMERA_TO_HEAD
        rotation = room_from_camera.T                                # room -> camera
        tvec = -rotation @ camera_pos
        projected, _ = cv2.projectPoints(marker_object_points(marker_m), cv2.Rodrigues(rotation)[0], tvec, k, dist)
        warp = cv2.getPerspectiveTransform(src, projected.reshape(4, 2).astype(np.float32))
        image = cv2.warpPerspective(tile, warp, (width, height), borderValue=255)
        corners, ids, _ = detector.detectMarkers(image)
        if ids is None or MARKER_ID not in ids.flatten():
            raise SystemExit(f"SELFTEST FAIL: marker not detected for eye {eye}, yaw {yaw_deg}, pitch {pitch_deg}")
        marker_cam = marker_in_camera(corners[list(ids.flatten()).index(MARKER_ID)], k, dist, marker_m)
        got = eye_in_room(marker_cam, yaw, pitch)
        error = float(np.linalg.norm(got - eye))
        worst = max(worst, error)
        print(f"  eye {np.round(eye, 3)} yaw {yaw_deg:+d} pitch {pitch_deg:+d} -> {np.round(got, 3)}  "
              f"error {error * 1000:.1f} mm")
    print("SELFTEST", "PASS" if worst < 0.01 else "FAIL", f"(worst {worst * 1000:.1f} mm)")


def _detector_parameters() -> cv2.aruco.DetectorParameters:
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return params


def _send_hands(sock: socket.socket, target, hands: list, mount: dict) -> None:
    packet = {"type": "scrapvr_hands", "hands": [
        {"side": h["side"], "pinch": h["pinch"], "grab": h["grab"],
         "p": [round(float(v), 4) for v in h["points"].reshape(-1)]} for h in hands]}
    packet.update(mount)
    sock.sendto(json.dumps(packet).encode("utf-8"), target)


def _send(sock: socket.socket, target, marker_cam: np.ndarray, tracked: bool, mount: dict) -> None:
    packet = {"type": "scrapvr_head_pos", "tx": round(float(marker_cam[0]), 4),
              "ty": round(float(marker_cam[1]), 4), "tz": round(float(marker_cam[2]), 4), "tracked": tracked}
    packet.update(mount)
    sock.sendto(json.dumps(packet).encode("utf-8"), target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--calibrate", action="store_true", help="capture the ChArUco board and save intrinsics")
    parser.add_argument("--calibrate-hands", action="store_true", help="record open / fist / pinch and save thresholds")
    parser.add_argument("--calibrate-mount", action="store_true",
                        help="webcam vs the S9's rear camera (the eye view), both on the iPad board")
    parser.add_argument("--mount-calibration", default=MOUNT_CALIBRATION)
    parser.add_argument("--phone-ip", default="", help="the S9 on the tether; found from the RNDIS route if empty")
    parser.add_argument("--board-square-mm", type=float, default=38.5,
                        help="a chessboard square as shown (12.9in iPad, calibration_screen.png full screen: 38.5)")
    parser.add_argument("--debug-dir", default="", help="save the latest camera views here")
    parser.add_argument("--hand-calibration", default=HAND_CALIBRATION)
    parser.add_argument("--selftest", action="store_true", help="check the maths on a rendered marker")
    parser.add_argument("--measure", type=float, default=0.0, metavar="SECONDS",
                        help="headset still: record this long and report how steady the position is")
    parser.add_argument("--camera-name", default="Lenovo 510 RGB Camera", help="DirectShow name, as ffmpeg lists it")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--marker-mm", type=float, default=150.0, help="measured edge of the printed marker")
    parser.add_argument("--hfov", type=float, default=70.0, help="horizontal FOV guess when uncalibrated")
    parser.add_argument("--calibration", default=DEFAULT_CALIBRATION)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=16061)
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--no-hands", action="store_true", help="head position only, no hand tracking")
    parser.add_argument("--tilt-down", type=float, default=0.0, metavar="DEG",
                        help="how far the camera mount points down from your eye line; sent to the receiver")
    parser.add_argument("--snapshot", default="", metavar="PNG", help="save the camera's view here every 2 s")
    a = parser.parse_args()
    if a.selftest:
        selftest()
    elif a.measure > 0:
        measure(a)
    elif a.calibrate_hands:
        calibrate_hands(a)
    elif a.calibrate_mount:
        calibrate_mount(a)
    elif a.calibrate:
        calibrate(a)
    else:
        track(a)
