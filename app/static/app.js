const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
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
const size = (b) => b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : (b / 1e6).toFixed(1) + " MB";
const niceDate = (d) => new Date(d + "T12:00").toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" });

let status, job = null, step = 1, segments = [], wpMeta = null, poll = null, formLoadedFor = null, waitingDescribe = false;
const uploads = { audio: null, image: null };
let uploading = 0;  // uploads in flight
let encStart = null;  // {t, f} when we first saw the encode progress, for the time estimate

// ======================================================================== wizard
function reachable(n) {
  if (n === 1) return true;
  if (!job) return false;
  if (n === 2) return true;
  if (n === 3) return job.status === "ready";
  if (n === 4) return !!(job.output || job.encoding);
}

function go(n) {
  if (!reachable(n)) return;
  step = n;
  $$("section.step").forEach((s) => s.hidden = +s.dataset.step !== n);
  history.replaceState(null, "", job ? `#${job.id}/${n}` : "#");
  renderStepper();
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (n === 2) { loadArchive(); }
  if (n === 3) { showReview(); }
  if (n === 4) { renderPublish(); }
  ensurePolling();
}

function renderStepper() {
  $$("#stepper li").forEach((li) => {
    const n = +li.dataset.step;
    // everything before the step you're on (or the furthest one reached) is finished
    li.className = n === step ? "current" : n < step || n < furthest() ? "done" : "";
    if (n !== step && reachable(n)) li.classList.add("reachable");
  });
}
function furthest() {
  if (!job) return 1;
  if (job.published) return 5;
  if (job.output || job.encoding) return 4;
  if (job.status === "ready") return 3;
  return 2;
}
$$("#stepper li").forEach((li) => li.onclick = () => go(+li.dataset.step));
$$("[data-go]").forEach((b) => b.onclick = () => go(+b.dataset.go));

function ensurePolling() {
  const busy = job && (!["ready", "error", "cancelled"].includes(job.status) || job.encoding || job.publishing || job.describing
    || job.archive?.status === "saving");
  if (busy && !poll) poll = setInterval(refresh, 1500);
  if (!busy && poll) { clearInterval(poll); poll = null; }
}

async function refresh() {
  if (!job) return;
  job = await api(`/api/jobs/${job.id}`);
  renderStepper();
  if (step === 2) renderAnalyze();
  if (step === 3) renderReviewState();
  if (step === 4) renderPublish();
  ensurePolling();
}

async function openJob(id, wanted) {
  job = await api(`/api/jobs/${id}`);
  segments = []; formLoadedFor = null;
  $$(".jobName").forEach((e) => e.textContent = `${job.name} · recorded ${niceDate(job.date)}`);
  go(Math.min(wanted || furthest(), 4));
}

// ======================================================================== step 1: upload
function setupDrop(el, kind) {
  const input = $("input", el);
  el.onclick = () => input.click();
  el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") input.click(); };
  input.onchange = () => input.files[0] && upload(el, kind, input.files[0]);
  el.ondragover = (e) => { e.preventDefault(); el.classList.add("over"); };
  el.ondragleave = () => el.classList.remove("over");
  el.ondrop = (e) => {
    e.preventDefault(); el.classList.remove("over");
    if (e.dataTransfer.files[0]) upload(el, kind, e.dataTransfer.files[0]);
  };
}

function upload(el, kind, file) {
  const bar = $("progress", el), label = $(".file", el);
  el.classList.remove("done");
  uploads[kind] = null;
  updateNext();
  if (kind === "image") {
    const img = $(".preview", el);
    img.src = URL.createObjectURL(file); img.hidden = false;
  }
  const fd = new FormData();
  fd.append("file", file);
  if (kind === "audio") fd.append("last_modified", file.lastModified);
  const xhr = new XMLHttpRequest();
  bar.hidden = false; bar.value = 0;
  label.textContent = `Uploading ${file.name}…`;
  uploading++;
  xhr.onloadend = () => uploading--;
  xhr.upload.onprogress = (e) => {
    bar.value = e.loaded / e.total;
    label.textContent = `Uploading ${file.name}: ${Math.round(e.loaded / e.total * 100)}% of ${size(e.total)}`;
  };
  xhr.onload = () => {
    bar.hidden = true;
    if (xhr.status !== 200) { label.textContent = "Upload failed: " + (JSON.parse(xhr.responseText || "{}").detail || xhr.status); return; }
    uploads[kind] = JSON.parse(xhr.responseText);
    label.textContent = `${file.name} (${size(file.size)})`;
    el.classList.add("done");
    updateNext();
  };
  xhr.onerror = () => { bar.hidden = true; label.textContent = "Upload failed (connection lost)"; };
  xhr.open("POST", `/api/upload/${kind}`);
  xhr.send(fd);
}

