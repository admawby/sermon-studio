# Sermon Studio

A self-hosted web app that turns a full Sunday service recording into a sermon podcast episode:

1. **Pick** a recording from the NAS or the server (or upload one from your computer).
2. **AI finds the sermon.** Whisper transcribes the service locally and a local LLM picks where the sermon starts and ends. It also suggests a title and the main scripture passage.
3. **Review** the cut on a waveform with a clickable, searchable transcript. Click any line to hear it and adjust the start or end with one click.
4. **Fill in the details:** title, speaker, scripture, date, cover image and **series**. The series list is pulled from WordPress and defaults to the series of the most recently edited sermon.
5. **Encode** a small mono MP3 (volume-levelled, ID3-tagged, cover art embedded).
6. **Upload** to the church WordPress site as a draft or published post.

Everything except the WordPress upload runs on your own hardware. No audio leaves the house until you press upload.

Built for a church website hosted on The Church Co's WordPress platform. The upload step is written for their podcast form, and everything else is generic.

---

## How it works

```
 browser ──► sermon-studio (FastAPI, :8790) ──► ffmpeg        (convert, waveform, encode)
                    │                      ──► faster-whisper (speech-to-text, CPU or CUDA)
                    │                      ──► ollama         (local LLM: sermon boundaries, title)
                    └──────────────────────────► WordPress wp-admin (login → upload media → fill in the podcast form)
```

| Stage | What happens | Output (in `data/work/<job-id>/`) |
|---|---|---|
| Prepare | ffmpeg makes a 16 kHz mono WAV for Whisper, a 48 kbps preview MP3 for the browser player and a waveform PNG | `speech.wav`, `preview.mp3`, `waveform.png` |
| Transcribe | faster-whisper with VAD; `small.en` on CPU, `large-v3-turbo` on GPU | `transcript.json` |
| Detect | **AI:** a coarse pass over the whole service (30-second lines), then a full-resolution pass around each edge to pin the exact start/end. **Fallback:** the longest stretch of steady speech (speech runs about 140 words per minute, sung lyrics much less) | `proposal.json` |
| Suggest | Title and main scripture from the sermon text (LLM, plus a regex scripture finder) | stored in `state.json` |
| Encode | Cut, 70 Hz high-pass, loudness-normalised to −16 LUFS (the podcast standard), fades, mono MP3 at the chosen bitrate, ID3 tags and cover | `data/output/<FILE_PREFIX>YYYYMMDD.mp3` |
| Upload | Logs in to wp-admin, uploads the MP3 and cover to the media library and submits the "Add New Podcast" form the same way a person would | link shown in the UI |

Transcripts are cached per recording, so reopening a job is instant and "re-run detection" doesn't re-transcribe.

### Accuracy so far

On a real 64-minute service with a hand-edited sermon to compare against (found inside the raw recording by audio cross-correlation), the AI cut started 14 s after the hand cut and ended 2 s after it. The suggested title and scripture matched what was actually published. The heuristic fallback (no LLM) found the end within 20 s but started a few minutes early, because it included the scripture reading. The review screen exists so a person always checks the edges before anything is published.

---

## Requirements

- Docker with Compose v2
- About 10 GB of disk for images and models (the Whisper model and `qwen2.5:7b-instruct` download on first use)
- **CPU-only works**, it's just slower. On an i5-10500, a 65-minute service takes roughly 10–15 minutes to transcribe, and the LLM detection takes a few minutes more.
- **NVIDIA GPU (recommended):** takes a whole service from about 20 minutes down to about 1–2 minutes. Needs the NVIDIA driver and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html). About 8 GB of VRAM fits both Whisper `large-v3-turbo` and a 7B LLM.

## Install

```bash
git clone https://github.com/<your-user>/sermon-studio.git ~/Docker/sermon-studio
cd ~/Docker/sermon-studio
cp .env.example .env        # then edit it (see below)
mkdir -p data ollama
docker compose up -d --build
```

