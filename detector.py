#!/usr/bin/env python3
"""Linux new-window/dialog detector -> OpenClaw webhook (demo).

How it works
------------
1. Polls the accessibility tree (AT-SPI) every --interval seconds.
   Works on X11 AND Wayland (incl. Ubuntu GNOME where Shell Eval is locked down).
2. Alerts ONLY on NEW top-level windows/dialogs (role frame/dialog/alert/window),
   not on every focus change.
3. Captures a screenshot (portal on Wayland after one-time grant, mss on X11).
4. POSTs to OpenClaw hooks/agent so the agent can forward to owner Zalo.

Default mode is DRY-RUN (prints what would be sent). Use --live to really POST.

Setup:
  export OPENCLAW_TOKEN='<your-token>'
  # optional: echo "OPENCLAW_TOKEN=..." > .env
  python3 detector.py --dry-run --once        # snapshot dump
  python3 detector.py --grant-screenshot      # one-time Wayland permission (click Allow)
  python3 detector.py --live                  # daemon

Requires (Ubuntu system packages, no pip needed):
  gir1.2-atspi-2.0, python3-gi  (AT-SPI)
Optional: python3-mss (screenshots on X11 only)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import time
import urllib.request
import uuid

# ---------------------------------------------------------------- config

OPENCLAW_URL = os.environ.get(
    "OPENCLAW_URL", "http://127.0.0.1:18789/hooks/agent"
)
AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "main")
ZALO_CHANNEL = os.environ.get("ZALO_CHANNEL", "zalo")
ZALO_TARGET = os.environ.get("ZALO_TARGET", "8513a5da1d8ff4d1ad9e")
POLL_INTERVAL = float(os.environ.get("DETECT_INTERVAL", "1.0"))
PER_KEY_COOLDOWN = int(os.environ.get("DETECT_COOLDOWN", "300"))
SHOTS_DIR = os.environ.get("DETECT_SHOTS_DIR", "./shots")

ALERT_ROLES = {"dialog", "alert"}                      # always alert, even unnamed
FRAME_ROLES = {"frame", "window"}                      # alert only if named
IGNORE_APPS = {"gnome-shell", "gjs", "ibus-extension-gtk3",
               "xdg-desktop-portal-gtk", "evolution-alarm-notify",
               "update-notifier", "gnome-shell-calendar-server"}
IGNORE_NAMES = {"Main stage", "Desktop Icons 1", "Desktop Icons 2"}


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip("'\"")
            os.environ.setdefault(k.strip(), v)


load_dotenv()

# ---------------------------------------------------------------- AT-SPI snapshot

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    HAVE_ATSPI = True
except Exception as e:  # pragma: no cover
    print(f"WARN: AT-SPI unavailable ({e}); detector cannot run.", file=sys.stderr)
    HAVE_ATSPI = False


def snapshot():
    """Return (windows dict, active_key). windows: key -> {app, role, name}."""
    from gi.repository import Atspi

    windows = {}
    active_key = None
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            app_name = app.get_name() or "?"
        except Exception:
            continue
        if app_name in IGNORE_APPS:
            continue
        try:
            n = app.get_child_count()
        except Exception:
            continue
        for j in range(min(n, 40)):
            try:
                w = app.get_child_at_index(j)
                if w is None:
                    continue
                role = (w.get_role_name() or "").lower()
                name = w.get_name() or ""
                if role not in ALERT_ROLES and role not in FRAME_ROLES:
                    continue
                try:
                    states = w.get_state_set()
                    showing = states.contains(Atspi.StateType.SHOWING)
                    is_active = states.contains(Atspi.StateType.ACTIVE)
                except Exception:
                    showing = True
                    is_active = False
                if not showing:
                    continue
                key = f"{app_name}\u2502{role}\u2502{name}"
                windows[key] = {"app": app_name, "role": role, "name": name}
                if is_active:
                    active_key = key
            except Exception:
                continue
    return windows, active_key


def is_alertable(info):
    if info["role"] in ALERT_ROLES:
        return True
    return bool(info["name"]) and info["name"] not in IGNORE_NAMES


# ---------------------------------------------------------------- screenshots

def capture_screenshot(shots_dir, interactive=False):
    """Return screenshot path or None. Wayland: portal (needs one-time grant).
    X11: mss. Never raises."""
    os.makedirs(shots_dir, exist_ok=True)
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session == "x11" or not session:
        path = _shot_mss(shots_dir)
        if path:
            return path
    return _shot_portal(shots_dir, interactive=interactive)


def _shot_mss(shots_dir):
    try:
        import mss

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(shots_dir, f"shot_{stamp}.png")
        with mss.mss() as s:
            s.shot(output=path)
        if os.path.getsize(path) < 1024:
            return None
        # reject blank grabs (Wayland returns black via X API)
        try:
            from PIL import Image

            im = Image.open(path).convert("RGB").resize((96, 54))
            px = list(im.getdata())
            mean = sum(sum(p) for p in px) / len(px) / 3
            if mean < 8:
                return None
        except Exception:
            pass
        return path
    except Exception as e:
        print(f"screenshot(mss): skipped ({e})")
        return None


def _shot_portal(shots_dir, interactive=False, timeout_s=12):
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as e:
        print(f"screenshot(portal): skipped ({e})")
        return None

    # Interactive grant needs time for the user to find + click the dialog.
    wait_s = 120 if interactive else timeout_s
    if interactive:
        print("Waiting up to 120s: look for the SYSTEM screenshot dialog "
              "(top bar) and click Allow/Share, then pick screen if asked.")

    result = {}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        loop = GLib.MainLoop()

        def on_signal(conn, sender, path, iface, sig, params, data):
            if sig == "Response":
                try:
                    code, values = params.unpack()
                except Exception:
                    code, values = None, {}
                result["code"] = code
                result["data"] = values if isinstance(values, dict) else {}
                loop.quit()

        bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            on_signal,
            None,
        )
        token = f"shot{uuid.uuid4().hex[:8]}"
        opts = {"handle_token": GLib.Variant("s", token),
                "interactive": GLib.Variant("b", interactive)}
        req = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot",
            "Screenshot",
            GLib.Variant("(sa{sv})", ["", opts]),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            10000,
            None,
        )
        GLib.timeout_add_seconds(wait_s, loop.quit)
        loop.run()
    except Exception as e:
        print(f"screenshot(portal): skipped ({e})")
        return None

    if "code" not in result:
        print(f"screenshot(portal): timed out after {wait_s}s with no system response. "
              "Did a system dialog appear? If not, see README Wayland notes.")
        return None
    if result.get("code") != 0:
        print(f"screenshot(portal): denied/cancelled (response={result.get('code')}). "
              "Run again with --grant-screenshot and click Allow in the system dialog.")
        return None
    uri = result["data"].get("uri", "") or ""
    if not uri:
        print(f"screenshot(portal): success response but no uri "
              f"(keys={sorted(result['data'].keys())})")
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(shots_dir, f"shot_{stamp}.png")
    try:
        Gio.File.new_for_uri(uri).copy(
            Gio.File.new_for_path(dst),
            Gio.FileCopyFlags.OVERWRITE,
            None, None, None, None)
        return dst
    except Exception as e:
        print(f"screenshot(portal): copy failed ({e}), uri={uri}")
        return None


# ---------------------------------------------------------------- OpenClaw webhook

def build_message(info, shot_path):
    name = (info["name"] or "").strip()
    if name:
        text = f"\U0001F514 A new {info['role']} \u201c{name}\u201d appeared on screen."
    else:
        text = f"\U0001F514 A new {info['role']} appeared on screen."
    send = (f'Send it now via the message tool: action "send", '
            f'channel "{ZALO_CHANNEL}", target "{ZALO_TARGET}", '
            f'message "{text}"')
    if shot_path:
        send += f', media "{shot_path}"'
    else:
        send += " (no screenshot was captured; text only)"
    return f"{text}\n{send}."


def send_openclaw(message, url, token, agent_id="main", deliver=False):
    payload = json.dumps(
        {"message": message, "agentId": agent_id, "deliver": deliver}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": uuid.uuid4().hex,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()[:500]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Linux new-window detector -> OpenClaw")
    ap.add_argument("--live", action="store_true", help="really POST to OpenClaw (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry-run (default behavior)")
    ap.add_argument("--once", action="store_true", help="dump current windows and exit")
    ap.add_argument("--grant-screenshot", action="store_true", help="one interactive portal screenshot to grant permission, then exit")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL)
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = forever)")
    ap.add_argument("--token", default=os.environ.get("OPENCLAW_TOKEN", ""))
    ap.add_argument("--url", default=OPENCLAW_URL)
    ap.add_argument("--agent-id", default=AGENT_ID)
    ap.add_argument("--no-screenshot", action="store_true")
    args = ap.parse_args()
    live = args.live and not args.dry_run

    if not HAVE_ATSPI:
        return 2

    if args.grant_screenshot:
        p = capture_screenshot(os.path.abspath(SHOTS_DIR), interactive=True)
        print(f"screenshot: {p or 'FAILED - no permission granted'}")
        return 0 if p else 1

    if args.once:
        wins, active = snapshot()
        print(f"windows={len(wins)} active={active}")
        for k in sorted(wins):
            print(f"  {k}  alertable={is_alertable(wins[k])}")
        return 0

    if live and not args.token:
        print("ERROR: --live needs OPENCLAW_TOKEN env, .env entry, or --token.", file=sys.stderr)
        return 2

    known, _ = snapshot()
    print(f"watching {len(known)} windows, interval={args.interval}s "
          f"mode={'LIVE -> ' + args.url if live else 'DRY-RUN (use --live to POST)'}",
          flush=True)
    last_sent_global = 0.0
    last_sent_key: dict[str, float] = {}
    t0 = time.time()

    while True:
        time.sleep(args.interval)
        if args.duration and time.time() - t0 > args.duration:
            break
        try:
            wins, active = snapshot()
        except Exception as e:
            print(f"snapshot error: {e}", flush=True)
            continue
        now = time.time()
        for key, info in wins.items():
            if key in known or not is_alertable(info):
                continue
            known[key] = info
            if now - last_sent_key.get(key, 0) < PER_KEY_COOLDOWN:
                continue
            if now - last_sent_global < 5:  # burst guard
                continue
            shot = None if args.no_screenshot else capture_screenshot(os.path.abspath(SHOTS_DIR))
            msg = build_message(info, shot)
            last_sent_key[key] = now
            last_sent_global = now
            if live:
                try:
                    st, body = send_openclaw(msg, args.url, args.token, args.agent_id)
                    print(f"ALERT sent {st} :: {key} :: shot={shot} :: {body}", flush=True)
                except Exception as e:
                    print(f"ALERT FAILED :: {key} :: {e}", flush=True)
            else:
                print(f"[DRY-RUN] would send :: {key} (active={active} shot={shot})\n  msg={msg}", flush=True)
        # forget closed windows so reopening re-alerts after cooldown
        for key in [k for k in known if k not in wins]:
            del known[key]


if __name__ == "__main__":
    sys.exit(main())
