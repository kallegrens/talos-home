# Agent Instructions

> Repository-level context for AI coding agents working on **talos-home**.
> See <https://agentskills.io/home> for the Agent Skills initiative.

## Repository Overview

GitOps-managed Kubernetes home-lab running on a single **Talos Linux** node.
**FluxCD** reconciles this repo into the cluster. All changes go through git —
commit, push, and Flux picks them up automatically.

### Key Paths

| Path | Purpose |
|---|---|
| `clusters/main/kubernetes/apps/` | Application deployments (Flux Kustomizations + Helm) |
| `clusters/main/kubernetes/core/` | Core infra (blocky, metallb-config, clusterissuer) |
| `clusters/main/kubernetes/system/` | System services (cert-manager, longhorn, prometheus, etc.) |
| `clusters/main/kubernetes/flux-system/` | Flux bootstrap & cluster config |
| `clusters/main/talos/` | Talos machine config (talconfig.yaml, patches) |
| `repositories/` | HelmRepository / GitRepository / OCIRepository sources |

### Encryption (SOPS + age)

Files matching patterns in `.sops.yaml` are encrypted with age. The pre-commit
hook (`clustertool precommit`) enforces this — **commits will fail** if a
secret file is unencrypted. Encrypted file patterns:

- `*.secret.yaml` under `clusters/*/kubernetes/`
- `*.values.yaml` under `clusters/*/kubernetes/`
- `clusterenv.yaml`, `talsecret.yaml`

The age key lives at `age.agekey` (repo root). Never commit plaintext secrets.

### Pre-commit Hooks

Configured in `.pre-commit-config.yaml`. Runs automatically on `git commit`:
- merge-conflict check, trailing whitespace, end-of-file fixer
- yamllint (excludes SOPS-encrypted files and HA `configuration.yaml` which uses `!include`)
- zizmor (GitHub Actions security linter)
- **clustertool encrypt** — ensures all secret files are SOPS-encrypted

---

## Deployment Workflow

### Standard Change Process

1. **Edit** the file(s) in the repo
2. **Deploy to test** — use `kubectl cp` to push config into the running pod
3. **Validate** — reload the affected service, verify the change works
4. **Test failure modes** — if you added a toggle, test both states; if you added a condition, test when it's true AND false
5. **Commit** — only after live validation succeeds
6. **Push** — Flux will reconcile (typically within 5 minutes)

### FluxCD Reconciliation

Flux watches the `main` branch of `ssh://git@github.com/kallegrens/talos-home.git`.
To force immediate reconciliation:

```bash
flux reconcile kustomization cluster --with-source
```

The top-level Kustomization (`cluster`) cascades into child Kustomizations.
Each app has a `ks.yaml` (Flux Kustomization) pointing to its `app/` folder.
Variable substitution (`${DOMAIN_0}`, `${VIP}`, etc.) comes from the
`cluster-config` ConfigMap in `flux-system`.

### Useful kubectl Commands

```bash
# Check Flux status
flux get kustomizations
flux get helmreleases -A

# Watch pod status
kubectl get pods -n <namespace> -w

# Check logs (use --tail to avoid flooding context)
kubectl logs -n <namespace> deploy/<name> --tail=50

# Copy file into a pod
kubectl cp <local-path> <namespace>/<pod>:<remote-path> -c <container>
```

---

## Home Assistant

**Namespace:** `home-assistant`
**Pod pattern:** `home-assistant-<replicaset>-<hash>`
**Container names:** `home-assistant` (main), `home-assistant-codeserver` (sidecar)
**Config path inside pod:** `/config/`
**Chart:** TrueCharts home-assistant (hostNetwork: true)
**Database:** CloudNativePG PostgreSQL (pods `home-assistant-cnpg-main-{1,2}`)

### Configuration Files

| File | Deployed by | Editable via |
|---|---|---|
| `configuration.yaml` | code-server / kubectl cp | Reload via API |
| `automations.yaml` | code-server / kubectl cp | Reload via API |
| `scripts.yaml`, `scenes.yaml`, `groups.yaml` | code-server | Reload via API |

These files are **version-controlled in this repo** but are NOT deployed by
Flux (Flux only deploys the Helm chart). They are edited either through the
code-server addon or by `kubectl cp` for testing.

**⚠️ `configuration.yaml` uses `!include` tags** which standard YAML parsers
reject. The yamllint pre-commit hook is configured to skip this file.

### HA API Access

Home Assistant requires a long-lived access token for API calls. Get it from
the EMHASS secrets:

```bash
kubectl exec -n emhass deploy/emhass -- cat /app/secrets_emhass.yaml
# Look for the `long_lived_token` field
```

Then use it for API calls from inside the cluster:

