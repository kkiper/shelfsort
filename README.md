# shelfsort

Organizes an ebook collection (books, magazines, plans and articles) on a homelab.
It scans your source folders, identifies each file (filename, embedded metadata, ISBN,
OCR of the cover, optional local AI), groups duplicates and variants, and builds a plan
of renames and moves into `Books`, `Magazines` and `Plans & Articles` folders. It writes
metadata into the files and can add an OPF sidecar. Nothing changes under your sources
until you press Apply. It never deletes: removed files go to `_quarantine`.

## Security

There is **no login**. Anyone who can reach the port can scan, apply and empty the
quarantine. Use it only on a trusted LAN and never expose it to the internet (no port
forward, no public reverse proxy).
POSTs whose `Origin` (or `Referer`) names another host:port are refused (403), so a
web page on another site cannot drive it through your browser. A reverse proxy must
pass the original `Host` header.

## Install on Unraid

1. Build the image somewhere with Docker and push it, or build it on the Unraid box:

   ```
   docker build -t ghcr.io/YOUR_GITHUB_USER/shelfsort:latest .
   docker push ghcr.io/YOUR_GITHUB_USER/shelfsort:latest     # skip if building on Unraid
   ```
2. Copy `unraid/shelfsort.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the
   Unraid flash drive. In Docker > Add Container, pick the `shelfsort` template.
3. Replace `YOUR_GITHUB_USER` in Repository (or use your local image name), and set the
   paths: `/config` to `/mnt/user/appdata/shelfsort`, `/sources/remote` to your NAS mount,
   `/sources/local` to BookOrbit's library share. The container runs as `99:100`
   (`nobody:users`); make sure that user can write to those folders.
4. Apply and open `http://<unraid-ip>:8585/`.

Any other folder you mount under `/sources/<name>` is picked up as an extra source.

### Mounting the remote NAS

Install the Unassigned Devices plugin, then Add Remote SMB/NFS Share in its tab and
mount it. It appears under `/mnt/remotes/<share>`; use that as the `/sources/remote` path.
The share must be mounted read-write, and the dashboard shows each source's mounted and
writable status.

## First run checklist

1. **Settings:** fill in the magazine title list and the subject map; check the sources.
2. **Scan** (a dry run: nothing is modified). Watch the dashboard counts.
3. **Review:** work through the tabs (variants, near-copies, possible matches,
   unidentified, uncertain magazines, uncertain subject/kind).
4. **Plan, then Apply ONE small subject batch** only.
5. **Check it in BookOrbit:** import or open those processed files.
6. **Spec section 11.1 sidecar check:** confirm whether BookOrbit reads the embedded
   metadata and/or the `metadata.opf` sidecar. If embedded metadata is enough, turn off
   the sidecar toggle ("OPF sidecar for all files") in Settings.
7. Continue with the remaining batches.

## Undo and quarantine

* Every operation is journaled before it runs. If the container dies mid-batch, the next
  start recovers.
* **Undo** (History) reverses a batch. Original files that had metadata written in place
  are first copied to `<source>/_quarantine/_originals/` and restored from there.
* Duplicates you remove are moved to `<source>/_quarantine/<relative path>`, not deleted.
  Files set aside by an undo go to `_quarantine/_undone`.
* **Empty quarantine** (History) **permanently deletes** the quarantine contents and makes
  those batches impossible to undo. It asks for confirmation. Do it only when you are
  happy with the result.

## Optional: Ollama

An AI resolver for hard-to-identify covers is off by default; no cloud services are used.
Run an Ollama container yourself (a commented-out example is in `docker-compose.yml`),
then in Settings enable Ollama, enter its URL (for example `http://<unraid-ip>:11434`)
and a vision-capable model (default `llava`).

## First build

Run these where Docker exists (Docker was not available when this was written, so none of
this has been run yet):

```
mkdir -p dev/sources/remote dev/sources/local dev/config
sudo chown -R 99:100 dev      # the container runs as 99:100 and writes to these mounts
docker compose build
docker compose run --rm app pytest -m tools -v    # ebook-meta, tesseract, real OCR
docker compose up -d
curl -s localhost:8585/ | grep -q shelfsort && echo OK
```

On Unraid the appdata and share permissions for nobody:users are normally already right,
so the chown is only needed for the local dev folders. To pin Calibre, build with
`--build-arg CALIBRE_VERSION=x.y.z` (default: latest).

Then run a scan from the UI against `dev/sources` and confirm the dashboard counts.

## Tests

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\...
python -m pytest -m "not tools"                  # runs anywhere
docker compose run --rm app pytest -m tools      # needs Calibre/Tesseract (in the image)
```

## Configuration

Environment variables: `PORT` (default 8585), `SHELFSORT_DB` (default
`/config/shelfsort.db`), `SHELFSORT_SOURCES_ROOT` (default `/sources`).
