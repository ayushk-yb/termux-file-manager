# TermuxFM

A web-based file manager that runs directly in **native Termux on Android**, with
**zero dependencies outside the Python standard library**.

Built for managing the storage shared by Transmission, aria2 and Jellyfin on an
always-on Android home server — browse, upload, download, rename, move, copy,
delete, search, preview and ZIP, from any browser on your LAN.

```
git clone <this repo>
cd FileManagerService
./scripts/install-termux.sh
sv up filemanager
```

Then open `http://<your-phone-lan-ip>:8080`.

---

## Why pure-stdlib Python, and why prebuilt binaries fail

Android is **not** a normal Linux userspace, and that is what breaks the usual
"download the linux-arm64 build" approach:

| Attempt | Result | Actual cause |
| --- | --- | --- |
| File Browser Quantum `linux-arm64` | `unexpected e_type: 2` | The binary is **ET_EXEC**. Android's linker only loads **ET_DYN** (PIE) executables, and the binary is linked against glibc — Android uses **Bionic**. |
| AmberDAV prebuilt `aarch64-linux` | not viable | Targets `aarch64-unknown-linux-musl`, not `aarch64-linux-android`. |
| Old PHP "Termux Manager" | rejected | Unmaintained since ~2020. |
| `http-share-termux` | installer failed | Downloads an opaque image; checksum step broken. |

The right correction is not "find a better binary" — it is to run something that
**cannot** carry a glibc assumption in the first place.

**Termux's `python` package is CPython compiled by the Termux project against
Bionic for `aarch64-linux-android`.** TermuxFM adds *nothing* to it:

- **No pip packages. No native extensions. No compile step.** Installation is a
  file copy, so there is nothing that can fail to build.
- Everything needed already ships in that interpreter: `http.server` +
  `socketserver.ThreadingHTTPServer`, `hashlib.pbkdf2_hmac`,
  `hmac.compare_digest`, `secrets`, `os.scandir`, `os.sendfile`, `shutil`,
  `zipfile`, `mimetypes`, `unicodedata`, `unittest`.
- **~28 MB RSS, ~0% CPU idle**, no database — filesystem metadata is the only
  state besides in-memory sessions. That figure is measured *after* a 200 MB
  upload and a range read 100 MB into the file, which is the point: transfers
  stream in 512 KB chunks and never land in memory.

### Rust was considered and rejected

Termux's `rust` package genuinely targets `aarch64-linux-android`, so a locally
built binary *would* be correct. It was still the wrong choice here:

- An axum/tokio build on the phone is a 15–40 minute, >1.5 GB-RSS compile that
  Android's low-memory killer can terminate mid-`rustc`, and every `git pull`
  repeats it.
- The crypto layer is the real trap: `ring` and `aws-lc-rs` are the most common
  `aarch64-linux-android` build failures in that stack.

**The honest trade-off:** Python's GIL and per-chunk interpreter overhead cap
transfers at roughly **30–80 MB/s**. Over Wi-Fi your radio is the bottleneck
well before the interpreter is, so for organising media this does not matter. If
you need wire speed for bulk transfers, use the SFTP you already have on 8022.

### Written for Android's FUSE storage, not generic POSIX

`~/storage/shared` is `/storage/emulated/0`, a FUSE/sdcardfs view. This is where
naive file managers break, and each point below is handled explicitly:

1. **`os.rename` fails with `EXDEV`** between Termux-internal storage and shared
   storage, so every move falls back to copy-then-unlink. Same-volume moves stay
   instant metadata renames.
2. **No symlinks, no hardlinks, no `chmod`/`chown`**; `os.utime` often fails.
   Metadata copying is best-effort and never aborts an operation.
3. **`" * / : < > ? \ |`**, control characters and trailing dots/spaces are
   rejected by the volume — validated up front so you get a clear `422` instead
   of a raw `EINVAL`.
4. **`stat` is not free on FUSE**, so a listing does exactly one `scandir` pass
   and reuses `DirEntry.stat()`'s cached result.
5. Upload temp files are created **in the destination directory**, so the final
   `os.replace` is same-volume: atomic and instant even for a 40 GB file.

---

## Features

**Browsing** · folder navigation · breadcrumbs · sort by name/size/date/type
(natural order, so `Season 2` precedes `Season 10`) · file size · modification
time · recursive folder size on demand · search (recursive, case-insensitive)

