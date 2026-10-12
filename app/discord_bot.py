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
actions = {}


def enabled():
    return bool(config.DISCORD_TOKEN and config.DISCORD_USER_ID)


def start(acts):
    if not enabled():
        return
    actions.update(acts)
    threading.Thread(target=lambda: asyncio.run(_main()), daemon=True, name="discord").start()


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


def _view(job, enabled_buttons):
    import discord
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Publish", emoji="✅", style=discord.ButtonStyle.success,
                                 custom_id=f"ss:publish:{job['id']}", disabled=not enabled_buttons))
    v.add_item(discord.ui.Button(label="Save as draft", emoji="📝", style=discord.ButtonStyle.secondary,
                                 custom_id=f"ss:draft:{job['id']}", disabled=not enabled_buttons))
    if _studio_link(job):
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
        text = ("✅ **The sermon is ready.** Listen to how it starts and ends (attached).\n"
                + ("Slide is attached already. Press **Publish** when it looks right."
                   if has_cover else
                   "📎 **Reply with this week's sermon slide** (an image) and I'll attach it to the post. "
                   "Publish unlocks once it's here."))
        msg = await ch.send(text, embed=embed, files=files, view=_view(job, has_cover))
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
        log.info("discord: logged in as %s", _client.user)
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
