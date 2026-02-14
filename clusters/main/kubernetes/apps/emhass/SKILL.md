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

## Key Files

| File | Purpose |
|---|---|
| `app/helm-release.yaml` | Helm values (image, ingress, persistence, mounts) |
| `app/emhass-config.yaml` | ConfigMap → mounted at `/share/config.json` |
| `app/emhass-secrets.secret.yaml` | SOPS Secret → mounted at `/app/secrets_emhass.yaml` |

## Configuration

Config lives in `emhass-config.yaml` (ConfigMap). Key settings:
- 15-min optimization time step, 48-slot default prediction horizon
- Open-Meteo weather forecasting (Gothenburg area)
- Battery: 14.2 kWh capacity, 4800W max charge/discharge
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

1. HA automation "EMHASS MPC Loop" fires every **7 minutes** (coprime with
   15-min price slots to prevent systematic alignment flip-flop)
2. Calls `rest_command.emhass_mpc_optim` with dynamic payload:
   - Current SOC, buy/sell price forecasts, prediction horizon
   - Snow override (zeroes PV forecast if panels are snow-covered)
3. EMHASS solves MPC and publishes via `continual_publish` to HA sensors:
   - `sensor.p_batt_forecast` — battery power schedule
   - `sensor.soc_batt_forecast` — SOC trajectory
   - `sensor.p_pv_forecast` — PV forecast used
4. HA automation "EMHASS Battery Control" triggers on `p_batt_forecast` change
5. Uses 4-slot lookahead to determine dominant intent (charge/discharge/idle)
6. Translates to GoodWe eco-mode: `eco_charge`, `eco_discharge`, or idle hold
7. Grid export limit set to 0W on discharge (prevents battery→grid spill)
8. Failsafe holds battery if forecast stale >20 min

## Battery Control Details

- **Deadband:** 200W — prevents mode switching on small values
- **Lookahead:** 4 slots (~1 hour) — smooths staccato optimizer output
- **Power %:** Based on peak of upcoming slots in dominant direction
- **Idle mode:** `eco_charge` with SOC target 5% (battery always above 5%)
- **Never uses `general` mode** — it means self-use which discharges uncontrolled

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
