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
  if (n === 2) { renderAnalyze(); }
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
    || job.archive?.status === "saving");  // (NAS copies show in step 4)
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
    await askNas(j.id);
    await openJob(j.id, 2);
  } catch (e) { alert(e.message); }
  $("#toAnalyze").disabled = false;
};

// ---- "Save to the NAS?" dialog (shown when leaving step 1)
let nasJob = null;
async function askNas(jid, folder = "") {
  let plan;
  try { plan = await api(`/api/jobs/${jid}/archive${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`); }
  catch { return; }  // no archive folder configured: nothing to ask
  nasJob = jid;
  $("#nasFolder").textContent = plan.folder;
  $("#nasFolderTag").textContent = plan.exists ? "existing folder" : "new folder";
  $("#nasFolderTag").className = "tag" + (plan.exists ? "" : " new");
  $("#nasFiles").innerHTML = plan.files.map((f) => `<li>
      <span class="what"><b>${esc(f.label)}</b><code>${esc(f.name)}</code>
        <span class="hint">${f.present ? " · already in this folder" : ""}${f.note ? ` · ${esc(f.note)}` : ""}</span></span>
      <label class="switch"><input type="checkbox" data-key="${f.key}" ${f.default ? "checked" : ""}><span></span></label></li>`).join("");
  $("#nasPicker").hidden = true;
  const dlg = $("#nasDialog");
  if (dlg.open) return;
  dlg.showModal();
  await new Promise((resolve) => { dlg.onclose = resolve; });
}
async function nasSubmit(skip) {
  const body = { folder: $("#nasFolder").textContent, skip };
  $$("#nasFiles input").forEach((i) => body[i.dataset.key] = i.checked);
  try { await api(`/api/jobs/${nasJob}/archive`, { method: "POST", body }); $("#nasDialog").close(); }
  catch (e) { alert(e.message); }
}
$("#nasOk").onclick = () => nasSubmit(false);
$("#nasSkip").onclick = () => nasSubmit(true);
$("#nasDialog").oncancel = (e) => { e.preventDefault(); };  // Esc: make an explicit choice
$("#nasChange").onclick = () => {
  const box = $("#nasPicker"); box.hidden = !box.hidden;
  if (!box.hidden) browse(box, $("#nasFolder").textContent.split("/").slice(0, -1).join("/"), "dir", () => {})
    .catch(() => browse(box, status.archive_root, "dir", () => {}));
};
$("#nasUse").onclick = () => {
  const p = $("#nasPicker").dataset.path;
  if (!p) return;
  askNas(nasJob, p);  // re-check which files are already in that folder
};

async function loadRecent() {
  const list = $("#recent"); list.innerHTML = "";
  const jobsList = await api("/api/jobs");
  $("#recentBox").hidden = !jobsList.length;
  for (const j of jobsList) {
    const li = item(`${niceDate(j.date)} · ${j.name}`,
      j.published ? (j.published.status === "publish" ? "published" : "draft in WordPress") : j.status === "ready" ? "ready to review" : j.status,
      () => openJob(j.id));
    const del = document.createElement("button");
    del.className = "secondary small del"; del.textContent = "🗑 Delete"; del.title = "Delete this sermon from Sermon Studio";
    del.onclick = (e) => { e.stopPropagation(); deleteJob(j, li); };
    li.append(del);
    list.append(li);
  }
}

