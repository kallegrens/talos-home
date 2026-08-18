---
name: emhass
description: >-
  EMHASS energy optimizer running on Kubernetes. Handles MPC battery
  optimization, PV forecasting, and GoodWe inverter control via Home
  Assistant. Load this skill when editing EMHASS config, MPC optimization
  parameters, battery control automations, or price forecast sensors.
---

# EMHASS

**Namespace:** `emhass`
**Service:** `emhass.emhass.svc.cluster.local:5000`
**Chart:** bjw-s app-template v4.6.2
**Image:** `ghcr.io/kallegrens/emhass:fix-solcast-and-empty-days` (fork with PR #739 + #740)
**Upstream:** `ghcr.io/davidusb-geek/emhass` — revert once PRs are merged

## Documentation

The full documentation directory for EMHASS can be found at `clusters/main/kubernetes/apps/emhass/resources/emhass-docs/`. This is the same as found on their website. Reference this before answering questions about EMHASS features, config options, or architecture. Never guess or make assumptions about how EMHASS works without checking the docs first.

## Key Files

| File | Purpose |
|---|---|
| `app/helm-release.yaml` | Helm values (image, ingress, persistence, mounts) |
| `app/emhass-config.yaml` | ConfigMap → mounted at `/share/config.json` |
| `app/emhass-secrets.secret.yaml` | SOPS Secret → mounted at `/app/secrets_emhass.yaml` |

## Fork & Custom Image

The EMHASS fork lives at `/home/karl/karl-syncthing/emhass/`. The combined
branch `fix/combined-solcast-and-empty-days` contains fixes from PRs #739
and #740. To build and push a new image:

```bash
cd /home/karl/karl-syncthing/emhass
docker build -t ghcr.io/kallegrens/emhass:fix-solcast-and-empty-days .
docker push ghcr.io/kallegrens/emhass:fix-solcast-and-empty-days
kubectl rollout restart -n emhass deploy/emhass   # pullPolicy: Always re-pulls
```

## Configuration

Config lives in `emhass-config.yaml` (ConfigMap). The ConfigMap is mounted
via `subPath`, so Kubernetes will **not** propagate changes into the running
pod automatically. After editing the config:

```bash
flux reconcile source git cluster                     # Flux pulls latest git
kubectl rollout restart deployment/emhass -n emhass   # Pod restarts with new config
```

Never `kubectl apply` the ConfigMap directly — let Flux own the resource.

Key settings:

- 15-min optimization time step, dynamic prediction horizon (from HA payload)
- Solcast weather forecasting (free tier: 10 calls/day, resets ~14:00 CET)
- Battery: 14.2 kWh capacity, 5050W max charge/discharge
- PV: 32 modules, 2 strings, 10kW inverter, 14° tilt, 125° azimuth
- ML PV correction: LassoRegression, retrained every 24h
- Reads HA sensors via long-lived token in `secrets_emhass.yaml`

## ⚠️ Logs Are Extremely Verbose

**Never run `kubectl logs -n emhass deploy/emhass` without `--tail=N`.**
EMHASS logs every optimization step at DEBUG level — thousands of lines per cycle. Always use:

```bash
kubectl logs -n emhass deploy/emhass --tail=N
```

## ⚠️ Solcast Rate Limits

The Solcast free tier allows **10 API calls per day**, resetting on
Australian time (~14:00 CET). Each MPC call that uses
`weather_forecast_method: solcast` consumes one credit. The weather cache
automation (`emhass_pv_forecast_cache`) is designed to fetch once and cache.

**Never call the MPC endpoint repeatedly for testing/benchmarking** without
overriding `weather_forecast_method`. To bypass Solcast in test calls, pass
`pv_power_forecast` as a list in runtimeparams — EMHASS auto-switches to
`weather_forecast_method: list`:

```bash
kubectl exec -n emhass deploy/emhass -- curl -s -X POST \
  http://localhost:5000/action/naive-mpc-optim \
  -H 'Content-Type: application/json' \
  -d '{"prediction_horizon": 48, "pv_power_forecast": [0,0,...], ...}'
```

If Solcast quota is exhausted, all real MPC calls will fail and **stale
sensor values persist in HA** until the next successful call. The failsafe
automation will revert to `general` mode after 20 min of stale data.

## Deploying & Testing Code Changes in the Container

### Container Architecture

EMHASS runs under **tini → gunicorn → uvicorn workers**. The entrypoint:

```
/usr/bin/tini -- /bin/bash -c "... exec uv run --frozen gunicorn emhass.web_server:app ..."
```

Key details:

- Python is managed by `uv` — there is no `python` or `python3` on `$PATH`.
  Use `uv run python` to execute Python inside the container.
- The package is installed as an editable install from `/app/src/emhass/`.
- `kill` is NOT available in the container (minimal image).

### Copying Modified Files

You can `kubectl cp` modified source files into the running pod:

```bash
POD=$(kubectl get pod -n emhass -o jsonpath='{.items[0].metadata.name}')
kubectl cp /path/to/forecast.py emhass/$POD:/app/src/emhass/forecast.py
```

**However, this alone does NOT take effect.** Gunicorn workers cache all
imported Python modules in memory. The copied file sits on disk but the
running worker still uses the old code.

### Reloading Code (The Hard Way)

There is **no clean way to hot-reload** code in the running pod:

1. **`kill -HUP 1`** — would reload gunicorn workers, but `kill` is not in
   the container image. Sending signals via Python (`os.kill(1, 1)`) requires
   `uv run python` which triggers `uv` to reinstall the package first.
2. **`uv run python -c "import os; os.kill(1, 1)"`** — this works, but as a
   side effect `uv` detects the modified source file and **rebuilds the
   package** before executing. The SIGHUP then restarts gunicorn workers, so
   they load the newly-built package. This is actually the most reliable
   approach.
3. **Clearing `__pycache__`** alone does NOT help — the module is already
   loaded in the worker's memory.

### Recommended Workflow for Code Changes

**Option A: Build a new Docker image (preferred for persistent changes)**

```bash
cd /home/karl/karl-syncthing/emhass
# Make your changes in src/emhass/
docker build -t ghcr.io/kallegrens/emhass:fix-solcast-and-empty-days .
docker push ghcr.io/kallegrens/emhass:fix-solcast-and-empty-days
kubectl rollout restart -n emhass deploy/emhass
```

**Option B: Quick test in running pod (ephemeral, lost on restart)**

```bash
POD=$(kubectl get pod -n emhass -o jsonpath='{.items[0].metadata.name}')
# Copy the modified file
kubectl cp src/emhass/forecast.py emhass/$POD:/app/src/emhass/forecast.py
# Rebuild package + restart workers (one command)
kubectl exec -n emhass deploy/emhass -- uv run python -c "import os; os.kill(1, 1)"
# Wait for workers to restart (~10s)
sleep 15
# Verify the new code is loaded
kubectl logs -n emhass deploy/emhass --tail=5
```

**Option C: Run a standalone script inside the pod**

For benchmarking or analysis scripts that don't need to modify the web
server, copy and run directly:

```bash
kubectl cp /tmp/my_script.py emhass/$POD:/tmp/my_script.py
kubectl exec -n emhass deploy/emhass -- uv run python /tmp/my_script.py
```

This runs with the full EMHASS environment (sklearn, pvlib, pandas, etc.)
and can access HA via the token in `/app/secrets_emhass.yaml`.

### ⚠️ Critical: Never Publish Stale/Test Data

Benchmark or test calls to `/action/naive-mpc-optim` **will publish results
to HA sensors** (via `continual_publish`). If you pass dummy parameters
(e.g. `soc_init: 0.5`, `pv_power_forecast: [0,0,...]`), those garbage
values become the live battery control signal.

To avoid this during testing:

- Use the `publish-data` action separately after validating results
- Or immediately trigger a real MPC call afterward to overwrite test values
- Or temporarily disable the `emhass_battery_control` automation in HA

### Pod Restart Clears Everything

`kubectl rollout restart` creates a **new pod from the Docker image**. All
`kubectl cp`'d files, cached models (`/data/*.pkl`), and weather caches are
lost (unless `/data` is on a PersistentVolume). The ML PV model will
retrain on the next MPC cycle (takes ~35s, covered by the 300s HA timeout).

## MPC Optimization Flow

1. HA automation "EMHASS MPC Loop" fires every **4 minutes** (coprime with
   15-min price slots to prevent systematic alignment flip-flop)
2. Calls `rest_command.emhass_mpc_optim` with dynamic payload:
   - Current SOC, buy/sell price forecasts, prediction horizon
   - Snow override (zeroes PV forecast if panels are snow-covered)
   - BMS SOC clamp: when BMS blocks charge/discharge, sets
     `battery_maximum/minimum_state_of_charge = curr_soc` so the
     optimizer doesn't plan impossible battery actions
3. EMHASS solves MPC and publishes via `continual_publish` to HA sensors:
   - `sensor.p_batt_forecast` — battery power schedule
   - `sensor.soc_batt_forecast` — SOC trajectory
   - `sensor.p_pv_forecast` — PV forecast used
4. HA automation "EMHASS Battery Control" triggers on `p_batt_forecast` change
5. Uses 4-slot lookahead to determine dominant intent (charge/discharge/idle)
6. Translates to GoodWe mode: `eco_charge`, `eco_discharge`, or `general` (idle)
7. Grid export limit set to 0W on discharge (prevents battery→grid spill)
8. Failsafe switches to `general` mode if forecast stale >20 min

## BMS SOC Clamping

When the GoodWe BMS refuses further charge or discharge (common near SOC
limits), EMHASS would otherwise plan battery actions based on a range it
can't actually use, causing up to ±5% SOC forecast error.

**Detection:** `sensor.goodwe_diag_status` is parsed for BMS flags:

- `"BMS: Discharge disabled"` → `binary_sensor.bms_discharge_disabled`
- `"BMS: Charge disabled"` → `binary_sensor.bms_charge_disabled`

**Hysteresis:** Binary sensors have `delay_on: 2 min` / `delay_off: 5 min`.
The diag register flips between "Charge time on" / "Discharge time on" every
~7 min as the automation switches modes, but BMS flags are stable across
these transitions. The 2-min delay_on debounces mode-switch noise; the 5-min
delay_off prevents oscillation when BMS toggles at the edge.

**Runtime params:** When a BMS binary sensor is ON, the MPC payload sends:

- `battery_minimum_state_of_charge = curr_soc` (discharge blocked)
- `battery_maximum_state_of_charge = curr_soc` (charge blocked)

Otherwise the static defaults (0.05 / 1.0) are sent.

## Battery Control Details

### GoodWe Two-Layer Control Architecture

The GoodWe ET inverter has **two independent control layers**:

1. **Inverter Operation Mode** (register 47000):
   `general`(0), `off_grid`(1), `backup`(2), `eco`(3), `peak_shaving`(4),
   `eco_charge`(98, emulated), `eco_discharge`(99, emulated)
2. **EMS Mode** (register 47511) + **EMS Power Limit** (register 47512):
   `auto`(1), `charge_pv`(2), `discharge_pv`(3), `import_ac`(4),
   `export_ac`(5), `conserve`(6), `battery_standby`(8), etc.

**Critical side effect:** `set_operation_mode()` (via HACS select entity)
always calls `_set_offline(False)`, which **resets EMS mode to AUTO** and
EMS power limit to 0. This is a one-way decoupling — changing EMS mode
does NOT affect the operation mode. For modes that call
`_clear_battery_mode_param()` (general, backup, peak_shaving, self_use),
register 47533 is also written, which may reset current-limit parameters.

**eco_charge/eco_discharge** are emulated by the goodwe library (values
98/99). They set `work_mode=3` (eco) + program eco_mode_1 schedule
00:00-23:59 with the configured power% and SOC%.

**Writable parameters via `goodwe.set_parameter`:**

- `battery_charge_current` (register 45353) — max charge current in amps
- `battery_discharge_current` (register 45355) — max discharge current in amps

### Mode Mapping

| EMHASS Decision | Inverter Mode | Key Settings |
| --- | --- | --- |
| **CHARGE** (p_batt < -deadband) | `eco_charge` | Rate-controlled grid→battery. Both currents restored to 25A. grid_export_limit=10kW. eco_mode_power=charge%, eco_mode_soc=target+buffer. |
| **DISCHARGE** (p_batt > deadband) | `eco_discharge` | Rate-controlled battery→house. Both currents restored to 25A. grid_export_limit=0W (prevents battery→grid spill). eco_mode_power=discharge%, eco_mode_soc=target-buffer. |
| **IDLE** (p_batt ≈ 0) | `general` + `battery_discharge_current=0` | PV serves house first, surplus charges battery. Battery CANNOT discharge (discharge_current=0). Export only when battery full + excess PV. No grid→battery charge (general mode). grid_export_limit=10kW (restored). |

**Order matters for IDLE:** `set_operation_mode(general)` calls
`_clear_battery_mode_param()` which may reset the discharge current.
The automation sets `battery_discharge_current=0` AFTER a 2-second delay
following the mode change to ensure it sticks.

**CHARGE and DISCHARGE** both restore `battery_discharge_current=25` and
`battery_charge_current=25` as their first actions, clearing any idle block.

### Other Parameters

- **Deadband:** 200W — prevents mode switching on small values
- **Power %:** `|p_batt| / inverter_rated_w (10000W) × 100`, clamped 5–100%.
  GoodWe firmware interprets eco_mode_power as % of inverter rated power,
  NOT battery max. This allows EMHASS to set a real watt-level discharge cap
  (e.g. 2000W forecast → 20% → inverter limits battery to 2000W, grid covers
  the rest of house consumption).
- **Idle streak:** Requires 2 consecutive idle decisions before switching
  (prevents staccato mode changes from MPC re-solves)
- **Grid export:** Set to 0W on discharge (prevents battery→grid spill). Left
  unrestricted (10 kW cap) on charge and idle so PV surplus can reach the grid
  when the battery is full.

### Failsafe

If `p_batt_forecast` is stale >20 min, the failsafe automation switches to `general` mode + `battery_discharge_current=0` (same as idle). Battery holds SOC, PV charges it, house draws from grid. Checks every 7 minutes.

### GoodWe Availability Cascade (March 2026 Fix)

The `goodwe` Python library has a single `_consecutive_failures_count` on the
`Inverter` object, shared by **reads** (coordinator polling every 5s) AND
**writes** (automation register writes). The coordinator tolerates 2 consecutive
failures before raising `UpdateFailed` and marking sensors unavailable.

**The cascade:** A transient UDP failure → write retries (10 × 1s timeout =
10s per write, lock held) → counter incremented by writes past 3 → coordinator
immediately fails on next poll → sensors go unavailable → MPC Loop condition
fails (requires `goodwe_battery_state_of_charge` is_number) → no EMHASS calls
→ heartbeat goes stale → Failsafe fires → 5+ writes with `continue_on_error`
(~50s of hammering every 7 min) → counter stays high → recovery impossible.

**Fix:** Both Battery Control and Failsafe now have a guard condition:

```yaml
- condition: template
  value_template: >-
    {{ states('sensor.goodwe_battery_state_of_charge') not in ['unknown', 'unavailable'] }}
```

When the inverter is unreachable, automations skip writes entirely, letting the
coordinator poll uncontested. The counter resets on the first successful read →
sensors recover → automations resume. The inverter continues in its last-set
mode during the outage (safe: it defaults to self-use behavior).

## Testing Changes

**Config changes** (`emhass-config.yaml`) require a pod restart:

```bash
kubectl rollout restart -n emhass deploy/emhass
sleep 30 && kubectl get pod -n emhass
```

**Code changes** (Python source in the EMHASS fork) — see the
"Deploying & Testing Code Changes in the Container" section above for
the full workflow (image rebuild vs. live `kubectl cp` + worker reload).

**HA-side changes** (rest_command, automations) use the HA reload
workflow — see the Home Assistant SKILL.md.

## Price Forecast Sensors

Buy/sell price forecasts are template sensors in HA `configuration.yaml`:

- `sensor.emhass_buy_price_forecast` — Tibber + Nordpool + energiskatt
- `sensor.emhass_sell_price_forecast` — Nordpool + grid compensation
- Optional 3rd-day tail prices controlled by `input_boolean.emhass_send_tail_prices`
- Prices are 15-min interpolated from hourly Nordpool data
