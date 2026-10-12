const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts.body && !(opts.body instanceof FormData)
    ? { ...opts, headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.body) } : opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
};
const fmt = (t) => {
  t = Math.max(0, Math.round(t));
  return `${Math.floor(t / 3600)}:${String(Math.floor(t / 60) % 60).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};
const parse = (s) => s.trim().split(":").reverse().reduce((a, p, i) => a + parseFloat(p || 0) * 60 ** i, 0);
const size = (b) => b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : (b / 1e6).toFixed(0) + " MB";

let status, job, segments = [], wpMeta, poll;

// ---------- browsing ----------
async function browse(path, kind = "audio", list = $("#files"), crumbs = $("#crumbs")) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}&kind=${kind}`);
  if (crumbs) {
    crumbs.innerHTML = "";
    const parts = data.path ? data.path.split("/") : [];
    const home = document.createElement("a");
    home.textContent = "All locations";
    home.onclick = () => browse("", kind, list, crumbs);
    crumbs.append(home);
    parts.forEach((p, i) => {
      const sep = document.createElement("span"); sep.className = "sep"; sep.textContent = "›";
      const a = document.createElement("a"); a.textContent = p;
      a.onclick = () => browse(parts.slice(0, i + 1).join("/"), kind, list, crumbs);
      crumbs.append(sep, a);
    });
  }
  list.innerHTML = "";
  if (data.path && !crumbs) list.append(item("⬑ ..", "", () => browse(data.path.split("/").slice(0, -1).join("/"), kind, list, crumbs)));
  // newest recordings first
  const entries = [...data.entries].sort((a, b) => a.dir !== b.dir ? (a.dir ? -1 : 1)
    : kind === "audio" ? b.name.localeCompare(a.name, undefined, { numeric: true }) : a.name.localeCompare(b.name));
  for (const e of entries) {
    const full = data.path ? `${data.path}/${e.name}` : e.name;
    list.append(e.dir
      ? item("📁 " + e.name, "", () => browse(full, kind, list, crumbs))
      : item((kind === "audio" ? "🎙 " : "🖼 ") + e.name, `${size(e.size)} · ${new Date(e.mtime * 1000).toLocaleDateString()}`,
             () => kind === "audio" ? start(full) : pickCover(full)));
  }
}
function item(label, meta, onclick) {
  const li = document.createElement("li");
  li.innerHTML = `<span></span><span class="meta"></span>`;
  li.children[0].textContent = label; li.children[1].textContent = meta;
  li.onclick = onclick;
  return li;
}

$("#localAudio").onchange = (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  const xhr = new XMLHttpRequest();
  const box = $("#uploadProgress"); box.hidden = false;
  xhr.upload.onprogress = (e) => box.textContent = `Uploading ${file.name}: ${Math.round(e.loaded / e.total * 100)}%`;
  xhr.onload = () => {
    box.hidden = true;
    if (xhr.status === 200) start(JSON.parse(xhr.responseText).virtual);
    else alert("Upload failed: " + xhr.responseText);
  };
  xhr.open("POST", "/api/upload-source"); xhr.send(fd);
};

async function loadRecent() {
  const list = $("#recent"); list.innerHTML = "";
  for (const j of await api("/api/jobs"))
    list.append(item(`${j.date} — ${j.name}`, j.published ? `posted (${j.published.status})` : j.status, () => open(j.id)));
}

// ---------- job ----------
async function start(path) {
  const j = await api("/api/jobs", { method: "POST", body: { path } });
  open(j.id);
}

async function open(id) {
  clearInterval(poll);
  job = await api(`/api/jobs/${id}`);
  history.replaceState(null, "", `#${id}`);
  $("#review").hidden = false; $("#details").hidden = true; $("#editor").hidden = true;
  $("#jobName").textContent = `${job.virtual} · recorded ${job.date}`;
  segments = [];
  $("#review").scrollIntoView({ behavior: "smooth" });
  tick();
  poll = setInterval(tick, 1500);
}

async function tick() {
  job = await api(`/api/jobs/${job.id}`);
  const busy = !["ready", "error"].includes(job.status);
  $("#progressBox").hidden = !busy && job.status !== "error";
  $("#jobMsg").textContent = job.message + (job.status === "transcribing" ? ` ${Math.round(job.progress * 100)}%` : "");
  $("#jobProgress").value = job.progress || 0;
  if (job.status === "ready" && !segments.length) await showEditor();
  if (job.status === "ready") showDetailsState();
  if (!busy && !job.encoding && !job.publishing) { clearInterval(poll); poll = null; }
}