async function deleteJob(j, li) {
  if (!confirm(`Delete “${j.name}” (${niceDate(j.date)}) from Sermon Studio?\n\n`
    + "Removes its analysis, transcript, previews, the MP3 made here and the files you uploaded for it.\n"
    + "Doesn't touch anything saved on the NAS or posted to WordPress.")) return;
  try {
    await api(`/api/jobs/${j.id}`, { method: "DELETE" });
    li.remove();
    if (!$("#recent").children.length) $("#recentBox").hidden = true;
    if (job?.id === j.id) { job = null; history.replaceState(null, "", "#"); }
  } catch (e) { alert(e.message); }
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

function renderAnalyze() {
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
  const nb = $("#notifyMe"), done = ["ready", "error", "cancelled"].includes(job.status);
  nb.checked = !!(job.notify || job.auto);
  nb.disabled = !status.discord || !!job.auto || done;
  $("#notifyHint").textContent = !status.discord ? "(set up the Discord bot in Settings first)"
    : job.auto ? "(always on for recordings found on the NAS)" : done && job.notify ? "(sent)" : "";
  nb.closest("label").hidden = done && !job.notify && !job.auto;
  const running = !["ready", "error", "cancelled"].includes(job.status);
  $("#stopAnalysis").hidden = !running;
  $("#restartAnalysis").hidden = !["error", "cancelled"].includes(job.status);
}
$("#toReview").onclick = () => go(3);
$("#notifyMe").onchange = async (e) => {
  try { await api(`/api/jobs/${job.id}/notify`, { method: "POST", body: { on: e.target.checked } }); job.notify = e.target.checked; }
  catch (err) { alert(err.message); e.target.checked = !e.target.checked; }
};
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
      : a.status === "saving" ? "NAS: copying…" : a.status === "error" ? a.message : "Not saved to the NAS.";
  }
  if (/Encoding failed/.test(job.message || "")) { $("#encoding").hidden = true; $("#publishMsg").textContent = job.message; }
  const ready = !job.encoding && job.output && !job.publishing;
  const p0 = job.published, isDraft = p0?.status === "draft", isLive = p0?.status === "publish";
  $("#draft").textContent = isDraft ? "Update the draft" : "Upload as draft";
  $("#publish").textContent = isDraft ? "Publish the draft" : "Publish now";
  $("#draft").hidden = $("#publish").hidden = isLive;
  $("#draft").disabled = $("#publish").disabled = !ready || !status.wp_configured;
  $("#download").hidden = !job.output || job.encoding;
  const p = job.published;
  $("#result").hidden = !p;
  if (p) {
    $("#result").innerHTML = `<h3>${p.status === "publish" ? "Published! 🎉" : "Saved as a draft"}</h3>
      ${p.status === "draft" ? "<p class=hint>Check it in WordPress, then use <b>Publish the draft</b> below. It publishes this same post without uploading again.</p>" : ""}
      <p><a target="_blank" href="${p.edit}">Open it in WordPress</a>${p.status === "publish" && p.link ? ` · <a target="_blank" href="${p.link}">View it on the website</a>` : ""}</p>
      <button id="another">Start the next sermon</button>`;
    $("#another").onclick = startOver;
  }
  $("#publishMsg").textContent = job.publishing ? job.message
    : /failed/i.test(job.message || "") ? job.message
    : !status.wp_configured ? "Set up WordPress under ⚙ Settings to enable uploading." : "";
}
async function publish(st) {
  const isDraft = job.published?.status === "draft";
  if (st === "publish" && !confirm(isDraft ? "Publish the draft on the church website now?" : "Publish this sermon on the church website now?")) return;
  if (st === "draft" && isDraft && !confirm("Update the existing draft with the current MP3 and details?")) return;
  try {
    await api(`/api/jobs/${job.id}/publish`, { method: "POST", body: { status: st } });
    job.publishing = true; job.message = isDraft ? "Updating WordPress…" : "Uploading to WordPress…"; renderPublish(); ensurePolling();
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

// ======================================================================== settings + first-run setup
let settingsPoll = null, cfg = null, setupStep = null;
const SETUP = ["cardWelcome", "cardStorage", "cardWordpress", "cardDiscord", "cardMonitor", "cardDone"];
const ago = (t) => {
  if (!t) return "never";
  const s = Math.round(Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function showTab(tab) {
  $("#settingsView").hidden = tab !== "settings";
  $("#wizardView").hidden = tab === "settings";
  $$("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  clearInterval(settingsPoll); settingsPoll = null;
  if (tab === "settings") { loadSettings(true); settingsPoll = setInterval(() => loadSettings(false), 5000); }
}
$$("#tabs a").forEach((a) => a.onclick = (e) => { e.preventDefault(); showTab(a.dataset.tab); });

async function loadSettings(fillForms) {
  const [s, c] = await Promise.all([api("/api/settings"), api("/api/config")]);
  cfg = c;
  renderSettings(s);
  renderConfig(fillForms);
}

// ---- monitoring + about
function renderSettings(s) {
  const w = s.watch;
  $("#watchToggle").checked = w.enabled;
  $("#watchWhat").textContent = `Checks ${w.folder} every ${Math.round(w.interval / 60)} min for new ${w.types.join(", ")} files.`;
  $("#scanNow").disabled = !w.enabled;
  const parts = [w.enabled ? "<b class=ok>On</b>" : "<b class=bad>Off</b>", `Last checked: ${ago(w.last_scan)}`, `${w.seen} recordings already known`];
  if (w.pending) parts.push(`${w.pending} new file(s) still copying`);
  if (w.last_found) parts.push(`Last new recording: ${esc(w.last_found.path.split("/").pop())} (${ago(w.last_found.at)})`);
  if (w.last_error) parts.push(`<span class=bad>Last check failed: ${esc(w.last_error)}</span>`);
  $("#watchStatus").innerHTML = parts.join(" · ");
  const ai = s.ai;
  $("#aboutList").innerHTML = [["AI", `${esc(ai.model)} on ${ai.device.toUpperCase()}${ai.llm ? ` + ${esc(ai.llm)}` : " (no LLM)"}`],
    ["Source", `<a href="https://github.com/admawby/sermon-studio" target="_blank" rel="noopener">github.com/admawby/sermon-studio</a>`]]
    .map(([k, v]) => `<li><span class="k">${k}</span><span>${v}</span></li>`).join("");
}

// ---- storage / WordPress / Discord forms
function renderConfig(fillForms) {
  const v = cfg.values;
  $("#rootList").innerHTML = cfg.roots.map((r) => `<li><span class="k">${esc(r.name)}</span><span>
    ${!r.exists ? "<span class=bad>✗ missing</span>" : r.writable ? "<span class=ok>✓ read &amp; write</span>" : "<span class=bad>read-only</span>"}
    · <code>${esc(r.path)}</code>${r.exists && !r.items && r.name !== "Uploads" ? " · <span class=bad>empty: is the host folder mounted?</span>" : ""}</span></li>`).join("");
  $("#cfgArchive").textContent = v.ARCHIVE_ROOT || "(not set)";
  $("#cfgArchive").className = cfg.archive_ok ? "" : "bad";
  const d = cfg.discord;
  $("#discordState").innerHTML = !d.configured ? "<span class=hint>Not set up.</span>"
    : d.connected ? `<span class=ok>● Connected</span> as ${esc(d.name)}`
    : d.error ? `<span class=bad>● ${esc(d.error)}</span>` : "<span class=hint>● Connecting…</span>";
  if (!fillForms) return;
  $("#cfgPrefix").value = v.FILE_PREFIX; $("#prefixEx").textContent = v.FILE_PREFIX || "Sermon_";
  $("#cfgWpUrl").value = v.WP_URL; $("#cfgWpUser").value = v.WP_USER;
  $("#cfgWpPass").value = ""; $("#cfgWpPass").placeholder = v.WP_PASSWORD.set ? "•••••••• saved (leave blank to keep)" : "";
  $("#cfgDcToken").value = ""; $("#cfgDcToken").placeholder = v.DISCORD_TOKEN.set ? "•••••••• saved (leave blank to keep)" : "Paste the bot token";
  $("#cfgDcUser").value = v.DISCORD_USER_ID || ""; $("#cfgDcChannel").value = v.DISCORD_CHANNEL_ID || "";
  $("#cfgPublicUrl").value = v.PUBLIC_URL || "";
}
$("#cfgPrefix").oninput = (e) => $("#prefixEx").textContent = e.target.value || "Sermon_";
$("#useThisUrl").onclick = () => $("#cfgPublicUrl").value = location.origin;

function cardMsg(card, text, ok) {
  const m = $(`#${card} .msg`);
  m.textContent = text; m.className = "hint msg" + (ok === true ? " ok" : ok === false ? " bad" : "");
}
async function saveValues(card, values) {
  cardMsg(card, "Saving…");
  try {
    cfg = await api("/api/config", { method: "POST", body: { values } });
    renderConfig(false);
    cardMsg(card, "Saved.", true);
    return true;
  } catch (e) { cardMsg(card, e.message, false); return false; }
}
const saveStorage = () => saveValues("cardStorage", { ARCHIVE_ROOT: $("#cfgArchive").textContent.replace("(not set)", ""), FILE_PREFIX: $("#cfgPrefix").value });
const saveWordpress = () => saveValues("cardWordpress", { WP_URL: $("#cfgWpUrl").value, WP_USER: $("#cfgWpUser").value, WP_PASSWORD: $("#cfgWpPass").value });
const saveDiscord = () => saveValues("cardDiscord", { DISCORD_TOKEN: $("#cfgDcToken").value, DISCORD_USER_ID: $("#cfgDcUser").value,
  DISCORD_CHANNEL_ID: $("#cfgDcChannel").value, PUBLIC_URL: $("#cfgPublicUrl").value });
$("#saveStorage").onclick = saveStorage;
$("#saveWordpress").onclick = saveWordpress;
$("#saveDiscord").onclick = saveDiscord;

async function runTest(card, url, save) {
  if (!(await save())) return false;
  cardMsg(card, card === "cardDiscord" ? "Connecting to Discord and sending a test message…" : "Logging in…");
  try { const r = await api(url, { method: "POST" }); cardMsg(card, r.message, true); return true; }
  catch (e) { cardMsg(card, e.message, false); return false; }
  finally { cfg = await api("/api/config"); renderConfig(false); }
}
$("#testWordpress").onclick = () => runTest("cardWordpress", "/api/config/test/wordpress", saveWordpress);
$("#testDiscord").onclick = () => runTest("cardDiscord", "/api/config/test/discord", saveDiscord);

$("#pickArchive").onclick = () => {
  const box = $("#archivePicker"); box.hidden = !box.hidden;
  const start = cfg.values.ARCHIVE_ROOT || "";
  if (!box.hidden) browse(box, start, "dir", () => {}).catch(() => browse(box, "", "dir", () => {}));
};
$("#useArchive").onclick = () => {
  const p = $("#archivePicker").dataset.path;
  if (!p) return alert("Open one of the folders first.");
  $("#cfgArchive").textContent = p; $("#cfgArchive").className = "";
  $("#archivePicker").hidden = true;
  if (setupStep === null) saveStorage();
};

// ---- monitoring switch
$("#watchToggle").onchange = async (e) => {
  const on = e.target.checked;
  let catchUp = false;
  if (on && setupStep === null) {
    catchUp = confirm("Turn on NAS monitoring.\n\nWere any recordings added while it was off that you want analyzed too?\n\nOK = yes, analyze those as well\nCancel = no, only watch for recordings added from now on");
  } else if (!on && setupStep === null && !confirm("Turn off NAS monitoring? New recordings won't be analyzed or sent to Discord until you turn it back on.")) {
    e.target.checked = true; return;
  }
  e.target.disabled = true;
  try { renderSettings(await api("/api/settings/watch", { method: "POST", body: { enabled: on, catch_up: catchUp } })); }
  catch (err) { alert(err.message); e.target.checked = !on; }
  e.target.disabled = false;
};
$("#scanNow").onclick = async () => {
  await api("/api/settings/watch/scan", { method: "POST" }).catch((e) => alert(e.message));
  $("#scanMsg").textContent = "Checking…";
  setTimeout(() => { $("#scanMsg").textContent = ""; loadSettings(false); }, 4000);
};

// ---- first-run setup wizard: the same cards, one at a time
async function enterSetup() {
  document.body.classList.add("setupMode");
  $("#settingsView").classList.add("setup");
  $("#settingsView").hidden = false; $("#wizardView").hidden = true;
  $("#settingsTitle").textContent = "Set up Sermon Studio";
  $("#setupHead").hidden = false; $("#setupNav").hidden = false;
  setupStep = 0;
  await loadSettings(true);
  if (!$("#cfgPublicUrl").value) $("#cfgPublicUrl").value = location.origin;
  renderSetup();
}
function renderSetup() {
  SETUP.forEach((id, i) => $("#" + id).classList.toggle("current", i === setupStep));
  $("#setupDots").innerHTML = SETUP.map((id, i) =>
    `<li class="${i === setupStep ? "current" : i < setupStep ? "done" : ""}">${i + 1}. ${$("#" + id).dataset.title}</li>`).join("");
  const id = SETUP[setupStep];
  $("#setupBack").style.visibility = setupStep ? "visible" : "hidden";
  $("#setupSkip").hidden = !["cardWordpress", "cardDiscord", "cardMonitor"].includes(id);
  $("#setupNext").textContent = id === "cardWelcome" ? "Let's start →" : id === "cardDone" ? "Finish" : "Save & continue →";
  if (id === "cardMonitor") $("#watchToggle").disabled = !cfg.archive_ok;
  if (id === "cardDone") renderDone();
  window.scrollTo({ top: 0 });
}
function renderDone() {
  const v = cfg.values, d = cfg.discord;
  $("#doneList").innerHTML = [
    ["Recordings", cfg.archive_ok ? `<code>${esc(v.ARCHIVE_ROOT)}</code>` : "<span class=bad>not set</span>"],
    ["WordPress", cfg.wordpress.configured ? `<span class=ok>✓</span> ${esc(v.WP_URL)}` : "<span class=hint>not set up (needed to publish)</span>"],
    ["Discord", d.connected ? `<span class=ok>✓</span> ${esc(d.name)}` : d.configured ? "<span class=bad>set up but not connected</span>" : "<span class=hint>skipped</span>"],
    ["Monitoring", $("#watchToggle").checked ? "<span class=ok>on</span>" : "<span class=hint>off</span>"],
  ].map(([k, x]) => `<li><span class="k">${k}</span><span>${x}</span></li>`).join("");
}
$("#setupBack").onclick = () => { setupStep--; renderSetup(); };
$("#setupSkip").onclick = () => { setupStep++; renderSetup(); };
$("#setupNext").onclick = async () => {
  const id = SETUP[setupStep];
  if (id === "cardStorage") {
    if (!(await saveStorage())) return;
    if (!cfg.archive_ok) return cardMsg("cardStorage", "Choose the folder your recordings are saved in.", false);
  }
  if (id === "cardWordpress" && ($("#cfgWpUrl").value || $("#cfgWpUser").value || $("#cfgWpPass").value)) {
    if (!(await runTest("cardWordpress", "/api/config/test/wordpress", saveWordpress))
        && !confirm("The WordPress login didn't work. Continue anyway and fix it later in Settings?")) return;
  }
  if (id === "cardDiscord" && ($("#cfgDcToken").value || $("#cfgDcUser").value)) {
    if (!(await runTest("cardDiscord", "/api/config/test/discord", saveDiscord))
        && !confirm("The Discord bot isn't working yet. Continue anyway and fix it later in Settings?")) return;
  }
  if (id === "cardDone") {
    await api("/api/setup/complete", { method: "POST" });
    location.hash = ""; location.reload();
    return;
  }
  setupStep++;
  if (SETUP[setupStep] === "cardDone") cfg = await api("/api/config");
  renderSetup();
};
$("#rerunSetup").onclick = () => enterSetup();

// ======================================================================== boot
(async () => {
  status = await api("/api/status");
  api("/api/wp/meta").then((m) => wpMeta = m).catch(() => {});  // warm up while the user uploads
  $("#engine").textContent = `AI: ${status.ai.model} on ${status.ai.device.toUpperCase()}` + (status.llm ? ` + ${status.llm}` : "");
  setupDrop($("#dropAudio"), "audio");
  setupDrop($("#dropImage"), "image");
  loadRecent();
  if (!status.setup_complete) { enterSetup(); return; }
  const m = location.hash.match(/^#([0-9a-f]{12})(?:\/(\d))?/);
  if (m) openJob(m[1], m[2] ? +m[2] : undefined).catch(() => go(1));
  else go(1);
})();