Open **http://<server>:8790**.

### Paths

`docker-compose.yml` mounts:

| Host | Container | Shown in the browser as |
|---|---|---|
| `NAS_PATH` (e.g. a NAS share, read-only) | `/media/nas` | `NAS` |
| `LOCAL_PATH` (read-only) | `/media/local` | `Local` |
| `./data/uploads` | `/data/uploads` | `Uploads` (files uploaded from your computer) |

Set `NAS_PATH` and `LOCAL_PATH` in `.env` (Compose reads them from there), and keep `BROWSE_ROOTS` in step with the container paths. The file browser can't leave these folders. The container runs as UID 1000, so the mounted folders must be readable by that user.

### Turning on the GPU (once the card is installed)

```bash
# install the NVIDIA driver + nvidia-container-toolkit, then:
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

The header of the page shows which model and device are in use (for example `AI: large-v3-turbo on CUDA`). No rebuild is needed because the CUDA libraries are already in the image.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `WP_URL` | `https://www.example-church.org` | WordPress site |
| `WP_USER` / `WP_PASSWORD` | — | A WordPress login that can create podcasts and upload media (no 2FA). Set them with the snippet below so the password stays out of your shell history. |
| `WP_POST_TYPE` | `podcasts` | Post type for sermons |
| `WP_ALWAYS_CATEGORY` | `All Sermons` | Category ticked on every sermon in addition to its series |
| `WP_DEFAULT_STATUS` | `draft` | Reserved for the default upload status (the UI has both buttons) |
| `WHISPER_MODEL` | `auto` | `auto` = `large-v3-turbo` on GPU, `small.en` on CPU. Any faster-whisper model name works. |
| `LLM_MODEL` | `qwen2.5:7b-instruct` | Ollama model for detection and titles. Leave it blank to use the speech-rate heuristic only. |
| `MP3_BITRATE` | `64k` | Default bitrate (can be changed per sermon in the UI) |
| `FILE_PREFIX` | `Sermon_` | Output name: `<prefix>YYYYMMDD.mp3` |
| `NAS_PATH` / `LOCAL_PATH` | `/mnt/nas`, `/srv/recordings` | Host folders mounted read-only for the file browser |
| `BROWSE_ROOTS` | `NAS=/media/nas,Local=/media/local,Uploads=/data/uploads` | Named folders the file browser can open |
| `START_PATH` | `NAS` | Folder the browser opens at |

To set the login without the password landing in your shell history, save this as `set-wp-login.sh` and run `bash set-wp-login.sh`. Run it as a file rather than pasting it, because a pasted `read` takes the next pasted line as its answer:

```bash
#!/bin/bash
cd ~/Docker/sermon-studio || exit 1
read -p "WordPress username: " WPU
read -s -p "WordPress password: " WPP; echo
WPU="$WPU" WPP="$WPP" python3 - <<'EOF'
import os, re, sys
user, pw = os.environ["WPU"], os.environ["WPP"]
if "'" in pw:
    sys.exit("Passwords containing ' can't be stored in .env; please pick another one.")
s = open(".env").read()
s = re.sub(r"^WP_USER=.*$", lambda m: f"WP_USER={user}", s, flags=re.M)
s = re.sub(r"^WP_PASSWORD=.*$", lambda m: f"WP_PASSWORD='{pw}'", s, flags=re.M)
open(".env", "w").write(s)
EOF
chmod 600 .env
docker compose up -d --force-recreate sermon-studio
```

`.env` holds your WordPress password. It's in `.gitignore`, so never commit it.

### About the WordPress login

The Church Co blocks the WordPress REST API for everyone, logged in or not: every route answers `{"message":"God Bless"}`. So Sermon Studio does what you would do by hand. It logs in at `wp-login.php` and fills in **Podcasts → Add New** (`post-new.php?post_type=podcasts`):

