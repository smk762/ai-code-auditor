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

## Overnight timers (rest + ecosystem)

Preferred install (idempotent):

```bash
bash scripts/install_overnight_timers.sh
systemctl --user list-timers | grep ai-audit
```

| Timer | When (local) | What |
|-------|--------------|------|
| `ai-audit-rest.timer` | 00:30 | Night conductor `rest-run` (host venv) |
| `ai-audit-docker-ecosystem.timer` | 02:00 | Full ecosystem audit via `docker compose run auditor` |
| `ai-audit-merge-poll.timer` | hourly | GitHub API `dev` tip poll → pack/RAG refresh |

`loginctl enable-linger "$USER"` so timers fire without an interactive login.

Manual copy:

```bash
cp contrib/systemd/user/ai-audit-rest.{service,timer} ~/.config/systemd/user/
cp contrib/systemd/user/ai-audit-docker-ecosystem.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-audit-rest.timer ai-audit-docker-ecosystem.timer
```

Between nights, merges to **`dev`** can refresh packs/RAG via the lightweight merge hook
(`POST /audit/hooks/github` / `ai-audit merge-refresh`) — see
[`contrib/github/merge-refresh-webhook.md`](../../github/merge-refresh-webhook.md).