async function showEditor() {
  segments = await api(`/api/jobs/${job.id}/transcript`);
  $("#editor").hidden = false;
  $("#waveImg").src = `/api/jobs/${job.id}/waveform.png`;
  $("#player").src = `/api/jobs/${job.id}/preview.mp3`;
  const cut = job.cut || job.proposal;
  $("#start").value = fmt(cut.start); $("#end").value = fmt(cut.end);
  $("#reason").textContent = job.proposal.source === "ai"
    ? `AI pick: ${job.proposal.reason}` : `Best guess (${job.proposal.source}): ${job.proposal.reason}`;
  const ol = $("#transcript"); ol.innerHTML = "";
  segments.forEach((s, i) => {
    const li = document.createElement("li");
    li.dataset.i = i;
    li.innerHTML = `<span class="t"></span><span class="txt"></span><span class="b"><button class="secondary" data-set="start">start here</button><button class="secondary" data-set="end">end here</button></span>`;
    li.querySelector(".t").textContent = fmt(s.start);
    li.querySelector(".txt").textContent = s.text;
    ol.append(li);
  });
  ol.onclick = (e) => {
    const li = e.target.closest("li"); if (!li) return;
    const s = segments[li.dataset.i];
    if (e.target.dataset.set === "start") $("#start").value = fmt(s.start);
    else if (e.target.dataset.set === "end") $("#end").value = fmt(s.end + 1);
    else { $("#player").currentTime = s.start; $("#player").play(); }
    drawCut();
  };
  drawCut();
  scrollToLine(parse($("#start").value));
  await showDetails();
}

function drawCut() {
  const d = job.duration, s = parse($("#start").value), e = parse($("#end").value);
  $("#region").style.left = `${s / d * 100}%`;
  $("#region").style.width = `${Math.max(0, e - s) / d * 100}%`;
  $("#length").textContent = e > s ? fmt(e - s) : "—";
  document.querySelectorAll("#transcript li").forEach((li) => {
    const seg = segments[li.dataset.i];
    li.classList.toggle("in", seg.start >= s - 0.5 && seg.end <= e + 0.5);
  });
}
function scrollToLine(t) {
  const i = segments.findIndex((s) => s.end >= t);
  const li = document.querySelector(`#transcript li[data-i="${Math.max(i - 2, 0)}"]`);
  if (li) li.parentElement.scrollTop = li.offsetTop - li.parentElement.offsetTop;
}
["#start", "#end"].forEach((s) => $(s).addEventListener("change", drawCut));
$("#wave").onclick = (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  $("#player").currentTime = (e.clientX - r.left) / r.width * job.duration;
  $("#player").play();
};
let lastNow = -1;
$("#player").ontimeupdate = () => {
  const t = $("#player").currentTime;
  $("#playhead").style.left = `${t / job.duration * 100}%`;
  const i = segments.findIndex((s) => s.end >= t);
  if (i !== lastNow) {
    document.querySelector("#transcript li.now")?.classList.remove("now");
    document.querySelector(`#transcript li[data-i="${i}"]`)?.classList.add("now");
    lastNow = i;
  }
};
document.querySelector(".cuts").onclick = (e) => {
  const act = e.target.dataset.act, p = $("#player");
  if (act === "setStart") $("#start").value = fmt(p.currentTime);
  if (act === "setEnd") $("#end").value = fmt(p.currentTime);
  if (act === "playStart") { p.currentTime = parse($("#start").value); p.play(); scrollToLine(p.currentTime); }
  if (act === "playEnd") { p.currentTime = parse($("#end").value) - 20; p.play(); scrollToLine(p.currentTime); }
  drawCut();
};
$("#redetect").onclick = async (e) => {
  e.preventDefault();
  await api(`/api/jobs/${job.id}/redetect`, { method: "POST" });
  segments = []; $("#editor").hidden = true; $("#details").hidden = true;
  poll = poll || setInterval(tick, 1500); tick();
};
$("#search").oninput = (e) => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll("#transcript li").forEach((li) =>
    li.classList.toggle("hide", q && !segments[li.dataset.i].text.toLowerCase().includes(q)));
};

// ---------- details ----------
async function showDetails() {
  $("#details").hidden = false;
  const cut = job.cut || {};
  const st = status.settings || {};
  $("#title").value = cut.title || job.suggest?.title || "";
  $("#scripture").value = cut.scripture || job.suggest?.scripture || "";
  $("#speaker").value = cut.speaker || "";
  $("#date").value = cut.date || job.date;
  $("#bitrate").value = cut.bitrate || st.bitrate || status.bitrate;
  $("#normalize").checked = cut.normalize ?? true;
  showCover();
  await loadSeries(cut.series);
  showDetailsState();
}