```bash
HA_URL="http://home-assistant.home-assistant.svc.cluster.local:8123"
HA_TOKEN="<token from above>"

# Reload input_booleans
kubectl exec -n home-assistant <pod> -c home-assistant -- \
  curl -s -X POST "$HA_URL/api/services/input_boolean/reload" \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json"

# Reload template sensors
kubectl exec -n home-assistant <pod> -c home-assistant -- \
  curl -s -X POST "$HA_URL/api/services/template/reload" \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json"

# Reload automations
kubectl exec -n home-assistant <pod> -c home-assistant -- \
  curl -s -X POST "$HA_URL/api/services/automation/reload" \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json"

# Check entity state
kubectl exec -n home-assistant <pod> -c home-assistant -- \
  curl -s "$HA_URL/api/states/<entity_id>" \
  -H "Authorization: Bearer $HA_TOKEN"
```

### HA Reload Gotcha: Trigger-Based Template Sensors

Trigger-based template sensors (the ones with a `trigger:` block in
`configuration.yaml`) **only re-evaluate when their triggers fire**.
`template/reload` re-registers them but does NOT fire the triggers.
`homeassistant.update_entity` also does NOT work on them.

**To force re-evaluation:** Either wait for the next time_pattern trigger, or
add the relevant entity as a trigger for the sensor (e.g. adding
`input_boolean.x` as a `trigger: state` so toggling it fires the template).

### Testing HA Changes

1. `kubectl cp` the file into the pod
2. Call the appropriate reload API endpoint
3. Verify with `GET /api/states/<entity_id>` — check both `state` and `attributes`
4. If you added a toggle, **test both ON and OFF states** and verify the output changes
5. Only commit after both states are confirmed working

---

## EMHASS

**Namespace:** `emhass`
**Pod pattern:** `emhass-<replicaset>-<hash>`
**Service:** `emhass.emhass.svc.cluster.local:5000`
**Chart:** bjw-s app-template v4.6.2
**Image:** `ghcr.io/davidusb-geek/emhass:v0.16.2`
**Internal ingress:** `emhass.${DOMAIN_0}`

### Key Files

| File | Purpose |
|---|---|
| `apps/emhass/app/helm-release.yaml` | Helm values (image, ingress, persistence, mounts) |
| `apps/emhass/app/emhass-config.yaml` | ConfigMap → mounted at `/share/config.json` |
| `apps/emhass/app/emhass-secrets.secret.yaml` | SOPS Secret → mounted at `/app/secrets_emhass.yaml` |

### Configuration

EMHASS config is in `emhass-config.yaml` (ConfigMap). Key settings:
- 15-min optimization time step, 48-slot prediction horizon (default)
- Open-Meteo weather forecasting (lat/lon for Gothenburg area)
- Battery: 14.2 kWh capacity, 4800W max charge/discharge
- PV: 16 modules, 2 strings, 10kW inverter, 15° tilt, 125° azimuth
- Sensors read from HA via the long-lived token

### ⚠️ EMHASS Logs Are Extremely Verbose

**Never run `kubectl logs -n emhass deploy/emhass` without `--tail=N`.**
EMHASS logs every optimization step at DEBUG level and will flood your context
window with thousands of lines. Always use:

```bash
kubectl logs -n emhass deploy/emhass --tail=30
```

### MPC Optimization Flow

1. HA automation "EMHASS MPC Loop" fires every 7 minutes
2. Calls `rest_command.emhass_mpc_optim` with dynamic payload (SOC, prices, horizon)
3. EMHASS solves and publishes results via `continual_publish` to HA sensors
4. HA automation "EMHASS Battery Control" triggers on `sensor.p_batt_forecast` state change
5. Translates forecast into GoodWe eco-mode commands (charge/discharge/idle)
6. Failsafe automation holds battery if forecast goes stale (>20 min)

### Testing EMHASS Changes

For config changes (`emhass-config.yaml`), you need to restart the pod since
it's a ConfigMap mount:

```bash
kubectl rollout restart -n emhass deploy/emhass
# Wait and check
sleep 30 && kubectl get pod -n emhass
```

For HA-side changes (rest_command payload, automations), use the HA reload
workflow above.

---

## Common Patterns

### Namespaces to Know

| Namespace | Contains |
|---|---|
| `home-assistant` | HA + CNPG PostgreSQL |
| `emhass` | EMHASS energy optimizer |
| `flux-system` | Flux controllers + cluster config |
| `kube-system` | Cilium, metrics-server, descheduler, NFD |
| `networking` | nginx-internal, nginx-external ingress |
| `system` | cert-manager, longhorn, metallb, prometheus-stack |

### Ingress Classes

- `internal` — accessible only within the local network
- `external` — exposed to the internet via Cloudflare

### Storage

- **Longhorn** — default StorageClass for PVCs
- **OpenEBS** — also available

### Variable Substitution

Flux `postBuild` substitutes `${VAR}` placeholders from the `cluster-config`
ConfigMap. Common variables: `${DOMAIN_0}` (domain name), `${VIP}` (cluster
VIP), `${MASTER1IP_IP}`, `${PODNET}`, `${SVCNET}`.