| Form field | Filled with |
|---|---|
| Title | sermon title |
| Content and Excerpt | scripture reference (e.g. `Romans 8:28-39`), as on existing sermons |
| Audio (ACF file field `podcast_audio`) | the MP3, uploaded to the media library |
| Featured image | the cover image |
| Feed Image (ACF `podcast_feed_image`) | copied from the most recent sermon (the same podcast artwork every week) |
| People | the speaker; the UI suggests names from the People list |
| Categories | **All Sermons** plus the chosen series |
| Publish date | the service date, 10:00 |

ACF fields are looked up by name, so it keeps working if the field keys change. If the login fails with "no session cookie", the account probably has 2FA or the login page has a captcha.

**Default series and speaker:** WordPress doesn't record when a category was edited, so the app opens the most recently modified sermon post and uses its series (any category other than "All Sermons") and its speaker.

`GET /api/wp/discover` dumps the podcast form's fields, ACF fields and categories, which helps if The Church Co changes the form.

## Using it

1. **Pick the recording.** The browser opens at `START_PATH` with the newest folders first. Click a `.wav`, or use **Upload from this computer**.
2. **Wait for the analysis.** A progress bar shows each stage. Jobs run one at a time, and you can close the tab and come back (open a job again from **Recent recordings**).
3. **Check the cut.**
   - The shaded region on the waveform is the proposed sermon. Click the waveform to jump there.
   - **▶ Play from start** and **▶ Last 20 s** let you hear both edges quickly.
   - Hover a transcript line and press **start here** or **end here**, or type a time (`H:MM:SS`), or play to a point and press **Set to playhead**.
   - Use the search box to find phrases like "let's pray" or "open your Bibles".
4. **Fill in the details.** The title and scripture are pre-filled from the AI suggestions. The speaker is remembered from last time. The date comes from the recording's file name. Choose the series and add a cover image, either uploaded from your computer or picked from the NAS.
5. **Make MP3**, then listen to the result in the player or download it.
6. **Upload as draft** (recommended) and check it in WP Admin, or **Upload & publish**.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | models, device, roots, saved settings |
| GET | `/api/browse?path=NAS/Audio&kind=audio\|image` | list a folder |
| POST | `/api/upload-source` | multipart `file` → `{virtual}` |
| POST | `/api/jobs` `{path}` | start (or reopen) analysis of a recording |
| GET | `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/transcript` | job state |
| GET | `/api/jobs/{id}/preview.mp3 \| waveform.png \| cover.jpg \| output.mp3` | files |
| POST | `/api/jobs/{id}/redetect` | re-run detection on the cached transcript |
| POST | `/api/jobs/{id}/cover` (multipart) or `/cover-path` `{path}` | set the cover image |
| POST | `/api/jobs/{id}/encode` `{start,end,title,speaker,series,series_name,scripture,date,bitrate,normalize}` | make the MP3 |
| POST | `/api/jobs/{id}/publish` `{status: draft\|publish}` | upload to WordPress |
| GET | `/api/wp/meta`, `/api/wp/discover` | series list and default; field discovery |

## Project layout

```
app/
  main.py         FastAPI routes, safe path resolution, encode/publish threads
  jobs.py         job queue + per-recording state in data/work/<id>/
  audio.py        ffmpeg helpers: prepare, encode, ID3 tags
  transcribe.py   faster-whisper (auto CPU/CUDA)
  detect.py       sermon boundary detection (LLM + heuristic), title/scripture suggestion
  wordpress.py    wp-admin client: series/speaker sync, media upload, podcast form
  static/         single-page UI (no build step, no external JS)
docker-compose.yml       app + ollama
docker-compose.gpu.yml   GPU override
```

## Known limitations / TODO

- No authentication on the web UI. It's meant for the LAN or Tailscale only. Don't expose port 8790 to the internet.
- One analysis runs at a time. Encoding and uploading run alongside it.
- A speaker name that isn't already in People is created as a new person when the post is saved, so watch for typos.