function setPicked(kind, virtual) {
  const el = kind === "audio" ? $("#dropAudio") : $("#dropImage");
  uploads[kind] = { virtual, name: virtual.split("/").pop() };
  $(".file", el).textContent = `${uploads[kind].name} (from the NAS)`;
  el.classList.add("done");
  $("#nasPicker").hidden = true;
  updateNext();
}

function updateNext() { $("#toAnalyze").disabled = !uploads.audio; }

$("#toAnalyze").onclick = async () => {
  if (!uploads.image && !confirm("Continue without a cover image? You can add one in the Review step.")) return;
  $("#toAnalyze").disabled = true;
  try {
    const j = await api("/api/jobs", { method: "POST", body: { path: uploads.audio.virtual, cover: uploads.image?.virtual || "" } });
    await openJob(j.id, 2);
  } catch (e) { alert(e.message); }
  $("#toAnalyze").disabled = false;
};

async function loadRecent() {
  const list = $("#recent"); list.innerHTML = "";
  for (const j of await api("/api/jobs"))
    list.append(item(`${niceDate(j.date)} · ${j.name}`, j.published ? `posted (${j.published.status})` : j.status === "ready" ? "ready to review" : j.status,
                     () => openJob(j.id)));
}

// generic folder browser used by both pickers
async function browse(box, path, kind, onFile) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}&kind=${kind}`);
  box.dataset.path = data.path;
  const crumbs = $(".crumbs", box); crumbs.innerHTML = "";
  const parts = data.path ? data.path.split("/") : [];
  const home = document.createElement("a"); home.textContent = "All locations";
  home.onclick = () => browse(box, "", kind, onFile);
  crumbs.append(home);
  parts.forEach((p, i) => {
    const sep = document.createElement("span"); sep.className = "sep"; sep.textContent = "›";
    const a = document.createElement("a"); a.textContent = p;
    a.onclick = () => browse(box, parts.slice(0, i + 1).join("/"), kind, onFile);
    crumbs.append(sep, a);
  });
  const list = $(".files", box); list.innerHTML = "";
  const entries = [...data.entries].sort((a, b) => a.dir !== b.dir ? (a.dir ? -1 : 1) : b.name.localeCompare(a.name, undefined, { numeric: true }));
  for (const e of entries) {
    const full = data.path ? `${data.path}/${e.name}` : e.name;
    list.append(e.dir
      ? item("📁 " + e.name, "", () => browse(box, full, kind, onFile))
      : item((kind === "audio" ? "🎙 " : "🖼 ") + e.name, `${size(e.size)} · ${new Date(e.mtime * 1000).toLocaleDateString()}`, () => onFile(full)));
  }
}
function item(label, meta, onclick) {
  const li = document.createElement("li");
  li.innerHTML = `<span></span><span class="meta"></span>`;
  li.children[0].textContent = label; li.children[1].textContent = meta;
  li.onclick = onclick;
  return li;
}
$("#pickNasAudio").onclick = (e) => {
  e.preventDefault(); $("#nasPicker").hidden = false;
  browse($("#nasPicker"), status.start_path, "audio", (p) => setPicked("audio", p)).catch(() => browse($("#nasPicker"), "", "audio", (p) => setPicked("audio", p)));
};
$("#pickNasImage").onclick = (e) => {
  e.preventDefault(); $("#nasPicker").hidden = false;
  browse($("#nasPicker"), status.start_path, "image", (p) => setPicked("image", p)).catch(() => browse($("#nasPicker"), "", "image", (p) => setPicked("image", p)));
};
$("[data-close]").onclick = () => $("#nasPicker").hidden = true;

// ======================================================================== step 2: analyze
const ORDER = ["queued", "preparing", "transcribing", "detecting", "describing", "ready"];

async function loadArchive() {
  renderAnalyze();
  const a = job.archive || {};
  if (a.folder) { $("#folder").textContent = a.folder; renderArchive(); return; }
  const rec = await api(`/api/jobs/${job.id}/archive`);
  $("#folder").textContent = rec.folder;
  $("#folderTag").textContent = rec.exists ? "existing folder" : "new folder";
  $("#folderTag").className = "tag" + (rec.exists ? "" : " new");
  renderArchive();
}

function renderArchive() {
  const a = job.archive || { status: "none" };
  if (a.folder) $("#folder").textContent = a.folder;
  const saved = a.status === "saved", saving = a.status === "saving";
  if (saved) { $("#folderTag").textContent = "saved"; $("#folderTag").className = "tag ok"; }
  $("#saveNas").hidden = saved;
  $("#changeFolder").hidden = saved || saving;
  $("#saveNas").disabled = saving;
  $("#archiveMsg").textContent = saving ? "Copying to the NAS…" : a.message || "";
}

$("#saveNas").onclick = async () => {
  try {
    await api(`/api/jobs/${job.id}/archive`, { method: "POST", body: { folder: $("#folder").textContent } });
    job.archive = { ...job.archive, status: "saving" }; renderArchive(); ensurePolling();
  } catch (e) { alert(e.message); }
};
$("#changeFolder").onclick = () => {
  const box = $("#folderPicker"); box.hidden = !box.hidden;
  if (!box.hidden) browse(box, $("#folder").textContent.split("/").slice(0, -1).join("/"), "dir", () => {})
    .catch(() => browse(box, status.archive_root, "dir", () => {}));
};
$("#useFolder").onclick = () => {
  const box = $("#folderPicker");
  $("#folder").textContent = box.dataset.path;
  $("#folderTag").textContent = "your choice"; $("#folderTag").className = "tag";
  box.hidden = true;
};

function renderAnalyze() {
  renderArchive();
  const idx = ORDER.indexOf(job.status);
  $$("#checklist li").forEach((li) => {
    const i = ORDER.indexOf(li.dataset.s);
    li.className = ["error", "cancelled"].includes(job.status) ? "" : i < idx ? "done" : i === idx ? "active" : "";
  });
  if (job.status === "error") {
    const active = $$("#checklist li").find((li) => !li.classList.contains("done"));
    active?.classList.add("failed");
  }
  $("#checklist .pct").textContent = job.status === "transcribing" ? `${Math.round((job.progress || 0) * 100)}%` : "";
  const overall = job.status === "ready" ? 1 : idx <= 1 ? 0.02 : idx === 2 ? 0.05 + 0.75 * (job.progress || 0) : idx === 3 ? 0.85 : 0.95;
  $("#jobProgress").value = overall;
  $("#jobMsg").textContent = job.status === "ready" ? "Done. The sermon has been found." : job.message;
  $("#toReview").disabled = job.status !== "ready";
  const running = !["ready", "error", "cancelled"].includes(job.status);
  $("#stopAnalysis").hidden = !running;
  $("#restartAnalysis").hidden = !["error", "cancelled"].includes(job.status);
}
$("#toReview").onclick = () => go(3);
$("#stopAnalysis").onclick = async () => {
  if (!confirm("Stop the analysis?" + (job.proposal ? " The previous start/end times will be kept." : " You can start it again later."))) return;
  await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).catch((e) => alert(e.message));
  $("#jobMsg").textContent = "Stopping…";
};
$("#restartAnalysis").onclick = async () => {
  await api(`/api/jobs/${job.id}/redetect`, { method: "POST" });
  job = await api(`/api/jobs/${job.id}`); renderAnalyze(); ensurePolling();
};

// ======================================================================== step 3: review
async function showReview() {
  if (!segments.length) {
    segments = await api(`/api/jobs/${job.id}/transcript`);
    buildTranscript();
  }
  $("#waveImg").src = `/api/jobs/${job.id}/waveform.png`;
  if (!$("#player").src.includes(job.id)) $("#player").src = `/api/jobs/${job.id}/preview.mp3`;
  renderReviewState();
  if (formLoadedFor !== job.id) {
    $("#toPublish").disabled = true;
    await fillForm();
    $("#toPublish").disabled = false;
  }
}

function buildTranscript() {
  const ol = $("#transcript"); ol.innerHTML = "";
  segments.forEach((s, i) => {
    const li = document.createElement("li");
    li.dataset.i = i;
    li.innerHTML = `<span class="t"></span><span class="txt"></span><span class="b"><button class="secondary" data-set="start">start here</button><button class="secondary" data-set="end">end here</button></span>`;
    $(".t", li).textContent = fmt(s.start);
    $(".txt", li).textContent = s.text;
    ol.append(li);
  });
}

async function fillForm() {
  formLoadedFor = job.id;
  const cut = job.cut || {}, sug = job.suggest || {}, prop = job.proposal;
  $("#start").value = fmt(cut.start ?? prop.start);
  $("#end").value = fmt(cut.end ?? prop.end);
  $("#reason").textContent = prop.source === "ai" ? `The AI's pick: ${prop.reason}` : `Best guess (${prop.source}): ${prop.reason}`;
  $("#title").value = cut.title || sug.title || "";
  $("#scripture").value = cut.scripture || sug.scripture || "";
  $("#description").value = cut.description || sug.description || "";
  $("#date").value = cut.date || job.date;
  $("#speaker").value = cut.speaker || "";
  const st = status.settings || {};
  $("#bitrate").value = cut.bitrate || st.bitrate || status.bitrate;
  $("#normalize").checked = cut.normalize ?? true;
  drawCut();
  await loadMeta(cut.series);
}

