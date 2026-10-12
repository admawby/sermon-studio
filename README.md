# Sermon Studio

A self-hosted web app that turns a full Sunday service recording into a sermon podcast episode. It works as a four-step wizard:

1. **Upload:** drop in the service recording and the sermon slide (cover image). You can also pick files already on the NAS.
2. **Analyze:** the app suggests a NAS folder to archive the recording in (following your existing folder layout). Meanwhile, Whisper transcribes the service locally and a local LLM finds where the sermon starts and ends, then writes a title, the scripture reference and a 2–3 sentence description.
3. **Review:** check the cut on a waveform with a clickable, searchable transcript. Then edit the title, speaker, date, scripture, description and **series**. Series and speaker come from WordPress and default to those of the most recently edited sermon.
4. **Publish:** the app encodes a small mono MP3 (volume-levelled, ID3-tagged, cover art embedded) and saves it next to the recording on the NAS. It shows a preview of the post, then uploads it to WordPress as a draft or publishes it.

It can also run **hands-off**: it watches the NAS archive for new service recordings, analyzes them on its own and sends the result to you on **Discord** with clips of where the sermon starts and ends. You reply with the week's slide and press **Publish** (see [Automation](#automation-new-recordings--discord)).

Everything except the WordPress upload and Discord runs on your own hardware. No audio leaves the house until you press upload.

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
| Upload | The browser uploads both files into `data/uploads/`, keeping the recording's original timestamp so re-uploading the same file reuses its analysis | `data/uploads/` |
| Archive | Suggests `<ARCHIVE_ROOT>/YYYY/MM - Month/YYYY_MM_DD`, copying the naming of existing folders and reusing a folder that already exists for that date. On **Save here**, it copies the recording there, and the MP3 later. It never overwrites a file it didn't write itself. | NAS |
| Prepare | ffmpeg makes a 16 kHz mono WAV for Whisper, a 48 kbps preview MP3 for the browser player and a waveform PNG | `speech.wav`, `preview.mp3`, `waveform.png` |
| Transcribe | faster-whisper with VAD; `small.en` on CPU, `large-v3-turbo` on GPU | `transcript.json` |
| Detect | **AI:** a coarse pass over the whole service (30-second lines), then a full-resolution pass around each edge to pin the exact start/end. **Fallback:** the longest stretch of steady speech (speech runs about 140 words per minute, sung lyrics much less) | `proposal.json` |
| Suggest | Title, main scripture and a 2–3 sentence description from the sermon text (LLM, plus a regex scripture finder). "✨ Write it again" in Review re-runs it for an adjusted cut. | stored in `state.json` |
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
| `NAS_PATH` (e.g. a NAS share, **writable** so recordings and MP3s can be archived) | `/media/nas` | `NAS` |
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
| `START_PATH` | `NAS` | Folder the NAS picker opens at |
| `ARCHIVE_ROOT` | `NAS` | Archive folder for recordings and MP3s; the app suggests `YYYY/MM - Month/YYYY_MM_DD` under it |

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
| Content | the 2–3 sentence description |
| Excerpt | scripture reference (e.g. `Romans 8:28-39`) |
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

The step bar at the top shows where you are. Click any finished step to go back to it.

1. **Upload.** Drop the WAV into the left box and the sermon slide into the right box, or tap a box to choose a file. Each box shows upload progress and turns green when the file is done. **Next: Analyze** starts the work. If you leave out the image, the app asks before continuing, and you can add one in Review.
2. **Analyze.** Check the suggested NAS folder. It's marked *existing folder* or *new folder*. Press **Save here**, or **Choose a different folder…** first. The checklist shows each stage: convert, transcribe (with %), find the sermon, write the title and description. On a CPU this takes about 20–25 minutes for a 65-minute service, and with a GPU a couple of minutes. You can close the tab and come back later via **Continue a sermon you already started** in step 1.
3. **Review.**
   - The shaded part of the waveform is the sermon. Click the waveform to listen anywhere. **▶ Listen** and **▶ Last 20 s** play the edges.
   - Hover a transcript line and press **start here** or **end here**, type a time (`H:MM:SS`), or play to a point and press **Set to playhead**. Search finds phrases like "let's pray".
   - Edit the title, speaker, date, series, scripture (excerpt) and description (content). **✨ Write it again** asks the AI for a new title, scripture and description based on your adjusted cut.
   - **MP3 settings** sets the bitrate (64 kbps mono by default) and loudness levelling.
4. **Publish.** The app makes the MP3 and copies it into the NAS folder. It then shows the post as it will appear, with a player for the finished MP3. Choose **Upload as draft** to check the post in WP Admin first (recommended), or **Publish now**. When it's done you get links to the post, and **Start the next sermon** starts over.

## Automation: new recordings → Discord

Monitoring can be switched on and off in the web app's **⚙ Settings** tab; that choice is remembered and overrides `WATCH_ENABLED` (which only sets the default). When you switch it back on, the app asks whether to also analyze recordings that arrived while it was off, or to skip them. The tab also shows when the NAS was last checked, has a **Check now** button, and shows whether Discord, WordPress and the AI are connected.

While monitoring is on, the app scans `ARCHIVE_ROOT` every `WATCH_INTERVAL` seconds for new `.wav` files.

- On the first scan it records everything already there and processes none of it, so only recordings that appear afterwards are picked up.
- A file counts as new once its size has stopped changing between two scans and it's at least 2 minutes old, so a recording that's still copying isn't analyzed half-finished.
- Files the app copies into the archive itself (from **Save here**) are ignored.
- The scan state is kept in `data/watch.json`. Delete that file to take a fresh baseline.

