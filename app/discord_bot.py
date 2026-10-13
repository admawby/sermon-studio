"""Discord bot: reports new recordings, sends the proposed sermon for approval, collects the slide.

Runs its own asyncio loop in a background thread. Other threads call the notify_* functions, which
hand the work to that loop. Only DISCORD_USER_ID may press the buttons or send slides.

Actions are injected by main.py (to avoid an import cycle):
  latest_job()                -> job dict the next slide image belongs to (or None)
  job_for_message(message_id) -> job dict whose Discord message this is (or None)
  set_cover(job, path)        -> save an image file as the job's cover
  approve(job_id, status)     -> make the MP3 + post it ("publish" | "draft"); returns (message, ok)
  job(job_id)                 -> job dict
  update(job, **fields)       -> persist fields on the job
"""
import asyncio
import logging
import tempfile
import threading
from pathlib import Path

from . import config

log = logging.getLogger("sermon.discord")
_client = None
_loop = None
_ready = threading.Event()
_thread = None
error = None  # why the bot couldn't connect, for the Settings page
actions = {}


def enabled():
    return bool(config.DISCORD_TOKEN and config.DISCORD_USER_ID)


def start(acts=None):
    global _thread, error
    if acts:
        actions.update(acts)
    if not enabled():
        return
    error = None
    _thread = threading.Thread(target=_run, daemon=True, name="discord")
    _thread.start()


def _run():
    global error
    try:
        asyncio.run(_main())
    except Exception as e:
        import discord
        if isinstance(e, discord.LoginFailure):
            error = "Discord rejected the bot token. Copy a fresh one from the Bot tab (Reset Token)."
        elif isinstance(e, discord.PrivilegedIntentsRequired):
            error = "Turn on “Message Content Intent” in the Developer Portal (Bot tab), then save again."
        else:
            error = f"{type(e).__name__}: {e}"
        log.warning("discord: %s", error)
    finally:
        _ready.clear()


def restart():
    """Reconnect with the current settings (after the token changes in the UI)."""
    global _client
    if _client and _loop and not _client.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_client.close(), _loop).result(timeout=15)
        except Exception:
            pass
    if _thread:
        _thread.join(timeout=15)
    _ready.clear()
    _client = None
    start()


def send_test():
    """Post a hello message. Returns (ok, message)."""
    if not enabled():
        return False, "Fill in the bot token and your user ID first."
    if not _ready.wait(timeout=25):
        return False, error or "The bot hasn't connected yet. Check the token."

    async def go():
        ch = await _channel()
        await ch.send("👋 **Sermon Studio is connected.** I'll post here when a new recording has been analyzed.")
        return getattr(ch, "name", None) or "your DMs"
    try:
        where = asyncio.run_coroutine_threadsafe(go(), _loop).result(timeout=30)
        return True, f"Sent a test message to #{where}." if where != "your DMs" else "Sent you a test DM."
    except Exception as e:
        return False, f"Connected, but couldn't post: {e}. Is the bot in that server, with permission to post in the channel?"


def connected():
    return _ready.is_set()


def name():
    return str(_client.user) if _client and _client.user else None


def _call(coro):
    """Run a coroutine on the bot's loop from any other thread and wait for it."""
    if not _ready.wait(timeout=60):
        log.warning("discord: bot not connected; dropping message")
        return None
    try:
        return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=300)
    except Exception:
        log.exception("discord: send failed")
        return None


async def _channel():
    if config.DISCORD_CHANNEL_ID:
        return _client.get_channel(config.DISCORD_CHANNEL_ID) or await _client.fetch_channel(config.DISCORD_CHANNEL_ID)
    user = await _client.fetch_user(config.DISCORD_USER_ID)
    return user.dm_channel or await user.create_dm()


def _studio_link(job):
    return f"{config.PUBLIC_URL}/#{job['id']}/3" if config.PUBLIC_URL else None


def _button_ok():
    """Discord only accepts link buttons with a full address (a dot in the host name, e.g.
    server.tailnet.ts.net or an IP), not a bare name like http://server:8790."""
    from urllib.parse import urlsplit
    host = urlsplit(config.PUBLIC_URL or "").hostname or ""
    return "." in host


def _view(job, enabled_buttons, link=True):
    import discord
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Publish", emoji="✅", style=discord.ButtonStyle.success,
                                 custom_id=f"ss:publish:{job['id']}", disabled=not enabled_buttons))
    v.add_item(discord.ui.Button(label="Save as draft", emoji="📝", style=discord.ButtonStyle.secondary,
                                 custom_id=f"ss:draft:{job['id']}", disabled=not enabled_buttons))
    if link and _studio_link(job) and _button_ok():
        v.add_item(discord.ui.Button(label="Open in Sermon Studio", emoji="✏️", url=_studio_link(job)))
    return v


