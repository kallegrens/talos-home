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

---

## Deployment Workflow

1. **Edit** the file(s) in the repo
2. **Deploy to test** — use `kubectl cp` to push config into the running pod
3. **Validate** — reload the affected service, verify the change works
4. **Test failure modes** — if you added a toggle, test both states; if you added a condition, test when it's true AND false
5. **Commit** — only after live validation succeeds
6. **Push** — Flux will reconcile (typically within 5 minutes)

### FluxCD Reconciliation

Each app has a `ks.yaml` (Flux Kustomization) pointing to its `app/` folder.
Variable substitution (`${DOMAIN_0}`, `${VIP}`, etc.) comes from the
`cluster-config` ConfigMap in `flux-system`. Force-reconcile:

```bash
flux reconcile kustomization cluster --with-source
```

### Context Window Hygiene

Always use `--tail=N` with `kubectl logs`. Some services (especially EMHASS)
produce thousands of DEBUG lines per optimization cycle.

---

## Skills

Domain-specific knowledge lives in `SKILL.md` files so it's only loaded
when relevant, not in every prompt:

| Skill | Path | When to load |
|---|---|---|
| Home Assistant | `clusters/main/kubernetes/apps/home-assistant/SKILL.md` | Editing HA config, automations, template sensors |
| EMHASS | `clusters/main/kubernetes/apps/emhass/SKILL.md` | Editing EMHASS config, MPC optimization, battery control |