For each new recording, the Discord bot:

1. **Tells you it's analyzing** the recording, with the folder name and a time estimate.
2. **Sends the proposed sermon** once the analysis is done. The message shows the title, series, speaker, date, scripture, description and start → end times, with two 20-second MP3 clips of how the sermon starts and ends. It has three buttons: **✅ Publish**, **📝 Save as draft** and **✏️ Open in Sermon Studio**, which opens the Review step if you want to change anything.
3. **Asks for this week's sermon slide.** Reply to the message with the image, or just post it in the channel. It becomes the post's cover, and the Publish and draft buttons unlock once it has arrived.

Pressing **Publish** or **Save as draft** makes the MP3 (with the proposed cut and details, or your edits if you changed them in the web app), saves it next to the recording on the NAS and posts it. The bot replies with the link. Only the Discord user in `DISCORD_USER_ID` can press the buttons or send slides.

The series and speaker default to those of the last sermon post, the same as in the web app.

### Creating the Discord bot

1. **Create a server** (optional but tidy). In Discord, press **+** in the server list → **Create My Own** → **For me and my friends**, and name it, e.g. "Home Lab". Add a text channel such as `#sermon-studio`.
2. **Turn on Developer Mode** so you can copy IDs: **User Settings → Advanced → Developer Mode**.
3. **Create the application:** go to <https://discord.com/developers/applications> → **New Application**, and name it "Sermon Studio".
4. **Set up the bot:** open the **Bot** tab.
   - Press **Reset Token**, then copy the token. That's `DISCORD_TOKEN`; treat it like a password.
   - Turn **on** *Message Content Intent*, which the bot needs to see the slide image you post.
   - Turn **off** *Public Bot*, so only you can add it to servers.
5. **Invite it to your server:** open **OAuth2 → URL Generator**. Tick the scope `bot`, then the bot permissions *View Channels*, *Send Messages*, *Attach Files*, *Embed Links* and *Read Message History*. Open the generated URL and pick your server.
6. **Copy the IDs:**
   - Right-click your own name → **Copy User ID**. That's `DISCORD_USER_ID`.
   - Right-click the channel → **Copy Channel ID**. That's `DISCORD_CHANNEL_ID`. Leave it blank to get DMs instead.
7. **Fill in `.env`:** set `DISCORD_TOKEN`, `DISCORD_USER_ID`, `DISCORD_CHANNEL_ID`, `PUBLIC_URL` (e.g. `http://192.168.1.10:8790`) and `WATCH_ENABLED=true`. Then run `docker compose up -d`.

The log (`docker logs sermon-studio`) shows `discord: logged in as Sermon Studio#1234` when the bot is connected.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | models, device, roots, saved settings |
| GET | `/api/browse?path=NAS/Audio&kind=audio\|image` | list a folder |
| POST | `/api/upload/audio` | multipart `file` (+ `last_modified` ms) → `{virtual}` |
| POST | `/api/upload/image` | multipart `file` → `{virtual}` |
| POST | `/api/jobs` `{path, cover}` | start (or reopen) analysis of a recording, with an optional cover image path |
| GET / POST | `/api/jobs/{id}/archive` (`{folder}`) | suggested NAS folder / save the recording (and MP3) there |
| POST | `/api/jobs/{id}/describe` `{start,end}` | re-write the title, scripture and description for a cut |
| GET | `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/transcript` | job state |
| GET | `/api/jobs/{id}/preview.mp3 \| waveform.png \| cover.jpg \| output.mp3` | files |
| POST | `/api/jobs/{id}/redetect` | re-run detection on the cached transcript |
| POST | `/api/jobs/{id}/cover` (multipart) or `/cover-path` `{path}` | set the cover image |
| POST | `/api/jobs/{id}/encode` `{start,end,title,speaker,series,series_name,scripture,description,date,bitrate,normalize}` | make the MP3 |
| POST | `/api/jobs/{id}/publish` `{status: draft\|publish}` | upload to WordPress |
| GET | `/api/wp/meta`, `/api/wp/discover` | series list and default; field discovery |
| GET | `/api/settings` | monitoring status + Discord/WordPress/AI connection status |
| POST | `/api/settings/watch` `{enabled, catch_up}` · `/api/settings/watch/scan` | turn monitoring on/off · check the NAS now |

## Project layout

```
app/
  main.py         FastAPI routes, safe path resolution, upload/archive/encode/publish
  archive.py      NAS folder suggestion + no-clobber copying
  jobs.py         job queue + per-recording state in data/work/<id>/
  audio.py        ffmpeg helpers: prepare, encode, ID3 tags
  transcribe.py   faster-whisper (auto CPU/CUDA)
  detect.py       sermon boundary detection (LLM + heuristic), title/scripture suggestion
  wordpress.py    wp-admin client: series/speaker sync, media upload, podcast form
  watcher.py      polls the archive for new recordings
  settings.py     settings changed from the UI (data/settings.json)
  discord_bot.py  Discord notifications, approval buttons, slide upload
  static/         four-step wizard UI (no build step, no external JS)
docker-compose.yml       app + ollama
docker-compose.gpu.yml   GPU override
```

## Known limitations / TODO

- No authentication on the web UI. It's meant for the LAN or Tailscale only. Don't expose port 8790 to the internet.
- One analysis runs at a time. Encoding and uploading run alongside it.
- A speaker name that isn't already in People is created as a new person when the post is saved, so watch for typos.