def _fmt(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


# --- notifications (called from worker threads) ----------------------------
def notify_analyzing(job, folder, minutes):
    async def go():
        ch = await _channel()
        await ch.send(f"🎙️ **New service recording found**\n`{folder}/{job['name']}`\n"
                      f"Analyzing it now. I'll message you when the sermon is ready (about {minutes} min).")
    _call(go())


def notify_error(job):
    async def go():
        ch = await _channel()
        await ch.send(f"⚠️ Analyzing `{job['name']}` failed: {job.get('message', 'unknown error')}")
    _call(go())


def notify_ready(job, cut, clips):
    """cut: the proposed cut/details; clips: (start_clip_path, end_clip_path)."""
    async def go():
        import discord
        ch = await _channel()
        embed = discord.Embed(title=cut["title"] or "(no title)", description=cut.get("description") or "",
                              color=0x3B6EA8)
        embed.add_field(name="Series", value=cut.get("series_name") or "—")
        embed.add_field(name="Speaker", value=cut.get("speaker") or "—")
        embed.add_field(name="Date", value=cut["date"])
        embed.add_field(name="Scripture", value=cut.get("scripture") or "—")
        embed.add_field(name="Sermon", value=f"{_fmt(cut['start'])} → {_fmt(cut['end'])} "
                                             f"({_fmt(cut['end'] - cut['start'])})")
        files = [discord.File(clips[0], filename="sermon-start.mp3"), discord.File(clips[1], filename="sermon-end.mp3")]
        has_cover = bool(job.get("cover"))
        if _studio_link(job) and not _button_ok():
            embed.add_field(name="Open in Sermon Studio", value=_studio_link(job), inline=False)
        text = ("✅ **The sermon is ready.** Listen to how it starts and ends (attached).\n"
                + ("Slide is attached already. Press **Publish** when it looks right."
                   if has_cover else
                   "📎 **Reply with this week's sermon slide** (an image) and I'll attach it to the post. "
                   "Publish unlocks once it's here."))
        try:
            msg = await ch.send(text, embed=embed, files=files, view=_view(job, has_cover))
        except discord.HTTPException as e:
            if "url" not in str(e).lower():
                raise
            # Discord only accepts link buttons with a full address (e.g. a Tailscale *.ts.net name);
            # send without that button and put the link in the text instead
            log.warning("discord: link button rejected (%s); sending without it", _studio_link(job))
            files = [discord.File(clips[0], filename="sermon-start.mp3"), discord.File(clips[1], filename="sermon-end.mp3")]
            msg = await ch.send(text + f"\n✏️ Open in Sermon Studio: {_studio_link(job)}", embed=embed, files=files,
                                view=_view(job, has_cover, link=False))
        actions["update"](job, discord={"channel": ch.id, "message": msg.id})
    _call(go())


# --- bot ----------------------------------------------------------------------
async def _main():
    global _client, _loop
    import discord
    intents = discord.Intents.default()
    intents.message_content = True  # needed to see image attachments in a server channel
    _client = discord.Client(intents=intents)
    _loop = asyncio.get_running_loop()

    @_client.event
    async def on_ready():
        global error
        log.info("discord: logged in as %s", _client.user)
        error = None
        _ready.set()

    @_client.event
    async def on_message(msg):
        if msg.author.bot or msg.author.id != config.DISCORD_USER_ID:
            return
        in_dm = isinstance(msg.channel, discord.DMChannel)
        if not (in_dm or msg.channel.id == config.DISCORD_CHANNEL_ID):
            return
        images = [a for a in msg.attachments
                  if (a.content_type or "").startswith("image/") or Path(a.filename).suffix.lower() in config.IMAGE_EXT]
        if not images:
            return
        job = None
        if msg.reference and msg.reference.message_id:
            job = actions["job_for_message"](msg.reference.message_id)
        job = job or actions["latest_job"]()
        if not job:
            await msg.reply("I don't have a sermon waiting for a slide right now.")
            return
        img = images[0]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ("slide" + (Path(img.filename).suffix.lower() or ".jpg"))
            await img.save(path)
            try:
                await _loop.run_in_executor(None, actions["set_cover"], job, path)
            except Exception as e:
                await msg.reply(f"⚠️ Couldn't use that image: {e}")
                return
        title = (job.get("cut") or {}).get("title") or job.get("suggest", {}).get("title") or job["name"]
        ready = job.get("status") == "ready"
        await msg.reply(f"🖼️ Got the slide for **{title}**."
                        + (" Publish is unlocked." if ready else " I'll use it when the analysis is done."))
        d = job.get("discord")
        if ready and d:
            try:
                ch = _client.get_channel(d["channel"]) or await _client.fetch_channel(d["channel"])
                orig = await ch.fetch_message(d["message"])
                await orig.edit(view=_view(job, True))
            except Exception:
                log.exception("discord: couldn't unlock buttons")

    @_client.event
    async def on_interaction(inter):
        cid = (inter.data or {}).get("custom_id", "")
        if inter.type != discord.InteractionType.component or not cid.startswith("ss:"):
            return
        if inter.user.id != config.DISCORD_USER_ID:
            await inter.response.send_message("Only the owner can do that.", ephemeral=True)
            return
        _, action, jid = cid.split(":", 2)
        await inter.response.defer(thinking=True)
        try:
            await inter.message.edit(view=None)  # no double clicks
        except Exception:
            pass
        status = "publish" if action == "publish" else "draft"
        text, ok = await _loop.run_in_executor(None, actions["approve"], jid, status)
        await inter.followup.send(text)
        if not ok:  # put the buttons back so it can be retried
            job = actions["job"](jid)
            if job:
                await inter.message.edit(view=_view(job, bool(job.get("cover"))))

    await _client.start(config.DISCORD_TOKEN)
