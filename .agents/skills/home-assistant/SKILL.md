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
| `app/configuration.yaml` | Main HA config: input_booleans, rest_commands, template sensors |
| `app/automations.yaml` | All automations (Tibber, EMHASS MPC, battery control, snow) |
| `app/dashboards/ev-charging.yaml` | YAML-mode Lovelace dashboard mounted into `/config/dashboards/` |
| `scripts.yaml`, `scenes.yaml`, `groups.yaml` | Standard HA includes |

`app/configuration.yaml`, `app/automations.yaml`, and
`app/dashboards/ev-charging.yaml`
are **version-controlled in the repo**, rendered into a ConfigMap by Flux,
and mounted into `/config/` in the Home Assistant pod.

These mounts use `subPath`, so ConfigMap updates do **not** refresh inside an
already-running pod. **Flux reconciliation alone is not enough** after editing
those repo-backed files. You must do a manual rollout restart every time:

```bash
flux reconcile source git cluster -n flux-system
flux reconcile kustomization home-assistant -n flux-system --with-source
kubectl rollout restart deployment/home-assistant -n home-assistant
kubectl rollout status deployment/home-assistant -n home-assistant --timeout=180s
```

Those mounted repo-backed files are effectively **read-only** inside the pod.
Do not rely on code-server edits or `kubectl cp` to `/config/configuration.yaml`,
`/config/automations.yaml`, or the mounted dashboard path — they will not be the
durable source of truth.

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

Note that the sensor being **stale** and the sensor just getting the **same** data consecutively is not the same thing. When Home Assistant receives a sensor where the value is not updated it does not make a new record.

## Testing Workflow

1. For repo-backed files, edit the file in git, then reconcile Flux and manually roll out `deployment/home-assistant`.
2. For PVC-backed files that are not mounted from the repo, `kubectl cp` or code-server edits are still valid.
3. Call the appropriate reload API endpoint when a full rollout is not required for the file you changed.
4. Verify with `GET /api/states/<entity_id>` — check both `state` and `attributes`.
5. **Test both states** if you added a toggle or condition.
6. Only commit after live validation succeeds.

## Notification Gotcha

Notifications are not accessible through the REST API. They can only be viewed in the HA UI or using WebSocket.
