# systemd user unit — ecosystem SSHFS (single switch)

Mounts every entry in `config/sshfs_mounts.yaml` with one service.

## 1. Edit mount definitions

- Install **sshfs** (and FUSE) on the machine that runs the unit, e.g. `sudo apt install sshfs fuse3`.
- Copy/adjust `config/sshfs_mounts.yaml` in the repo (remotes must match your servers).
- `sshfs_options` may use `${HOME}` for `IdentityFile`.
- If `sshfs` is still not found under systemd, set `SSHFS_BIN` in the unit file to the full path (see commented example in the `.service` file).

## 2. Install the unit

The unit uses **`Type=oneshot` + `RemainAfterExit=yes`**: `ExecStart` runs `sshfs` (which detaches) and exits; the unit stays **active** until you `stop`, so systemd does not treat “start finished” like a crashed `Type=simple` service (which used to trigger bad `ExecStop` behavior).

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/user/ai-audit-ecosystem-sshfs.service ~/.config/systemd/user/
# Edit the file: fix WorkingDirectory and %h/.../ai-code-auditor paths to your clone.
systemctl --user daemon-reload
systemctl --user enable --now ai-audit-ecosystem-sshfs.service
```

Optional (mounts before login / over SSH):

```bash
loginctl enable-linger "$USER"
```

## 3. Manual commands

From repo root:

```bash
python3 scripts/mount_ecosystem_sshfs.py status
python3 scripts/mount_ecosystem_sshfs.py stop
python3 scripts/mount_ecosystem_sshfs.py start   # runs sshfs for each path that is not yet a mountpoint
```

## 4. Remove per-repo units

If you had `sshfs-gothmog.service` etc., disable them so they don’t fight this unit:

```bash
systemctl --user disable --now sshfs-gothmog.service
```

---

## Optional: daily ecosystem audit via Docker

The **`auditor`** compose service is a **batch** job (runs once, exits). To run it on a schedule while **`audit-api`** stays up separately:

```bash
# API (continuous)
docker compose up -d audit-api

# Timer triggers one-off container (see files below)
```

Copy and edit paths, then:

```bash
cp contrib/systemd/user/ai-audit-docker-ecosystem.service ~/.config/systemd/user/
cp contrib/systemd/user/ai-audit-docker-ecosystem.timer ~/.config/systemd/user/
# Edit WorkingDirectory in the .service file if needed.
systemctl --user daemon-reload
systemctl --user enable --now ai-audit-docker-ecosystem.timer
systemctl --user list-timers | grep ai-audit-docker
```

`loginctl enable-linger "$USER"` if the timer should fire without an interactive login.