async function loadMeta(selected) {
  const sel = $("#series");
  if (!wpMeta) {
    $("#seriesNote").textContent = "Loading series and speakers from WordPress…";
    wpMeta = await api("/api/wp/meta").catch((e) => ({ series: [], error: e.message }));
  }
  sel.innerHTML = "";
  sel.append(new Option("(no series)", ""));
  for (const s of wpMeta.series) sel.append(new Option(s.name, s.id));
  sel.value = selected || wpMeta.default_series || "";
  $("#speakers").innerHTML = "";
  for (const p of wpMeta.speakers || []) $("#speakers").append(new Option(p));
  if (!$("#speaker").value) $("#speaker").value = wpMeta.default_speaker || status.settings?.speaker || "";
  $("#seriesNote").textContent = !wpMeta.configured ? "WordPress login isn't set up."
    : wpMeta.error ? wpMeta.error
    : wpMeta.last_post ? `Same as the last sermon you edited (“${wpMeta.last_post}”)` : "";
}

function renderReviewState() {
  $("#coverImg").hidden = !job.cover; $("#noCover").hidden = !!job.cover;
  if (job.cover && !$("#coverImg").src.includes(job.id)) $("#coverImg").src = `/api/jobs/${job.id}/cover.jpg?${Date.now()}`;
  $("#rewrite").disabled = !!job.describing || !status.llm;
  $("#rewriteMsg").textContent = job.describing ? "Writing… (a minute or two on the CPU)" : "";
  if (waitingDescribe && !job.describing) {
    waitingDescribe = false;
    $("#description").value = job.suggest.description || $("#description").value;
    if (job.suggest.title && confirm(`Use the suggested title “${job.suggest.title}”?`)) $("#title").value = job.suggest.title;
    if (job.suggest.scripture) $("#scripture").value = job.suggest.scripture;
  }
}