**Transfers** · multi-file upload · **folder upload** · drag-and-drop upload of
files *and* folders · per-file and aggregate progress · download · streaming ZIP
of any folder · HTTP Range support (so video seeking works)

**Managing** · create folder · rename · move · copy · delete · multi-select
(click, shift-click, ctrl-click, select-all) · clipboard model (mark → navigate →
paste) · drag a selection onto a folder row to move it · conflict handling
(fail / keep both / overwrite)

**Previews** · images · video and audio (seekable) · text files (first 1 MB)

**Interface** · works on desktop and phone · dark mode (follows the system, with
a manual toggle) · keyboard shortcuts (`/` search, `F2` rename, `Delete`,
`Ctrl+A`, `Backspace` up, `Esc`) · no build step, no CDN, works with the phone
offline from the internet

**Deliberately not included:** a trash bin (deletion is permanent — see below),
HTTPS, multi-user accounts, file editing, thumbnail generation, archive
extraction, any Jellyfin API integration.

> ### Deletion is permanent
> There is no trash directory. Deleting a folder requires **typing its exact
> name** to confirm, and the API refuses a delete that does not carry
> `"confirm": true`. Nothing is recoverable afterwards.

---

## Requirements

- Native Termux (F-Droid or Google Play build) — **not** proot-distro, not
  Andronix, not a Debian/Ubuntu container
- `python` ≥ 3.9 and `termux-services`
- Storage permission (`termux-setup-storage`)
- No root

```bash
pkg install python termux-services
```

After installing `termux-services` for the first time, **restart Termux** so the
`runsvdir` supervisor starts.

---

## Install

```bash
git clone <this repo>
cd FileManagerService
./scripts/install-termux.sh
```

The installer refuses to do anything surprising: it installs no packages,
requires no root, checks that `$PREFIX` really is native Termux, verifies the
Python version *and* the stdlib modules it needs, warns if port 8080 is taken,
creates the `Server/` tree only if it is missing (never touching existing
files), prompts for a username and password, and writes the service.

Options:

```bash
./scripts/install-termux.sh --port 8081 --root ~/storage/shared/Media
```

| Flag | Default |
| --- | --- |
| `--root PATH` | `$HOME/storage/shared/Server` |
| `--port PORT` | `8080` |
| `--host ADDR` | `0.0.0.0` (reachable on the LAN) |
| `--config PATH` | `~/.config/termuxfm/config.json` |
| `--force-setup` | re-prompt for credentials even if a config exists |

### Start it

```bash
sv up filemanager
sv status filemanager
```

Open `http://<your-phone-lan-ip>:8080` from your computer. The installer prints
the detected IP; `ifconfig wlan0` shows it too.

Port 8080 was chosen to avoid your existing services: `8022` SSH, `6800` aria2
RPC, `6880` AriaNg, `8096` Jellyfin, `9091` Transmission.

### Start it automatically after reboot

Add one line to `~/.termux/boot/start-server.sh`, alongside your existing
`sv up` lines:

```sh
sv up filemanager
```

For reference, the complete boot script then looks like:

```sh
#!/data/data/com.termux/files/usr/bin/bash

termux-wake-lock
sleep 10
source "$PREFIX/etc/profile.d/start-services.sh"

sv up sshd
sv up transmission
sv up aria2
sv up jellyfin
sv up ariang
sv up filemanager
```

Nothing else changes — `termux-wake-lock` and the existing delay already provide
everything this service needs.

---

## Managing the service

```bash
sv up filemanager          # start
sv down filemanager        # stop
sv restart filemanager     # restart
sv status filemanager      # status
tail -f $PREFIX/var/log/filemanager/current
```

The service runs **in the foreground** under `runit` (`exec`, no daemonising, no
`&`), which is what lets `sv` supervise it and restart it if it ever crashes.
`python3 -u` keeps output unbuffered so logs appear immediately.

---

## Configuration

Precedence: **CLI flag > environment variable > config file > default.**

```bash
filemanager serve --root ~/storage/shared/Server --host 0.0.0.0 --port 8080
```

| Flag | Env var | Default |
| --- | --- | --- |
| `--root` | `TERMUXFM_ROOT` | `~/storage/shared/Server` |
| `--host` | `TERMUXFM_HOST` | `0.0.0.0` |
| `--port` | `TERMUXFM_PORT` | `8080` |
| `--username` | `TERMUXFM_USERNAME` | `admin` |
| `--max-upload` | `TERMUXFM_MAX_UPLOAD` | `16G` (accepts `500M`, `2G`, …) |
| `--session-ttl` | `TERMUXFM_SESSION_TTL` | `604800` (7 days) |

