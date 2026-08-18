---
name: automatic-ripping-machine
description: >-
  Automatic Ripping Machine (ARM) on Kubernetes/Talos. Covers optical drive
  passthrough, udev auto-triggering, abcde/cdparanoia audio CD ripping,
  runtime ARM patches, and Flux/Helm rollout debugging. Load this skill when
  editing or debugging the automatic-ripping-machine app.
---

# Automatic Ripping Machine

**Namespace:** `automatic-ripping-machine`
**Deployment:** `deployment/automatic-ripping-machine`
**Chart:** TrueCharts `automatic-ripping-machine@7.3.3`
**Image:** `automaticrippingmachine/automatic-ripping-machine:2.23.2`
**Drive:** `MATSHITA BD-MLT UJ265` USB optical drive
**Stable pod device paths:** `/host-dev/optical`, `/host-dev/optical-sg`

## Key Files

| File | Purpose |
|---|---|
| `app/helm-release.yaml` | Helm values, host `/dev` mount, ConfigMap mounts, init patch wiring |
| `app/udev-rules.yaml` | ARM udev rules and `/etc/udev/arm-auto-trigger.sh` |
| `app/main-py-patcher.yaml` | Init container patch for ARM `main.py` and `utils.py` |
| `app/abcde-conf.yaml` | `abcde`/`cdparanoia` audio CD config |
| `ks.yaml` | Flux Kustomization for this app |

## Architecture Gotchas

- `/dev/srN` numbering is unstable after media changes or drive re-enumeration.
  The pod should rip through `/host-dev/optical` and `/host-dev/optical-sg`,
  which point at the live host device nodes.
- Pod `/dev` optical nodes can be stale after sr0/sr1 changes. Treat
  `/host-dev/*` as the source of truth for the live device.
- The upstream `51-docker-arm.rules` runs before `60-cdrom_id`, so it may not
  see `ID_CDROM_MEDIA`. This repo shadows that rule with an empty file and runs
  the real media-dependent trigger from `61-docker-arm.rules`.
- Do not mount read-only ConfigMap files under `/opt/arm/scripts`. ARM startup
  chowns files there and can crashloop on read-only mounts. Mount helper scripts
  under `/etc/udev` and invoke them from udev rules.
- The trigger script uses `set +u` because udev does not always populate every
  environment variable. It receives `%k` as the device name and logs to
  `/home/arm/logs/udev-arm-trigger.log`.
- The trigger script wraps ARM with `flock` on `/tmp/arm-optical.lock` so one
  media event does not start duplicate rips.
- The host `/dev/optical` symlink can move between `sr0` and `sr1` while the
  Kubernetes device-plugin allocation inside the pod remains stale. The
  `arm-optical-watch.sh` poller checks `/host-dev/optical`, mirrors it into the
  pod's expected ARM device path, and launches the same trigger script if udev
  misses the insert. The mirror prefers real `/dev` device nodes with the live
  host major/minor so ARM's eject path sees `/dev/srN`, not `/host-dev/srN`.
  On watcher startup it records already-present media as handled so a rollout
  with a CD still inserted does not duplicate-rip it.
- The init patcher resolves ARM drive rows by device identity (`st_rdev`) and
  patches audio ripping to run `abcde -f -d /host-dev/optical` with
  `CDPARANOIA_OPTS="-g /host-dev/optical-sg"`.
- Stale `/home/arm/abcde.*` work directories can make a later rip stall. The
  runtime `utils.py` patch removes them before starting `abcde`.

## First Checks

Use bounded logs. ARM and cluster event streams can get noisy quickly.

```bash
flux get kustomization automatic-ripping-machine -n flux-system
flux get helmrelease automatic-ripping-machine -n automatic-ripping-machine
kubectl get pod -n automatic-ripping-machine -o wide
kubectl rollout status deployment/automatic-ripping-machine \
  -n automatic-ripping-machine --timeout=120s
kubectl logs -n automatic-ripping-machine deployment/automatic-ripping-machine \
  --tail=120 --timestamps
```

Verify the live udev rules, trigger script, and device links in the pod:

```bash
POD=$(kubectl get pod -n automatic-ripping-machine \
  -l app.kubernetes.io/name=automatic-ripping-machine \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc '
  echo === rules ===
  cat /etc/udev/rules.d/51-docker-arm.rules
  echo ---
  cat /etc/udev/rules.d/61-docker-arm.rules
  echo === trigger ===
  ls -l /etc/udev/arm-auto-trigger.sh
  sed -n "1,120p" /etc/udev/arm-auto-trigger.sh
  echo === watcher ===
  ls -l /etc/udev/arm-optical-watch.sh
  tail -n 80 /home/arm/logs/arm-optical-watch.log 2>/dev/null || true
  echo === devices ===
  ls -l /host-dev/optical /host-dev/optical-sg /dev/optical /dev/optical-sg 2>&1 || true
  echo === cdrom props ===
  udevadm info --query=property --name=/dev/sr1 2>/dev/null | grep -E "ID_CDROM|ID_VENDOR|ID_MODEL|DEVNAME" || true
'
```

`udevadm test --action=change` is useful to see which rules would match, but
it does not prove that a real kernel/udev media event fired. Use live monitors
for insert/eject debugging.

## Insert Debugging Monitors

