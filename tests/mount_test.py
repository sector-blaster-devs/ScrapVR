"""Synthetic check of head_tracker.board_pose + solve_mount: a known webcam mount and the
S9 camera (either landscape way round) both see the ChArUco board rendered into their
pictures; the solved mount must match the truth."""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker"))
import head_tracker as h
from make_markers import make_board

SQUARE_MM = 38.5
board = make_board(SQUARE_MM)
detector = cv2.aruco.CharucoDetector(board)
PX_PER_MM = 4
cols, rows = 4, 6
board_w, board_h = int(cols * SQUARE_MM * PX_PER_MM), int(rows * SQUARE_MM * PX_PER_MM)
board_img = board.generateImage((board_w, board_h), marginSize=0, borderBits=1)

k_web, _, _ = h.load_intrinsics(h.DEFAULT_CALIBRATION, 1280, 720, 70.0)
k_s9 = np.array([[1550.0, 0, 1008.0], [0, 1550.0, 756.0], [0, 0, 1]])
zero = np.zeros(5)

# The board in head axes (x right, y up, z back): upright, facing you, 0.6 m ahead, its
# top edge at eye height; board x = right, board y = down the board, board z = away.
head_from_board = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])
DISTANCE = float(sys.argv[1]) if len(sys.argv) > 1 else 0.6
board_origin = np.array([-0.077, 0.0, -DISTANCE])
print(f"board {DISTANCE:.2f} m ahead")


SUPERSAMPLE = 4


def render(head_from_cam, cam_pos, k, size):
    """The board as this camera sees it (pinhole, no distortion). Drawn at 4x and averaged
    down, like a real sensor: a plain warp would alias the edges and bias the corners."""
    r_cb = head_from_cam.T @ head_from_board
    t_cb = head_from_cam.T @ (board_origin - cam_pos)
    corners_px = np.array([[0, 0], [board_w, 0], [board_w, board_h], [0, board_h]], dtype=np.float64)
    corners_board = np.column_stack([corners_px / PX_PER_MM / 1000.0, np.zeros(4)])
    k_big = k.copy()
    k_big[:2] *= SUPERSAMPLE
    k_big[0, 2] += (SUPERSAMPLE - 1) / 2.0   # keep pixel centres aligned after the box downsample
    k_big[1, 2] += (SUPERSAMPLE - 1) / 2.0
    projected, _ = cv2.projectPoints(corners_board, cv2.Rodrigues(r_cb)[0], t_cb, k_big, zero)
    warp = cv2.getPerspectiveTransform(corners_px.astype(np.float32), projected.reshape(4, 2).astype(np.float32))
    big = cv2.warpPerspective(board_img, warp, (size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE), borderValue=255)
    gray = cv2.resize(big, size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


ok = True
true_turn, true_tilt = 3.0, 27.0
yaw = cv2.Rodrigues(np.array([0.0, math.radians(-true_turn), 0.0]))[0]
true_cam_to_head = yaw @ h.camera_to_head(true_tilt)
true_eyes_from_lens = np.array([0.01, -0.06, 0.08])
for display_rotation in (1, 3):
    for sensor_orientation in (90,):
        head_from_s9 = h.head_from_device(display_rotation) @ h.device_from_rear_camera(sensor_orientation)
        s9_pos = -(h.head_from_device(display_rotation) @ h.EYES_FROM_S9_CAMERA)   # the eyes are at the origin
        web_pos = -true_eyes_from_lens
        s9_img = render(head_from_s9, s9_pos, k_s9, (2016, 1512))
        web_img = render(true_cam_to_head, web_pos, k_web, (1280, 720))
        s9 = h.board_pose(s9_img, k_s9, zero, board, detector)
        web = h.board_pose(web_img, k_web, zero, board, detector)
        if s9 is None or web is None:
            print(f"FAIL rotation {display_rotation}: board not found (S9 {s9 is not None}, webcam {web is not None})")
            ok = False
            continue
        relative = s9[0] @ web[0].T
        pairs = [(relative, s9[1] - relative @ web[1], s9[0])] * 8
        m = h.solve_mount(pairs, display_rotation, sensor_orientation)
        angle = h._angle_deg(m["camera_to_head"], true_cam_to_head)
        offset_mm = np.linalg.norm(m["eyes_from_lens"] - true_eyes_from_lens) * 1000.0
        good = angle < 0.5 and offset_mm < 5.0 and abs(m["tilt_down_deg"] - true_tilt) < 0.5 \
            and abs(m["turn_deg"] - true_turn) < 0.5
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} display rotation {display_rotation}: tilt {m['tilt_down_deg']:.2f} (27), "
              f"turn {m['turn_deg']:+.2f} (+3), rotation error {angle:.3f} deg, eyes-from-lens error {offset_mm:.1f} mm, "
              f"board corners S9 {s9[2]} / webcam {web[2]}, board down in head {np.round(m['board_down'], 2)}")
print("MOUNT", "PASS" if ok else "FAIL")