Config file: `~/.config/termuxfm/config.json`, mode `0600`. It stores the
username, root, port, limits and the **PBKDF2 hash** of your password. The
server refuses to start if that file is group- or world-readable.

```bash
filemanager setup      # first-run: choose username + password
filemanager passwd     # change the password
filemanager check      # validate config, root and web assets
filemanager clean      # reclaim orphaned upload temp files
filemanager --version
```

**Credentials are never hard-coded and never passed as arguments.** There is
deliberately no `--password` flag, because `argv` is readable through `/proc`.
Interactive prompts use `getpass` (no echo, no shell history); for scripts:

```bash
printf '%s' 'your-password' | filemanager setup --password-stdin
```

---

## Organising a TV series for Jellyfin

The primary use case. Transmission drops everything into one folder:

```
downloads/torrents/Show.Name.S01.1080p/
    Show.Name.S01E01.1080p.mkv
    Show.Name.S01E02.1080p.mkv
```

In the browser:

1. Open `downloads/torrents/Show.Name.S01.1080p`
2. Click the first episode's row, **shift-click** the last to select the range
3. Click **Move** (the selection goes to the clipboard and survives navigation)
4. Navigate to `media/tv`
5. **New folder** → `Show Name`, open it, **New folder** → `Season 01`, open it
6. Click **Move here (N)**

Result — ready for a Jellyfin library scan:

```
media/tv/Show Name/Season 01/
    Show.Name.S01E01.1080p.mkv
    Show.Name.S01E02.1080p.mkv
```

Large moves and copies run as background jobs with a live progress bar and a
cancel button, so a multi-GB season never blocks the browser. Within the same
volume a move is an instant rename, no copying at all.

---

## Security

This is a LAN tool, and it is built to be safe on a LAN.

**Sandbox.** Every client path goes through one module (`termuxfm/safepath.py`);
nothing else in the codebase builds a path from client input. Containment is
checked **after** `os.path.realpath`, so a symlink pointing out of the root is
*rejected*, not followed. The prefix test uses `root + os.sep`, so a sibling
named `Server-evil` cannot masquerade as a child of `Server`. `..` is rejected
wherever it appears, absolute paths are rejected, and recursive walks (copy,
delete, zip, search, size) never follow symlinks — so a link cannot redirect a
recursive delete outside the tree.

**Authentication.** PBKDF2-HMAC-SHA256, 600,000 rounds, random 16-byte salt;
`hmac.compare_digest` for every comparison, including the username, so response
timing does not reveal which field was wrong. Sessions are 256-bit random
tokens held in memory (a restart logs everyone out; there is no session store on
disk to leak). Cookies are `HttpOnly; SameSite=Strict`. Failed logins are
throttled per client IP with exponential backoff.

**CSRF.** Every mutating request must echo the session's token in
`X-CSRF-Token`; combined with `SameSite=Strict`, a page on another origin cannot
drive the API even from your own browser. All `GET` endpoints are side-effect
free.

**Uploaded files are never executed and never trusted.** Downloads are always
`Content-Disposition: attachment`. Only a small allowlist of image/video/audio
MIME types is ever served inline, and `.html`, `.svg` and `.xml` are excluded
from preview entirely, so stored markup cannot run as script on this origin.
Every file response carries `X-Content-Type-Options: nosniff` and
`Content-Security-Policy: default-src 'none'; sandbox`. There is no shell
invocation anywhere in the codebase — no `os.system`, no `subprocess`, no
`shell=True` — so there is no command-injection surface. Environment variables
are never exposed, and the `Server` header does not leak the Python version.