When debugging a real insert, start only the watchers you need and stop them
after the rip is validated.

```bash
PROBE=/tmp/arm-insert-probe
mkdir -p "$PROBE"
POD=$(kubectl get pod -n automatic-ripping-machine \
  -l app.kubernetes.io/name=automatic-ripping-machine \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc \
  'udevadm monitor --kernel --udev --property' \
  | awk '/(^KERNEL|^UDEV|sr[0-9]|sg[0-9]|MATSHITA|BD-MLT|ID_CDROM|DEVNAME=|DEVPATH=|ACTION=|SUBSYSTEM=|SEQNUM=)/ { print strftime("%Y-%m-%dT%H:%M:%S%z"), $0; fflush(); }' \
  | tee "$PROBE/udev-optical.log"
```

```bash
kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc '
  touch /home/arm/logs/udev-arm-trigger.log /home/arm/logs/arm.log /home/arm/logs/music_cd.log
  tail -n 0 -F /home/arm/logs/udev-arm-trigger.log /home/arm/logs/arm.log /home/arm/logs/music_cd*.log 2>&1
' | awk '{ print strftime("%Y-%m-%dT%H:%M:%S%z"), $0; fflush(); }' \
  | tee "$PROBE/arm-file-logs.log"
```

```bash
kubectl get events -n automatic-ripping-machine --watch --output-watch-events \
  | tee "$PROBE/k8s-events.log"
```

For host kernel messages, prefer a focused Talos `dmesg` grep for `sr1`,
`MATSHITA`, `cdrom`, `UDF`, `isofs`, `scsi_read`, and `No such device`.

## Manual Trigger Test

Only do this when no rip is already active. It bypasses the udev match and
tests the ARM wrapper directly.

```bash
kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc '
  ps -eo pid,ppid,user,stat,etime,args | grep -E "[d]ocker_arm|[m]ain.py -d|[a]bcde|[c]dparanoia" || true
  /bin/flock -n /tmp/arm-optical.lock /sbin/setuser arm \
    /opt/arm/scripts/docker/docker_arm_wrapper.sh sr1
'
```

## Validating A Rip

Do not rely on container logs alone. Validate through the ARM database, the
job log, output files, and absence of known device-loss signatures.

```bash
kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc '
  echo === db ===
  python3 - <<"PY"
import sqlite3
conn = sqlite3.connect("/home/arm/db/arm.db")
for row in conn.execute("select job_id,status,devpath,logfile,start_time,stop_time from job order by job_id desc limit 5"):
    print(row)
print(conn.execute("select * from system_drives").fetchall())
PY

  echo === active rip processes ===
  ps -eo pid,ppid,user,stat,etime,args | grep -E "[d]ocker_arm|[m]ain.py -d|[a]bcde|[c]dparanoia|[o]pusenc" || true

  echo === recent output ===
  find /home/arm/music -type f -mmin -120 -printf "%TY-%Tm-%Td %TH:%TM %s %p\n" 2>/dev/null | sort | tail -40
'
```

For the current job log, check for completion and old failure signatures:

```bash
LOG=music_cd_177895587491.log
kubectl exec -n automatic-ripping-machine "$POD" -- bash -lc "
  grep -n -E 'Finished|abcde call successful|Releasing current job' /home/arm/logs/$LOG || true
  grep -n -Ei 'scsi_read|No such device|cdparanoia.*failed|abcde.*failed' /home/arm/logs/$LOG || true
"
```

Success signals for an audio CD:

- ARM DB row has `status='success'` and a populated `stop_time`.
- The drive row in `system_drives` has released the current job.
- Job log includes `Finished.`, `abcde call successful`, and
  `Releasing current job from drive`.
- Expected `.opus` files and playlist exist under `/home/arm/music`.
- No `scsi_read`, `No such device`, `cdparanoia.*failed`, or `abcde.*failed`
  appears in the job log.

## Noisy But Usually Benign

These can appear for unknown audio CDs and are not by themselves fatal:

- `findmnt --json /dev/srN` returning no output.
- `mount --source /dev/srN` failing with wrong filesystem type.
- Kernel `Illegal mode for this track`, `I/O error, dev srN`, `UDF-fs`, or
  `isofs_fill_super` messages during filesystem probing.
- MusicBrainz `HTTP Error 404: Not Found`, which leaves metadata as
  `Unknown Artist` / `Unknown Album`.
- Repeated `Failed to calculate processing time - Resetting to now` in ARM
  container logs.

Treat `scsi_read`, `No such device`, `cdparanoia.*failed`, `abcde.*failed`, a
stuck current job, missing output files, or DB `fail` as real regression
signals.

## Deploying Changes

Edit the GitOps manifests, then let Flux apply them.

```bash
flux reconcile source git cluster -n flux-system
flux reconcile kustomization automatic-ripping-machine -n flux-system --with-source
flux reconcile helmrelease automatic-ripping-machine -n automatic-ripping-machine
kubectl rollout status deployment/automatic-ripping-machine \
  -n automatic-ripping-machine --timeout=180s
```

After rollout, re-check the live mounted rules and patched files inside the
new pod. If the init patcher fails, the pod may crash before ARM starts; check
the init container logs first.

```bash
kubectl logs -n automatic-ripping-machine "$POD" -c patch-main-py --tail=80
```
