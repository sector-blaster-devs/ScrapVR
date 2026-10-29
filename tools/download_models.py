"""Fetches the MediaPipe hand model (Apache 2.0, by Google) into tracker/models/.
It is not kept in the repo: 7.8 MB, and Google hosts the current version.

  python tools/download_models.py
"""
import os
import urllib.request

URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker", "models", "hand_landmarker.task")

os.makedirs(os.path.dirname(TARGET), exist_ok=True)
urllib.request.urlretrieve(URL, TARGET)
print(f"Saved {os.path.normpath(TARGET)} ({os.path.getsize(TARGET) / 1e6:.1f} MB)")
