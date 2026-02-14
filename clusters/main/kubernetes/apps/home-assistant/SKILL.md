---
name: home-assistant
description: >-
  Home Assistant deployment on Kubernetes via TrueCharts Helm chart.
  Covers HA configuration files, automations, template sensors, API
  access, and the testing/reload workflow. Load this skill when editing
  configuration.yaml, automations.yaml, or any HA template sensor.
---

# Home Assistant

**Namespace:** `home-assistant`
**Pod pattern:** `home-assistant-<replicaset>-<hash>`
**Container names:** `home-assistant` (main), `home-assistant-codeserver` (sidecar)
**Config path inside pod:** `/config/`
**Chart:** TrueCharts home-assistant (hostNetwork: true)
**Database:** CloudNativePG PostgreSQL (pods `home-assistant-cnpg-main-{1,2}`)

## Configuration Files

| File | Purpose |
|---|---|
| `configuration.yaml` | Main HA config: input_booleans, rest_commands, template sensors |
| `automations.yaml` | All automations (Tibber, EMHASS MPC, battery control, snow) |
| `scripts.yaml`, `scenes.yaml`, `groups.yaml` | Standard HA includes |

These files are **version-controlled in the repo** but NOT deployed by Flux.
Flux only deploys the Helm chart. Config files are edited via code-server
addon or `kubectl cp` for testing.

**⚠️ `configuration.yaml` uses `!include` tags** — standard YAML parsers
reject these. The yamllint pre-commit hook excludes this file.

## API Access

HA requires a long-lived access token. Get it from EMHASS:

```bash
kubectl exec -n emhass deploy/emhass -- cat /app/secrets_emhass.yaml
# → long_lived_token field
```

Use from inside the cluster:

```bash
HA_URL="http://home-assistant.home-assistant.svc.cluster.local:8123"

# Reload input_booleans / templates / automations
curl -s -X POST "$HA_URL/api/services/input_boolean/reload" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"

curl -s -X POST "$HA_URL/api/services/template/reload" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"

curl -s -X POST "$HA_URL/api/services/automation/reload" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"

# Check entity state
curl -s "$HA_URL/api/states/<entity_id>" \
  -H "Authorization: Bearer $TOKEN"

# Call a service (e.g. toggle)
curl -s -X POST "$HA_URL/api/services/input_boolean/turn_off" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"input_boolean.example"}'
```

Run these via `kubectl exec -n home-assistant <pod> -c home-assistant -- ...`

## Trigger-Based Template Sensor Gotcha

Trigger-based template sensors (with a `trigger:` block in `configuration.yaml`)
**only re-evaluate when their triggers fire**. `template/reload` re-registers
them but does NOT fire the triggers. `homeassistant.update_entity` also does
NOT work.

**To force re-evaluation:** add the relevant entity as a trigger for the
sensor (e.g. `input_boolean.x` as `trigger: state` so toggling it fires the
template), or wait for the next `time_pattern` trigger.

## Testing Workflow

1. `kubectl cp` the edited file into the pod at `/config/`
2. Call the appropriate reload API endpoint
3. Verify with `GET /api/states/<entity_id>` — check both `state` and `attributes`
4. **Test both states** if you added a toggle or condition
5. Only commit after live validation succeeds
