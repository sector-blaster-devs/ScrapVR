"""Keeps the phone-in-headset link up without taking the phone out of the headset. Every
few seconds, over adb:

- USB tethering gone (no IPv4 on rndis0) -> set the USB function back to RNDIS; Android
  tethers again by itself.
- Tethering up but dead (rndis0 has its address yet sends 0 packets, so the phone cannot
  reach Sunshine) -> switch the USB function off and back to RNDIS.
- Moonlight not streaming -> one step per check towards it: an error dialog (a USB blip
  gives "Connection Terminated", error 22) -> OK; the PC list -> your PC; the app list ->
  Resume Session (the old session survives the blip) or the app tile.

Needs USB debugging always on (no approval prompt after a reconnect). Windows PC (it
finds its tether address from the RNDIS adapter).

  python phone_watchdog.py --host MYPC     # the PC's name as Moonlight lists it; Ctrl+C to stop
"""
import argparse
import html
import re
import subprocess
import time
import unicodedata

POLL_S = 3.0
ERROR_TITLES = ("Connection Terminated", "Connection Error")


def adb(*args, timeout: float = 20.0) -> str:
    try:
        return subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def clean(text: str) -> str:
    """Moonlight's labels carry invisible bidi marks (the OK button does): drop format characters."""
    return "".join(c for c in html.unescape(text) if unicodedata.category(c) != "Cf").strip()


def focus() -> str:
    """The focused window, e.g. 'Connection Terminated' or 'com.limelight/com.limelight.AppView'."""
    match = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([^}]*)\}", adb("shell", "dumpsys", "window"))
    return match.group(1) if match else ""


def screen() -> list:
    """[(label, resource id, x, y)] for every node; empty while a video surface keeps the UI busy."""
    xml = adb("shell", "uiautomator dump /sdcard/scrapvr_ui.xml >/dev/null 2>&1; cat /sdcard/scrapvr_ui.xml")
    nodes = []
    for node in re.finditer(r"<node [^>]*>", xml):
        attrs = dict(re.findall(r'(\S+)="([^"]*)"', node.group(0)))
        bounds = [int(v) for v in re.findall(r"\d+", attrs.get("bounds", ""))]
        label = clean(attrs.get("text", "") or attrs.get("content-desc", ""))
        if len(bounds) == 4:
            nodes.append((label, attrs.get("resource-id", ""), (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2))
    return nodes


def tap(label: str, exact: bool = True, nodes: list = None) -> bool:
    for text, _, x, y in (screen() if nodes is None else nodes):
        if text and ((text == label) if exact else (label.lower() in text.lower())):
            adb("shell", "input", "tap", str(x), str(y))
            log(f"tapped '{label}'")
            return True
    return False


def tap_id(resource_id: str, nodes: list) -> bool:
    """The first node with this id; for the app tiles, which are pictures without text."""
    for _, rid, x, y in nodes:
        if rid == resource_id:
            adb("shell", "input", "tap", str(x), str(y))
            log(f"tapped {resource_id.split('/')[-1]}")
            return True
    return False


def tethered() -> bool:
    return "inet " in adb("shell", "ip -4 addr show rndis0 2>/dev/null")


def pc_address() -> str:
    """This PC's own address on the tether (the RNDIS adapter)."""
    script = ("(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex (Get-NetAdapter | Where-Object "
              "InterfaceDescription -like '*NDIS*' | Select-Object -First 1).ifIndex -ErrorAction SilentlyContinue).IPAddress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.splitlines()[0] if out else ""


def sunshine_reachable(pc: str) -> bool:
    """Asked from the phone, so it proves the tether carries traffic both ways."""
    reply = adb("shell", f"printf 'GET /serverinfo HTTP/1.0\\r\\n\\r\\n' | nc -w 3 {pc} 47989 | head -c 200")
    return "<root" in reply


def retether() -> None:
    adb("shell", "svc", "usb", "setFunctions")
    time.sleep(3.0)
    adb("wait-for-device", timeout=30.0)
    adb("shell", "svc", "usb", "setFunctions", "rndis")
    time.sleep(8.0)


def moonlight_step(window: str, host: str) -> None:
    """One step back towards streaming, chosen by the screen in front; the next check
    takes the next step. PC list -> the host; app list -> Resume Session if its menu is
    open, else the app tile (a picture, no text: tapping it resumes or opens that menu)."""
    if "com.limelight.PcView" in window:
        tap(host)
    elif "com.limelight.AppView" in window:
        nodes = screen()
        if not tap("Resume Session", nodes=nodes):
            tap_id("com.limelight:id/grid_image", nodes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="the PC's name as Moonlight shows it")
    args = parser.parse_args()
    log("Phone watchdog running (Ctrl+C to stop).")
    while True:
        if "\tdevice" not in adb("devices"):
            time.sleep(POLL_S)
            continue
        if not tethered():
            log("USB tethering is off - switching the USB function back to RNDIS.")
            adb("shell", "svc", "usb", "setFunctions", "rndis")
            time.sleep(6.0)
            continue
        pc = pc_address()
        if pc and not sunshine_reachable(pc):
            log("Tether is up but the phone cannot reach Sunshine - toggling it.")
            retether()
            continue
        window = focus()
        if any(title in window for title in ERROR_TITLES):
            log(f"Moonlight says '{window}'.")
            tap("OK")
        elif "com.limelight.PcView" in window or "com.limelight.AppView" in window:
            moonlight_step(window, args.host)
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