**Resource limits.** Per-file upload cap (default 16 GB, configurable), 1 MB
JSON body cap, capped concurrent threads (a client cannot fork-bomb the phone),
bounded search (depth, deadline, result count), bounded job history, and capped
logs — see [Disk and memory footprint](#disk-and-memory-footprint).

**What it does *not* do:** no HTTPS (plain HTTP on the LAN), no per-transfer
rate limiting. **Do not port-forward this to the internet.** If you need remote
access, tunnel it over the SSH you already run on 8022:

```bash
ssh -p 8022 -L 8080:localhost:8080 u0_a383@<your-phone-lan-ip>
```

…then browse `http://localhost:8080` on your computer.

---

## Testing

The suite is stdlib `unittest` only, and runs on the phone or on a laptop. It
starts a real server on a loopback socket and talks HTTP to it, so routing,
status codes, cookies, CSRF, Range requests and chunked framing are all
exercised the way a browser exercises them.

```bash
cd FileManagerService
python3 -m unittest discover tests -v
```

207 tests covering:

| Area | Examples |
| --- | --- |
| **Path traversal** | `../`, `a/../../..`, encoded, absolute, NUL bytes, backslashes |
| **Root sandbox** | symlink→outside rejected, symlink→inside allowed, `/`-symlink rejected, `Server-evil` sibling-prefix rejected |
| **Authentication** | every endpoint 401s unauthenticated, session lifecycle and expiry, logout invalidation, CSRF missing/wrong → 403, per-IP login throttling, identical message for wrong-user vs wrong-password |
| **Upload** | streaming in bounded chunks (never one big read), `.part` cleanup after an aborted or truncated upload, size limit enforced from `Content-Length` *and* mid-stream, folder upload, traversal in `webkitRelativePath` rejected |
| **Download** | Range (prefix/suffix/mid-file), invalid range → 416, `HEAD`, symlink refused, attachment headers |
| **Large-file streaming** | a sparse **2 GiB+** file: exact `Content-Length`, and Range reads past the 2³¹ boundary (where a 32-bit offset bug would surface) |
| **Copy / move** | recursive, byte-accurate progress, conflict modes, move-into-own-descendant refused, **`EXDEV` cross-device fallback** for files and folders, unexpected errno *not* swallowed |
| **Delete** | recursive, confirmation required, root refused, a symlink is unlinked without touching its target |
| **Folders** | create, duplicate → 409, invalid names → 422, natural sort order |
| **Filename edge cases** | spaces, quotes, `&`, `#`, `%`, `+`, brackets, CJK, emoji, 200-char names, 255-**byte** cap, NFC normalisation, Android-reserved characters |
| **ZIP** | round-trip integrity, nested structure, chunked (no `Content-Length`), symlinks skipped |
| **HTTP** | 404/405/409/413/422, malformed JSON, static-asset allowlist, security headers, HTTP/1.1 keep-alive, 12 concurrent clients |
| **Disk footprint** | successful requests and Range floods write no log lines, errors always do, temp files hidden from listings and search, orphan sweep removes old temp files but never in-flight ones or real files, and never follows a symlink out of the tree |

Manual end-to-end checks worth doing after install:

```bash
# unauthenticated -> 401
curl -i http://<phone-lan-ip>:8080/api/list
# traversal -> 403
curl -i 'http://<phone-lan-ip>:8080/api/list?path=../../../../etc'
```

Then in the browser: log in, move a season into `media/tv/...`, upload a file
and a folder by drag-and-drop, ZIP a folder, scrub a video preview to the middle
(this proves Range works), check a folder's recursive size, delete with the
typed confirmation — and finally reboot the phone and confirm port 8080 comes
back with your other services.

---

## Disk and memory footprint

An always-on phone must not quietly fill its own storage, so both paths that
could grow without bound are capped.

**Logs: ~1 MB, hard ceiling.** Successful requests are **not** logged. That
matters more than it sounds: seeking around one video is thousands of HTTP Range
requests and the UI polls job progress while a copy runs, so per-request logging
turned 240 requests into 22 KB — megabytes over an evening, recording only that
nothing went wrong. Now the same 240 requests write **488 bytes**, which is the
startup banner plus one login line. What is still recorded is what you would
actually troubleshoot: startup, logins, uploads, every 4xx/5xx, and unhandled
errors.

On top of that, the installer writes `$PREFIX/var/log/filemanager/config`:

```
s262144
n3
```

which caps `svlogd` at 256 KB × 4 ≈ **1 MB total**, rotated. Without that file
svlogd would use its own default of 1 MB × 10 ≈ 10 MB. To keep more history,
raise `s`/`n` there and `sv restart filemanager`.

Need per-request logs to debug something? Add `--access-log` to the service
`run` script temporarily — the 1 MB cap still applies.

**Orphaned upload temp files: swept automatically.** An upload streams to a
`.termuxfm-part-*` file in the destination directory before an atomic rename.
If Android's low-memory killer stops the service mid-upload, that partial file —
potentially gigabytes — would otherwise sit there forever. So:

- temp files are **hidden from listings and search** (they are not content);
- on startup a background sweep reclaims any older than 6 hours, logging only
  when it actually frees something;
- files younger than that are never touched, so a concurrent upload is safe.

Run it by hand any time:

```bash
filemanager clean                      # anything older than 6 hours
filemanager clean --older-than 60      # more aggressive
```

**No other disk growth.** No database, no thumbnail cache, no session store, no
search index — directory listings come straight from `scandir`. The only state
outside your own files is the ~1 KB config file.

**Memory: ~28 MB RSS.** Measured *after* a 200 MB upload and a Range read 100 MB
into that file, because transfers stream in 512 KB chunks and never land in
memory. Bounded in-memory state: at most 64 sessions, 200 job records (reaped 10
minutes after finishing), 512 cached folder sizes, and 32 request threads.

---

## Uninstall

```bash
./scripts/uninstall-termux.sh            # keeps your config
./scripts/uninstall-termux.sh --purge    # also deletes the config
```

Your files under the server root are never touched. Remember to remove
`sv up filemanager` from `~/.termux/boot/start-server.sh`.

---

## Layout

```
FileManagerService/
├── termuxfm/
│   ├── safepath.py    the sandbox boundary (all path validation)
│   ├── auth.py        PBKDF2, sessions, CSRF, login throttling
│   ├── jobs.py        background jobs with progress and cancellation
│   ├── fsops.py       list/mkdir/rename/move/copy/delete/search/du
│   ├── transfer.py    streaming upload, Range download, preview policy
│   ├── ziputil.py     streaming ZIP over a non-seekable stream
│   ├── httpd.py       threaded HTTP/1.1 server, router, chunked responses
│   ├── api.py         JSON endpoints
│   ├── cli.py         serve / setup / passwd / check
│   └── config.py      flag > env > file > default
├── web/               index.html, app.js, style.css (no build, no CDN)
├── scripts/           install-termux.sh, uninstall-termux.sh
├── termux/service/    reference run scripts for termux-services
└── tests/             stdlib unittest suite
```

### HTTP API

All endpoints require the session cookie; mutating ones also require
`X-CSRF-Token`. Long operations return `202` with a job id to poll.

| Method | Endpoint | Notes |
| --- | --- | --- |
| `POST` | `/api/login`, `/api/logout` | |
| `GET` | `/api/me`, `/api/ping` | |
| `GET` | `/api/list`, `/api/stat` | `?path=&sort=&order=` |
| `POST` | `/api/mkdir`, `/api/rename` | |
| `POST` | `/api/copy`, `/api/move`, `/api/delete` | → `202` job |
| `POST` | `/api/search`, `/api/du` | → `202` job |
| `GET` | `/api/job?id=`, `POST /api/job/cancel` | |
| `PUT` | `/api/upload?dir=` | raw body; `X-Filename` / `X-Rel-Path` are base64url |
| `GET` | `/api/download`, `/api/preview` | Range supported |
| `GET` | `/api/zip?path=` | chunked stream |

---

## Troubleshooting

**`sv status filemanager` says `down`** — read the log:
`tail -50 $PREFIX/var/log/filemanager/current`. Then run
`filemanager check` to validate the configuration.

**`sv: fatal: unable to change to service directory`** — `runsvdir` is not
running. Restart Termux, or `source "$PREFIX/etc/profile.d/start-services.sh"`.

**"No password configured"** — run `filemanager setup`.

**"Root directory does not exist"** — run `termux-setup-storage` and grant the
permission, or pass a different `--root`.

**Cannot reach it from your computer** — confirm both devices are on the same
Wi-Fi, that the host is `0.0.0.0` (not `127.0.0.1`), and that the phone's IP has
not changed (`ifconfig wlan0`). A static DHCP lease on your router avoids that.

**Stops working after the screen sleeps** — `termux-wake-lock` must be active
(your boot script already does this), and Termux must be exempt from battery
optimisation in Android settings.

**"This filesystem rejected the name"** — Android's shared storage forbids
`" * : < > ? \ |` and trailing dots or spaces in file names.

**Uploads fail at a certain size** — raise the cap:
`filemanager serve --max-upload 32G` (or edit the service `run` script).

**Storage disappeared after a failed upload** — Android may have killed the
service mid-transfer. Reclaim the partial file with `filemanager clean` (a
restart does this automatically for anything older than 6 hours).

---

## License

MIT — see [LICENSE](LICENSE).