function drawCut() {
  const d = job.duration, s = parse($("#start").value), e = parse($("#end").value);
  $("#region").style.left = `${s / d * 100}%`;
  $("#region").style.width = `${Math.max(0, e - s) / d * 100}%`;
  $("#length").textContent = e > s ? fmt(e - s) : "—";
  $$("#transcript li").forEach((li) => {
    const seg = segments[li.dataset.i];
    li.classList.toggle("in", seg.start >= s - 0.5 && seg.end <= e + 0.5);
  });
}
function scrollToLine(t) {
  const i = segments.findIndex((s) => s.end >= t);
  const li = $(`#transcript li[data-i="${Math.max(i - 2, 0)}"]`);
  if (li) li.parentElement.scrollTop = li.offsetTop - li.parentElement.offsetTop;
}
$("#transcript").onclick = (e) => {
  const li = e.target.closest("li"); if (!li) return;
  const s = segments[li.dataset.i];
  if (e.target.dataset.set === "start") $("#start").value = fmt(s.start);
  else if (e.target.dataset.set === "end") $("#end").value = fmt(s.end + 1);
  else { $("#player").currentTime = s.start; $("#player").play(); }
  drawCut();
};
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
    $("#transcript li.now")?.classList.remove("now");
    $(`#transcript li[data-i="${i}"]`)?.classList.add("now");
    lastNow = i;
  }
};
$(".cuts").onclick = (e) => {
  const act = e.target.dataset.act, p = $("#player");
  if (!act) return;
  if (act === "setStart") $("#start").value = fmt(p.currentTime);
  if (act === "setEnd") $("#end").value = fmt(p.currentTime);
  if (act === "playStart") { p.currentTime = parse($("#start").value); p.play(); scrollToLine(p.currentTime); }
  if (act === "playEnd") { p.currentTime = parse($("#end").value) - 20; p.play(); scrollToLine(p.currentTime); }
  drawCut();
};
$("#redetect").onclick = async (e) => {
  e.preventDefault();
  if (!confirm("Find the sermon again?\n\nThis re-runs the AI analysis (several minutes on the CPU) and replaces the current start/end times, title, scripture and description, including any edits you made.")) return;
  await api(`/api/jobs/${job.id}/redetect`, { method: "POST" });
  job = await api(`/api/jobs/${job.id}`); job.cut = null; formLoadedFor = null;
  go(2);
};
$("#search").oninput = (e) => {
  const q = e.target.value.toLowerCase();
  $$("#transcript li").forEach((li) => li.classList.toggle("hide", q && !segments[li.dataset.i].text.toLowerCase().includes(q)));
};
$("#coverFile").onchange = async (ev) => {
  const fd = new FormData(); fd.append("file", ev.target.files[0]);
  try {
    await api(`/api/jobs/${job.id}/cover`, { method: "POST", body: fd });
    job.cover = true; $("#coverImg").src = `/api/jobs/${job.id}/cover.jpg?${Date.now()}`; renderReviewState();
  } catch (e) { alert(e.message); }
};
$("#rewrite").onclick = async () => {
  await api(`/api/jobs/${job.id}/describe`, { method: "POST", body: { start: parse($("#start").value), end: parse($("#end").value) } });
  job.describing = true; waitingDescribe = true; renderReviewState(); ensurePolling();
};

