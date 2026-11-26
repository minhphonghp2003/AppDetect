# Linux new-window detector → OpenClaw → owner Zalo (demo)

Detects **new dialogs/windows** via AT-SPI (works on X11 **and** Wayland,
no GNOME Shell Eval needed), captures a screenshot, POSTs to OpenClaw
`hooks/agent` so the agent forwards it to the owner's Zalo.

## Run

```bash
export OPENCLAW_TOKEN='<your-token>'

python3 detector.py --dry-run --once        # show current windows, exit
python3 detector.py --dry-run --duration 60 # watch 60s, print only
python3 detector.py --live                  # daemon, really POSTs
```

Dry-run is the default. `--live` requires the token.

## Screenshots on GNOME Wayland

Programmatic screenshots go through `xdg-desktop-portal` and need a
one-time user grant. Run once and click **Allow** in the system dialog:

```bash
python3 detector.py --grant-screenshot
```

After that, daemon runs capture non-interactively. Without the grant the
webhook still sends window details with `Screenshot: unavailable ...`.

(X11 sessions: `pip install mss` is used instead, no permission needed.
Note: `mss` returns a black image on Wayland — expected, not a bug.)

## Webhook format

```
POST http://127.0.0.1:18789/hooks/agent
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <uuid per event>

{"message": "\U0001F514 A new dialog \"Are you still there?\" appeared on screen.",
 "agentId": "main", "deliver": false}
```

Equivalent curl:

```bash
curl -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer $OPENCLAW_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"message":"test from detector","agentId":"main","deliver":false}'
```

## Tuning

- `IGNORE_APPS` / `IGNORE_NAMES` in `detector.py` silence desktop noise.
- `DETECT_COOLDOWN=300` re-alerts the same popup at most every 5 min.
- New `frame`/`window` must have a title; `dialog`/`alert` always alert.
