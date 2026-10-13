"""WordPress client that drives wp-admin like a browser.

The Church Co blocks the REST API for everyone (every route answers {"message": "God Bless"}),
so this logs in with a username/password and fills in the classic "Add New Podcast" form:

  title          -> post_title
  description    -> content (2-3 sentences written by the local LLM, edited by a person)
  scripture      -> excerpt
  MP3            -> media library, attached in the ACF "Audio" file field
  cover image    -> media library, featured image
  feed image     -> copied from the most recent sermon (the same podcast artwork every week)
  speaker        -> "People" taxonomy (e.g. "Jane Doe")
  series         -> "podcast-category" checkboxes, plus "All Sermons"
"""
import html
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

from . import config


class WPError(Exception):
    pass


class DraftGone(WPError):
    """The draft we created earlier was deleted or trashed in WP Admin."""


class FormParser(HTMLParser):
    """Collects the successful controls of <form name="post"> as a list of (name, value)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields, self.in_form, self.select, self.textarea, self.text = [], False, None, None, ""
        self.first_option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("name") == "post":
            self.in_form = True
        if not self.in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            t = a.get("type", "text").lower()
            if t in ("submit", "button", "image", "file", "reset"):
                return
            if t in ("checkbox", "radio") and "checked" not in a:
                return
            self.fields.append((name, a.get("value", "on" if t == "checkbox" else "")))
        elif tag == "select" and name:
            self.select, self.first_option = [name, None], None
        elif tag == "option" and self.select:
            if self.first_option is None:
                self.first_option = a.get("value", "")
            if "selected" in a:
                self.select[1] = a.get("value", "")
        elif tag == "textarea" and name:
            self.textarea, self.text = name, ""

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form = False
        elif tag == "select" and self.select:
            self.fields.append((self.select[0], self.select[1] if self.select[1] is not None else self.first_option or ""))
            self.select = None
        elif tag == "textarea" and self.textarea:
            self.fields.append((self.textarea, self.text))
            self.textarea = None

    def handle_data(self, data):
        if self.textarea:
            self.text += data


def form_fields(page):
    p = FormParser()
    p.feed(page)
    # ACF repeater templates are not real rows
    return [(k, v) for k, v in p.fields if "acfcloneindex" not in k]


def acf_key(page, name):
    m = re.search(rf'data-name="{re.escape(name)}"[^>]*data-key="([^"]+)"', page)
    if not m:
        raise WPError(f"The podcast form has no '{name}' field any more.")
    return m.group(1)


def categories(page):
    """[(id, name, checked)] from the podcast-category checklist."""
    out = []
    for m in re.finditer(r'<input\b([^>]*name="tax_input\[podcast-category\]\[\]"[^>]*)/?>\s*([^<]+)<', page):
        attrs = m.group(1)
        out.append((int(re.search(r'value="(\d+)"', attrs).group(1)), html.unescape(m.group(2)).strip(),
                    "checked" in attrs))
    seen, uniq = set(), []
    for c in out:  # the "Most Used" tab repeats some
        if c[0] not in seen:
            seen.add(c[0])
            uniq.append(c)
    return uniq


class WordPress:
    def __init__(self):
        self.s = None
        self.logged_in_at = 0
        self.lock = threading.Lock()

    @property
    def base(self):
        return config.WP_URL

    def reset(self):
        """Forget the session (after the login settings change)."""
        self.s, self.logged_in_at = None, 0

    @property
    def configured(self):
        return bool(self.base and config.WP_USER and config.WP_PASSWORD)

    # --- session -------------------------------------------------------------
    def login(self, force=False):
        if not self.configured:
            raise WPError("WordPress login isn't set (WP_USER / WP_PASSWORD in .env).")
        if self.s and not force and time.time() - self.logged_in_at < 3600:
            return
        s = requests.Session()
        s.headers["User-Agent"] = "SermonStudio/1.0"
        s.get(f"{self.base}/wp-login.php", timeout=30)
        r = s.post(f"{self.base}/wp-login.php", timeout=30, data={
            "log": config.WP_USER, "pwd": config.WP_PASSWORD, "rememberme": "forever",
            "wp-submit": "Log In", "redirect_to": f"{self.base}/wp-admin/", "testcookie": "1",
        })
        if not any(c.name.startswith("wordpress_logged_in") for c in s.cookies):
            msg = re.search(r'id="login_error"[^>]*>(.*?)</div>', r.text, re.S)
            raise WPError("WordPress login failed: " + (html.unescape(re.sub(r"<[^>]+>", "", msg.group(1))).strip() if msg
                                                     else "no session cookie (2FA or a captcha on the login page?)"))
        self.s, self.logged_in_at = s, time.time()

    def admin(self, method, path, **kw):
        with self.lock:
            self.login()
            r = self.s.request(method, f"{self.base}/wp-admin/{path}", timeout=kw.pop("timeout", 60), **kw)
            if "wp-login.php" in r.url:
                self.login(force=True)
                r = self.s.request(method, f"{self.base}/wp-admin/{path}", timeout=60, **kw)
        if r.status_code >= 400:
            raise WPError(f"wp-admin/{path.split('?')[0]}: HTTP {r.status_code}")
        return r

    # --- reading -------------------------------------------------------------
    def latest_post_id(self):
        page = self.admin("GET", f"edit.php?post_type={config.WP_POST_TYPE}&orderby=modified&order=desc&post_status=all").text
        m = re.search(r'<tr id="post-(\d+)"', page)
        return int(m.group(1)) if m else None

    def people(self):
        page = self.admin("GET", f"edit-tags.php?taxonomy=people&post_type={config.WP_POST_TYPE}").text
        return sorted({html.unescape(n).strip() for n in re.findall(r'class="row-title"[^>]*>([^<]+)<', page)})

    def meta(self):
        """Series list + defaults taken from the most recently edited sermon."""
        pid = self.latest_post_id()
        if pid:
            page = self.admin("GET", f"post.php?post={pid}&action=edit").text
        else:
            page = self.admin("GET", f"post-new.php?post_type={config.WP_POST_TYPE}").text
        cats = categories(page)
        always = config.WP_ALWAYS_CATEGORY.lower()
        series = [{"id": c[0], "name": c[1]} for c in cats if c[1].lower() != always]
        checked = [c[0] for c in cats if c[2] and c[1].lower() != always]
        title = re.search(r'name="post_title"[^>]*value="([^"]*)"', page)
        people = re.search(r'name="tax_input\[people\]"[^>]*>(.*?)</textarea>', page, re.S)
        series.sort(key=lambda s: s["name"].lower())
        return {"series": series, "default_series": checked[0] if checked else None,
                "speakers": self.people(),
                "default_speaker": html.unescape(people.group(1)).split(",")[0].strip() if people else "",
                "last_post": html.unescape(title.group(1)) if title and pid else None}

    def discover(self):
        page = self.admin("GET", f"post-new.php?post_type={config.WP_POST_TYPE}").text
        acf = re.findall(r'data-name="([^"]+)"[^>]*data-type="([^"]+)"[^>]*data-key="([^"]+)"', page)
        return {"fields": sorted({k for k, _ in form_fields(page)}), "acf": acf,
                "categories": categories(page), **self.meta()}

    def post_state(self, post_id):
        """(status, permalink) of an existing post, or (None, None) if it's gone/trashed."""
        page = self.admin("GET", f"post.php?post={post_id}&action=edit").text
        st = re.search(r"name='?\"?original_post_status'?\"? value='?\"?(\w+)", page)
        if 'name="post_title"' not in page or not st or st.group(1) == "trash":
            return None, None
        link = re.search(r'id="sample-permalink"[^>]*>\s*<a href="([^"]+)"', page)
        return st.group(1), html.unescape(link.group(1)) if link else None

    def update_post(self, post_id, people=None, content_if_empty=None, mp3: Path | None = None):
        """Change an existing post in place (keeps its status, title and date). Returns what changed."""
        page = self.admin("GET", f"post.php?post={post_id}&action=edit").text
        if 'name="post_title"' not in page:
            raise WPError(f"Post {post_id} isn't in WordPress any more.")
        form = form_fields(page)
        cur = dict(form)
        override, changed = {}, []
        if people is not None and cur.get("tax_input[people]", "").strip() != people:
            override["tax_input[people]"] = people
            changed.append(f"speaker → {people}")
        if content_if_empty and not re.sub(r"<[^>]+>|\s|&nbsp;", "", cur.get("content", "")):
            override["content"] = content_if_empty
            changed.append("description added")
        if mp3:
            nonce = re.search(r'"action":"upload-attachment","_wpnonce":"([0-9a-f]+)"', page)
            if not nonce:
                raise WPError("Couldn't find the media upload token.")
            a = self.upload_media(mp3, "audio/mpeg", post_id, nonce.group(1))
            override[f"acf[{acf_key(page, 'podcast_audio')}]"] = str(a["id"])
            changed.append("audio replaced")
        if not override:
            return []
        data = [(k, v) for k, v in form if k not in override] + list(override.items()) + [("save", "Update")]
        r = self.admin("POST", "post.php", data=data, timeout=120)
        if f"post={post_id}" not in r.url:
            raise WPError("WordPress didn't accept the change.")
        return changed

    # --- writing -------------------------------------------------------------
    def upload_media(self, path: Path, mime: str, post_id: int, nonce: str):
        with path.open("rb") as f:
            r = self.admin("POST", "async-upload.php", timeout=1800,
                           data={"action": "upload-attachment", "_wpnonce": nonce, "post_id": str(post_id),
                                 "name": path.name},
                           files={"async-upload": (path.name, f, mime)})
        try:
            res = r.json()
        except ValueError:
            raise WPError(f"Uploading {path.name} failed: {re.sub(r'<[^>]+>', ' ', r.text)[:200].strip()}")
        if not res.get("success"):
            raise WPError(f"Uploading {path.name} failed: {res.get('data', {}).get('message', res)}")
        return res["data"]

    def publish(self, mp3: Path, cover: Path | None, fields: dict, status: str, progress,
                post_id: int | None = None, reuse_media: bool = False):
        """Create a sermon post, or update an existing draft (post_id). reuse_media keeps the draft's
        MP3 and cover instead of uploading them again."""
        progress("Opening the draft…" if post_id else "Opening a new sermon post…")
        latest = self.latest_post_id()
        feed_image = None
        if latest:
            prev = self.admin("GET", f"post.php?post={latest}&action=edit").text
            m = re.search(rf'name="acf\[{acf_key(prev, "podcast_feed_image")}\]"[^>]*value="(\d+)"', prev)
            feed_image = m.group(1) if m else None
        if post_id:
            page = self.admin("GET", f"post.php?post={post_id}&action=edit").text
            st = re.search(r"name='?\"?original_post_status'?\"? value='?\"?(\w+)", page)
            if 'name="post_title"' not in page or not st or st.group(1) == "trash":
                raise DraftGone(f"Draft {post_id} isn't in WordPress any more.")
        else:
            page = self.admin("GET", f"post-new.php?post_type={config.WP_POST_TYPE}").text
        form = form_fields(page)
        post_id = int(dict(form)["post_ID"])
        nonce = re.search(r'"action":"upload-attachment","_wpnonce":"([0-9a-f]+)"', page)
        if not nonce and not reuse_media:
            raise WPError("Couldn't find the media upload token on the post page.")

        audio = image = None
        if not reuse_media:
            progress("Uploading MP3…")
            audio = self.upload_media(mp3, "audio/mpeg", post_id, nonce.group(1))
            if cover and cover.exists():
                progress("Uploading cover image…")
                image = self.upload_media(cover, "image/jpeg", post_id, nonce.group(1))

        progress("Publishing…" if status == "publish" else "Saving the draft…")
        y, mo, d = fields["date"].split("-")
        scripture = fields.get("scripture", "")
        cats = [str(c[0]) for c in categories(page) if c[1].lower() == config.WP_ALWAYS_CATEGORY.lower()]
        if fields.get("series"):
            cats.append(str(fields["series"]))
        override = {
            "post_title": fields["title"], "content": fields.get("description") or scripture, "excerpt": scripture,
            "tax_input[people]": fields.get("speaker", ""),
            "aa": y, "mm": mo, "jj": d, "hh": "10", "mn": "00", "ss": "00", "edit_date": "1",
            "post_status": "draft", "hidden_post_status": "draft",
        }
        if audio:
            override[f"acf[{acf_key(page, 'podcast_audio')}]"] = str(audio["id"])
        if feed_image:
            override[f"acf[{acf_key(page, 'podcast_feed_image')}]"] = feed_image
        if image:
            override["_thumbnail_id"] = str(image["id"])
        data = [(k, v) for k, v in form if k not in override and k != "tax_input[podcast-category][]"]
        data += list(override.items()) + [("tax_input[podcast-category][]", c) for c in cats]
        data.append(("publish", "Publish") if status == "publish" else ("save", "Save Draft"))
        r = self.admin("POST", "post.php", data=data, timeout=120)
        if f"post={post_id}" not in r.url:
            raise WPError("WordPress didn't accept the post (no redirect back to the editor).")

        saved = self.admin("GET", f"post.php?post={post_id}&action=edit").text
        st = re.search(r"name='?\"?original_post_status'?\"? value='?\"?(\w+)", saved)
        link = re.search(r'id="sample-permalink"[^>]*>\s*<a href="([^"]+)"', saved) or \
            re.search(r'<a[^>]+href="([^"]+)"[^>]*>\s*View (?:Podcast|Post)', saved)
        return {"id": post_id, "status": st.group(1) if st else status,
                "link": html.unescape(link.group(1)) if link else None,
                "edit": f"{self.base}/wp-admin/post.php?post={post_id}&action=edit",
                "audio_url": audio.get("url") if audio else None}


wp = WordPress()