function cutBody() {
  const sel = $("#series");
  return {
    start: parse($("#start").value), end: parse($("#end").value), title: $("#title").value.trim(),
    speaker: $("#speaker").value.trim(), series: sel.value, series_name: sel.value ? sel.selectedOptions[0].text : "",
    scripture: $("#scripture").value.trim(), description: $("#description").value.trim(), date: $("#date").value,
    bitrate: $("#bitrate").value, normalize: $("#normalize").checked,
  };
}
$("#toPublish").onclick = async () => {
  const body = cutBody();
  const missing = [["title", "a title"], ["date", "a date"]].filter(([k]) => !body[k]).map(([, n]) => n);
  if (missing.length) return alert(`Please add ${missing.join(" and ")}.`);
  if (body.end - body.start < 60) return alert("The sermon needs to be at least a minute long. Check the start and end times.");
  try {
    await api(`/api/jobs/${job.id}/encode`, { method: "POST", body });
    job.encoding = true; job.cut = body; job.output = job.output || null;
    go(4);
  } catch (e) { alert(e.message); }
};

// ======================================================================== step 4: publish
function renderPublish() {
  const c = job.cut || {};
  $("#encoding").hidden = !job.encoding;
  if (job.encoding) {
    const f = job.encode_progress || 0;
    $("#encProgress").value = f;
    $("#encPct").textContent = `${Math.floor(f * 100)}%`;
    $("#encMsg").textContent = job.message && !/failed/i.test(job.message) ? job.message : "Making the MP3…";
    // estimate time left from the speed so far
    if (!encStart || f < encStart.f) encStart = { t: Date.now(), f };
    const rate = (f - encStart.f) / ((Date.now() - encStart.t) / 1000);
    $("#encEta").textContent = f >= 1 ? "Almost done…" : rate > 0 && f - encStart.f > 0.02
      ? `About ${Math.max(1, Math.round((1 - f) / rate / 60))} min left` : "Working out how long this will take…";
  } else encStart = null;
  $("#summary").hidden = !!job.encoding || !job.output;
  if (!job.encoding && job.output) {
    $("#sumCover").hidden = !job.cover;
    if (job.cover) $("#sumCover").src = `/api/jobs/${job.id}/cover.jpg?${job.output.size}`;
    $("#sumSeries").textContent = c.series_name || "No series";
    $("#sumTitle").textContent = c.title;
    $("#sumBy").textContent = [c.speaker, c.date && niceDate(c.date)].filter(Boolean).join(" · ");
    $("#sumScripture").textContent = c.scripture || "";
    $("#sumDesc").textContent = c.description || "(no description)";
    const url = `/api/jobs/${job.id}/output.mp3?v=${job.output.size}`;
    if (!$("#outPlayer").src.endsWith(url)) { $("#outPlayer").src = url; $("#download").href = url; }
    $("#download").download = job.output.name;
    $("#sumFile").textContent = `${job.output.name} · ${size(job.output.size)} · ${fmt(job.output.duration)}`;
    const a = job.archive || {};
    $("#sumNas").textContent = a.status === "saved" ? `NAS: ${a.folder} (${a.message})`
      : a.status === "saving" ? "NAS: copying…" : a.status === "error" ? a.message : "Not saved to the NAS (you can do that in step 2).";
  }
  if (/Encoding failed/.test(job.message || "")) { $("#encoding").hidden = true; $("#publishMsg").textContent = job.message; }
  const ready = !job.encoding && job.output && !job.publishing;
  $("#draft").disabled = $("#publish").disabled = !ready || !status.wp_configured;
  $("#download").hidden = !job.output || job.encoding;
  const p = job.published;
  $("#result").hidden = !p;
  if (p) {
    $("#result").innerHTML = `<h3>${p.status === "publish" ? "Published! 🎉" : "Saved as a draft"}</h3>
      <p><a target="_blank" href="${p.edit}">Open it in WordPress</a>${p.status === "publish" && p.link ? ` · <a target="_blank" href="${p.link}">View it on the website</a>` : ""}</p>
      <button id="another">Start the next sermon</button>`;
    $("#another").onclick = startOver;
  }
  $("#publishMsg").textContent = job.publishing ? job.message
    : /failed/i.test(job.message || "") ? job.message
    : !status.wp_configured ? "Add the WordPress login to .env to enable uploading." : "";
}
async function publish(st) {
  if (st === "publish" && !confirm("Publish this sermon on the church website now?")) return;
  if (job.published && !confirm("This sermon was already uploaded once. Upload it again as a new post?")) return;
  try {
    await api(`/api/jobs/${job.id}/publish`, { method: "POST", body: { status: st } });
    job.publishing = true; job.message = "Uploading to WordPress…"; renderPublish(); ensurePolling();
  } catch (e) { alert(e.message); }
}
$("#draft").onclick = () => publish("draft");
$("#publish").onclick = () => publish("publish");

// ======================================================================== start over
function startOver() {
  location.hash = "";
  location.reload();
}
$("#newSermon").onclick = () => {
  if (uploading && !confirm("A file is still uploading. Start over anyway? The upload will be cancelled.")) return;
  if (job && (job.encoding || job.publishing) && !confirm("This sermon is still being processed. It will keep going in the background. Start a new one?")) return;
  if (job && !confirm("Start a new sermon?\n\nThis one is saved. You can get back to it from “Continue a sermon you already started” in step 1.")) return;
  startOver();
};

// ======================================================================== boot
(async () => {
  status = await api("/api/status");
  api("/api/wp/meta").then((m) => wpMeta = m).catch(() => {});  // warm up while the user uploads
  $("#engine").textContent = `AI: ${status.ai.model} on ${status.ai.device.toUpperCase()}` + (status.llm ? ` + ${status.llm}` : "");
  setupDrop($("#dropAudio"), "audio");
  setupDrop($("#dropImage"), "image");
  loadRecent();
  const m = location.hash.match(/^#([0-9a-f]{12})(?:\/(\d))?/);
  if (m) openJob(m[1], m[2] ? +m[2] : undefined).catch(() => go(1));
  else go(1);
})();