async function loadSeries(selected) {
  const sel = $("#series");
  if (!wpMeta) wpMeta = await api("/api/wp/meta").catch((e) => ({ series: [], error: e.message }));
  sel.innerHTML = "";
  const none = new Option("(no series)", ""); sel.append(none);
  for (const s of wpMeta.series) sel.append(new Option(s.name, s.id));
  sel.value = selected || wpMeta.default_series || "";
  $("#speakers").innerHTML = "";
  for (const p of wpMeta.speakers || []) $("#speakers").append(new Option(p));
  if (!$("#speaker").value) $("#speaker").value = wpMeta.default_speaker || (status.settings || {}).speaker || "";
  $("#seriesNote").textContent = !wpMeta.configured ? "WordPress login not set up yet."
    : wpMeta.error ? wpMeta.error
    : wpMeta.last_post ? `Defaulted to the series and speaker of the last edited sermon: “${wpMeta.last_post}”` : "";
}

function showCover() {
  const img = $("#coverImg");
  img.hidden = !job.cover;
  if (job.cover) img.src = `/api/jobs/${job.id}/cover.jpg?${Date.now()}`;
}
$("#coverFile").onchange = async (ev) => {
  const fd = new FormData(); fd.append("file", ev.target.files[0]);
  await api(`/api/jobs/${job.id}/cover`, { method: "POST", body: fd }).catch((e) => alert(e.message));
  job.cover = true; showCover();
};
$("#coverBrowse").onclick = () => {
  const list = $("#coverFiles"); list.hidden = !list.hidden;
  if (!list.hidden) browse(job.virtual.split("/").slice(0, -1).join("/"), "image", list, null);
};
async function pickCover(path) {
  await api(`/api/jobs/${job.id}/cover-path`, { method: "POST", body: { path } }).catch((e) => alert(e.message));
  $("#coverFiles").hidden = true; job.cover = true; showCover();
}

function showDetailsState() {
  $("#encode").disabled = !!job.encoding;
  $("#encodeMsg").textContent = job.encoding ? "Encoding…" : /Encoding failed/.test(job.message) ? job.message : job.output ? `${job.output.name} · ${size(job.output.size)} · ${fmt(job.output.duration)}` : "";
  $("#outputBox").hidden = !job.output || job.encoding;
  if (job.output) {
    const url = `/api/jobs/${job.id}/output.mp3?v=${job.output.size}`;
    if (!$("#outPlayer").src.endsWith(url)) { $("#outPlayer").src = url; $("#download").href = url; }
    $("#download").download = job.output.name;
  }
  const canPost = status.wp_configured && !job.publishing && !job.encoding;
  $("#draft").disabled = $("#publish").disabled = !canPost;
  const p = job.published;
  $("#publishMsg").innerHTML = "";
  if (job.publishing || /failed/i.test(job.message)) $("#publishMsg").textContent = job.message;
  else if (p) $("#publishMsg").innerHTML = `${p.status === "publish" ? "Published" : "Draft saved"} · <a target="_blank" href="${p.edit}">edit in WordPress</a>` + (p.status === "publish" ? ` · <a target="_blank" href="${p.link}">view</a>` : "");
  else if (!status.wp_configured) $("#publishMsg").textContent = "Add WordPress login to .env to enable uploading.";
}

$("#encode").onclick = async () => {
  const sel = $("#series");
  const body = {
    start: parse($("#start").value), end: parse($("#end").value), title: $("#title").value.trim(),
    speaker: $("#speaker").value.trim(), series: sel.value, series_name: sel.value ? sel.selectedOptions[0].text : "",
    scripture: $("#scripture").value.trim(), date: $("#date").value, bitrate: $("#bitrate").value,
    normalize: $("#normalize").checked,
  };
  if (!body.title) return alert("Give the sermon a title first.");
  try {
    await api(`/api/jobs/${job.id}/encode`, { method: "POST", body });
    job.encoding = true; showDetailsState();
    poll = poll || setInterval(tick, 1500);
  } catch (e) { alert(e.message); }
};
async function upload(st) {
  if (st === "publish" && !confirm("Publish this sermon on the church website now?")) return;
  if (job.published && !confirm("This recording was already uploaded once. Upload again as a new post?")) return;
  await api(`/api/jobs/${job.id}/publish`, { method: "POST", body: { status: st } }).catch((e) => alert(e.message));
  job.publishing = true; showDetailsState();
  poll = poll || setInterval(tick, 1500);
}
$("#draft").onclick = () => upload("draft");
$("#publish").onclick = () => upload("publish");

// ---------- boot ----------
(async () => {
  status = await api("/api/status");
  $("#engine").textContent = `AI: ${status.ai.model} on ${status.ai.device.toUpperCase()}` + (status.llm ? ` + ${status.llm}` : "");
  await browse(status.start_path).catch(() => browse(""));
  loadRecent();
  if (location.hash.length > 1) open(location.hash.slice(1)).catch(() => {});
})();
