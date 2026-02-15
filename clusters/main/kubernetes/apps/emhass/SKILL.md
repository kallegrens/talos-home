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
**Image:** `ghcr.io/davidusb-geek/emhass:v0.16.2`

## Documenation

The full documentation directory for EMHASS can be found at `emhass/resources/emhass-docs/`. This is the same as found on their website. Reference this before answering questions about EMHASS features, config options, or architecture. Never guess or make assumptions about how EMHASS works without checking the docs first.

## Key Files

| File | Purpose |
|---|---|
| `app/helm-release.yaml` | Helm values (image, ingress, persistence, mounts) |
| `app/emhass-config.yaml` | ConfigMap → mounted at `/share/config.json` |
| `app/emhass-secrets.secret.yaml` | SOPS Secret → mounted at `/app/secrets_emhass.yaml` |

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

- 15-min optimization time step, 48-slot default prediction horizon
- Open-Meteo weather forecasting (Gothenburg area)
- Battery: 14.2 kWh capacity, 5050W max charge/discharge
- PV: 16 modules, 2 strings, 10kW inverter, 15° tilt, 125° azimuth
- Reads HA sensors via long-lived token in `secrets_emhass.yaml`

## ⚠️ Logs Are Extremely Verbose

**Never run `kubectl logs -n emhass deploy/emhass` without `--tail=N`.**
EMHASS logs every optimization step at DEBUG level — thousands of lines per
cycle. Always use:

```bash
kubectl logs -n emhass deploy/emhass --tail=30
```

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

If `p_batt_forecast` is stale >20 min, the failsafe automation switches to
`general` mode + `battery_discharge_current=0` (same as idle). Battery holds
SOC, PV charges it, house draws from grid. Checks every 7 minutes.

## Testing Changes

Config changes (`emhass-config.yaml`) require a pod restart:

```bash
kubectl rollout restart -n emhass deploy/emhass
sleep 30 && kubectl get pod -n emhass
```

HA-side changes (rest_command, automations) use the HA reload workflow — see
the Home Assistant SKILL.md.

## Price Forecast Sensors

Buy/sell price forecasts are template sensors in HA `configuration.yaml`:

- `sensor.emhass_buy_price_forecast` — Tibber + Nordpool + energiskatt
- `sensor.emhass_sell_price_forecast` — Nordpool + grid compensation
- Optional 3rd-day tail prices controlled by `input_boolean.emhass_send_tail_prices`
- Prices are 15-min interpolated from hourly Nordpool data
