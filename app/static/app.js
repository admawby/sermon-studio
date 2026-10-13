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
  if (n === 1) { refreshActive(); }
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

// ---- "already working on…" notices on step 1
function stageText(j) {
  return j.status === "transcribing" ? `transcribing ${Math.round((j.progress || 0) * 100)}%`
    : { queued: "waiting to start", preparing: "converting the audio", detecting: "finding the sermon",
        describing: "writing the title and description" }[j.status] || j.status;
}
async function refreshActive() {
  if (step !== 1 || $("#wizardView").hidden) return;
  let a;
  try { a = await api("/api/active"); } catch { return; }
  const box = $("#activeBanner");
  box.innerHTML = [
    ...a.working.map((j) => `<div class="notice"><span>⏳</span><span class="txt">
      <b>${j.batch ? "Batch processing is working on" : "Already working on"} the service from ${niceDate(j.date)}</b>${j.auto ? " (found on the NAS automatically)" : ""}: ${esc(stageText(j))}.
      ${j.batch ? "See the ⚡ Batch tab." : "No need to start it yourself."}</span>
      ${j.status === "transcribing" ? `<progress max="1" value="${j.progress || 0}"></progress>` : ""}
      <button class="secondary small" data-open="${j.id}" data-step="2">View progress</button></div>`),
    ...a.waiting.map((j) => `<div class="notice ready"><span>✅</span><span class="txt">
      <b>The sermon from ${niceDate(j.date)} is ready for review.</b> It was found on the NAS and analyzed automatically.</span>
      <button class="small" data-open="${j.id}" data-step="3">Review it</button></div>`),
  ].join("");
  box.hidden = !box.innerHTML;
  $$("#activeBanner [data-open]").forEach((b) => b.onclick = () => openJob(b.dataset.open, +b.dataset.step));
}
setInterval(refreshActive, 8000);

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
  $("#reason").textContent = (prop.source || "").startsWith("voice") ? `🎙 Found by the preacher's voice${prop.source.includes("AI") ? " and pinpointed by the AI" : ""}: ${prop.reason}`
    : prop.source === "ai" ? `The AI's pick: ${prop.reason}` : `Best guess (${prop.source}): ${prop.reason}`;
  $("#title").value = cut.title || sug.title || "";
  $("#scripture").value = cut.scripture || sug.scripture || "";
  $("#description").value = cut.description || sug.description || "";
  $("#date").value = cut.date || job.date;
  $("#speaker").value = cut.speaker || sug.speaker || "";
  const d = sug.speaker_detail || {};
  $("#speakerHint").textContent = cut.speaker && cut.speaker !== sug.speaker ? ""
    : sug.speaker_source === "voice" ? `🎙 Recognized by voice (match ${d.score}).`
    : sug.speaker_source === "transcript" ? `📝 From the transcript${d.evidence ? `: “${d.evidence}”` : ""}.`
    : "⚠ Couldn't recognize the voice, so this is last week's speaker. Please check it.";
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
  if (act === "playEnd") { p.currentTime = parse($("#end").value) - 10; p.play(); scrollToLine(p.currentTime); }
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
  $("#batchView").hidden = tab !== "batch";
  $("#wizardView").hidden = tab !== "wizard";
  $$("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  clearInterval(settingsPoll); settingsPoll = null;
  if (tab === "settings") { loadSettings(true); settingsPoll = setInterval(() => loadSettings(false), 5000); }
  if (tab === "batch") batchOpen(); else batchStopPolling();
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
  const act = s.active || [];
  $("#watchActive").hidden = !act.length;
  $("#watchActive").innerHTML = act.map((j) => `<div>⏳ <b>Working on ${esc(j.name)}</b> (service from ${niceDate(j.date)})
    · ${esc(stageText(j))} · <a href="#${j.id}/2" class="openJob" data-id="${j.id}">open</a></div>`).join("");
  $$("#watchActive .openJob").forEach((a) => a.onclick = (e) => { e.preventDefault(); showTab("wizard"); openJob(a.dataset.id, 2); });
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
  $("#cfgPublicUrl2").value = v.PUBLIC_URL_REMOTE || "";
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
  DISCORD_CHANNEL_ID: $("#cfgDcChannel").value, PUBLIC_URL: $("#cfgPublicUrl").value,
  PUBLIC_URL_REMOTE: $("#cfgPublicUrl2").value });
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


// ======================================================================== batch
const B = { recs: [], sel: new Set(), state: null, step: 1, poll: null, open: new Set(), gpu: false, opts: {}, meta: null, tx: {} };
const dur = (s) => s ? `${Math.round(s / 60)} min` : "";
const short = (d) => new Date(d + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
const FLAG = { no_post: ["warn", "No WordPress post"], unknown_voice: ["warn", "Voice not recognized"], speaker_differs: ["bad", "Speaker differs"],
  hand_not_found: ["warn", "Couldn't find your MP3 in the recording"], edges_differ: ["warn", "Start/end differs from yours"] };
const sgn = (x) => (x >= 0 ? "+" : "−") + Math.abs(Math.round(x)) + " s";

async function batchOpen() {
  B.state = await api("/api/batch");
  if (!B.meta) B.meta = wpMeta || await api("/api/wp/meta").catch(() => ({ series: [], speakers: [] }));
  if (!$("#speakers").children.length) for (const p of B.meta.speakers || []) $("#speakers").append(new Option(p));
  const st = B.state.status;
  if (st === "idle" || !B.state.items.length) { await batchLoadRecs(false); batchGo(1); }
  else batchGo(B.state.apply ? 5 : st === "done" ? 4 : 3);
  batchPolling();
}
function batchPolling() {
  batchStopPolling();
  B.poll = setInterval(async () => {
    B.state = await api("/api/batch");
    if (B.step === 3) renderQueue();
    if (B.step === 4) renderReview(false);
    if (B.step === 5) renderApply();
    renderChips();
  }, 4000);
}
function batchStopPolling() { clearInterval(B.poll); B.poll = null; }

function bReach(n) {
  const s = B.state || { status: "idle", items: [] }, busy = s.items.length > 0;
  if (n === 1 || n === 2) return !busy;  // a finished batch is cleared with "Start a new batch"
  if (n === 3) return busy;
  if (n === 4) return s.items.some((x) => x.state === "done" || x.state === "error");
  if (n === 5) return s.items.some((x) => x.approved && x.state === "done");
}
function renderChips() {
  const names = ["Choose recordings", "Options", "Processing", "Review results", "Apply"];
  $("#bSteps").innerHTML = names.map((t, i) => {
    const n = i + 1, cur = n === B.step, done = n < B.step || (n <= 2 && (B.state?.items.length || 0) > 0);
    return `<li data-n="${n}" class="${cur ? "cur" : done ? "done" : ""} ${!cur && bReach(n) ? "reach" : ""}">${done && !cur ? "✓ " : n + ". "}${t}</li>`;
  }).join("");
  $$("#bSteps li.reach").forEach((li) => li.onclick = () => batchGo(+li.dataset.n));
}
function batchGo(n) {
  B.step = n;
  $$("#batchView .bstep").forEach((s) => s.hidden = +s.dataset.b !== n);
  renderChips();
  if (n === 1) renderRecs();
  if (n === 2) renderOptions();
  if (n === 3) renderQueue();
  if (n === 4) renderReview(true);
  if (n === 5) loadPlan();
  window.scrollTo({ top: 0 });
}
$$("[data-bgo]").forEach((b) => b.onclick = () => batchGo(+b.dataset.bgo));

// ---- step 1
async function batchLoadRecs(refresh) {
  $("#bSummary").innerHTML = `<div class="hint">${refresh ? "Fetching the post list from WordPress…" : "Looking through the recordings…"}</div>`;
  try {
    const r = await api(`/api/batch/recordings${refresh ? "?refresh=true" : ""}`);
    B.recs = r.recordings; B.gpu = r.gpu; B.opts = r.options; B.wp = r.wp;
    B.sel = new Set(B.recs.filter((x) => !x.job && !x.unusable).map((x) => x.path));
    if (B.recs.length) {
      $("#bTo").value = B.recs[0].date.slice(0, 7);
      $("#bFrom").value = B.recs[B.recs.length - 1].date.slice(0, 7);
    }
  } catch (e) { $("#bSummary").innerHTML = `<div class="bad">${esc(e.message)}</div>`; B.recs = []; }
  renderRecs();
}
function shownRecs() {
  const from = $("#bFrom").value, to = $("#bTo").value, f = $("#bFilter").value, q = $("#bSearch").value.toLowerCase();
  return B.recs.filter((r) => (!from || r.date.slice(0, 7) >= from) && (!to || r.date.slice(0, 7) <= to)
    && (f !== "post" || r.post) && (f !== "new" || !r.job) && (f !== "nomp3" || !r.mp3)
    && (!q || `${r.post?.title || ""} ${r.post?.speaker || ""} ${r.name}`.toLowerCase().includes(q)));
}
function renderRecs() {
  const rows = shownRecs(), tb = $("#bRecs tbody");
  tb.innerHTML = rows.map((r) => r.unusable ? `<tr class="dim" title="This recording can't be processed">
    <td><input type="checkbox" disabled></td><td><b>${short(r.date)}</b></td><td><code>${esc(r.name)}</code></td>
    <td colspan="2"><span class="pill bad">${esc(r.unusable)}</span> <span class="note">The recorder probably failed that day.</span></td><td></td></tr>`
    : `<tr class="${B.sel.has(r.path) ? "sel" : ""}" data-p="${esc(r.path)}">
    <td><input type="checkbox" ${B.sel.has(r.path) ? "checked" : ""}></td><td><b>${short(r.date)}</b></td>
    <td><code>${esc(r.name)}</code><div class="note">${dur(r.duration)}</div></td>
    <td>${r.mp3 ? `✓ <span class="note">${esc(r.mp3)}</span>` : "—"}</td>
    <td>${r.post ? `${esc(r.post.title)} <span class="note">· ${esc(r.post.speaker || "no speaker")}</span>` : `<span class="pill warn">no post</span>`}</td>
    <td>${r.job ? `<span class="pill ok">done before</span>` : `<span class="pill">not processed</span>`}</td></tr>`).join("")
    || `<tr><td colspan="6" class="note" style="text-align:center">No recordings match.</td></tr>`;
  $$("#bRecs tbody tr[data-p]").forEach((tr) => tr.onclick = (e) => {
    const p = tr.dataset.p; B.sel.has(p) ? B.sel.delete(p) : B.sel.add(p);
    tr.classList.toggle("sel", B.sel.has(p)); $("input", tr).checked = B.sel.has(p); renderRecSummary();
  });
  renderRecSummary();
}
function renderRecSummary() {
  const sel = B.recs.filter((r) => B.sel.has(r.path)), n = sel.length;
  const mins = sel.reduce((a, r) => a + (r.duration || 4000) / 60, 0);
  const gpuH = (mins * 0.03 + n * 1.5) / 60, cpuH = (mins * 0.25 + n * 1.5) / 60;
  const fmtH = (h) => h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : `${h.toFixed(h < 10 ? 1 : 0)} h`;
  $("#bSummary").innerHTML = `<div><b>${n}</b>selected of ${B.recs.length}</div>
    <div><b>~${fmtH(B.gpu ? gpuH : cpuH)}</b>estimated on the ${B.gpu ? "GPU" : "CPU"}${B.gpu ? "" : `<br><span class="note">(~${fmtH(gpuH)} once the GPU is in)</span>`}</div>
    <div><b>${(sel.reduce((a, r) => a + r.size, 0) / 1e9).toFixed(0)} GB</b>of recordings to read</div>
    <div><b>${sel.filter((r) => r.post).length}</b>matched to a WordPress post</div>
    <div><b>${sel.filter((r) => !r.post).length}</b>without a post</div>
    ${B.recs.some((r) => r.unusable) ? `<div><b class="bad">${B.recs.filter((r) => r.unusable).length}</b>can't be used (empty or damaged)</div>` : ""}`;
  $("#bToOptions").disabled = !n;
  B.est = B.gpu ? gpuH : cpuH;
}
["#bFrom", "#bTo", "#bFilter"].forEach((s) => $(s).onchange = renderRecs);
$("#bSearch").oninput = renderRecs;
$("#bAll").onclick = () => { shownRecs().filter((r) => !r.unusable).forEach((r) => B.sel.add(r.path)); renderRecs(); };
$("#bNone").onclick = () => { B.sel.clear(); renderRecs(); };
$("#bRefresh").onclick = () => batchLoadRecs(true);
$("#bToOptions").onclick = () => batchGo(2);

// ---- step 2
function renderOptions() {
  const o = B.opts;
  $$("[data-o]").forEach((i) => i.checked = !!o[i.dataset.o]);
  ["cut", "existing", "order"].forEach((k) => $$(`[name=o_${k}]`).forEach((r) => r.checked = r.value === o[k]));
  $$("[data-o^=wp_]").forEach((i) => { i.disabled = !B.wp; });
  $("#oMp3Hint").textContent = `Named like your other sermon MP3s (prefix + YYYYMMDD.mp3), ${status.bitrate} mono, volume-levelled, with title, speaker, series and cover art.`;
  $("#bCpuWarn").hidden = B.gpu;
  const n = B.sel.size;
  $("#bStartHint").textContent = `${n} recording${n === 1 ? "" : "s"} · about ${B.est < 1 ? Math.round(B.est * 60) + " min" : B.est.toFixed(1) + " h"} on the ${B.gpu ? "GPU" : "CPU"}`;
}
function readOptions() {
  const o = { ...B.opts };
  $$("[data-o]").forEach((i) => o[i.dataset.o] = i.checked);
  ["cut", "existing", "order"].forEach((k) => { const r = $(`[name=o_${k}]:checked`); if (r) o[k] = r.value; });
  return o;
}
$("#bStart").onclick = async () => {
  const recs = B.recs.filter((r) => B.sel.has(r.path));
  if (!B.gpu && recs.length > 5 && !confirm(`No GPU yet: ${recs.length} recordings will take about ${B.est.toFixed(0)} hours on the CPU. Start anyway?`)) return;
  const done = recs.filter((r) => r.job).length;
  if (done && !confirm(`${done} of these were processed before; their results in Sermon Studio will be replaced by the batch's. Continue?`)) return;
  try {
    B.opts = readOptions();
    B.state = await api("/api/batch/start", { method: "POST", body: { recordings: recs, options: B.opts } });
    batchGo(3);
  } catch (e) { alert(e.message); }
};

// ---- step 3
const STAGE = { queued: "Waiting", preparing: "Converting the audio", transcribing: "Transcribing", detecting: "Finding the sermon",
  describing: "Writing title & description", ready: "Finishing" };
function renderQueue() {
  const s = B.state, items = s.items, done = items.filter((x) => x.state === "done").length, err = items.filter((x) => x.state === "error").length;
  const cur = s.current != null ? items[s.current] : null;
  $("#bNow").textContent = s.status === "done" ? "Finished" : s.status === "paused" ? "Paused" : s.status === "stopping" ? "Stopping…"
    : cur ? `Processing ${s.current + 1} of ${items.length} · ${short(cur.date)}` : "Starting…";
  $("#bCounts").textContent = `${done} done · ${err} failed · ${items.filter((x) => x.state === "queued").length} waiting`;
  const frac = (done + err + (cur && cur.job_status === "transcribing" ? (cur.progress || 0) * 0.7 : 0)) / Math.max(1, items.length);
  $("#bProg").value = frac;
  const times = items.filter((x) => x.state === "done" && x.started && x.finished).map((x) => x.finished - x.started);
  if (s.status === "done") $("#bEta").textContent = `Done ${new Date(s.finished * 1000).toLocaleString()}.`;
  else if (times.length) {
    const left = (times.reduce((a, b) => a + b, 0) / times.length) * items.filter((x) => ["queued", "running"].includes(x.state)).length;
    const end = new Date(Date.now() + left * 1000);
    $("#bEta").textContent = `Started ${new Date(s.started * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · about ${left > 3600 ? (left / 3600).toFixed(1) + " h" : Math.round(left / 60) + " min"} left · finishes around ${end.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`;
  } else $("#bEta").textContent = "Working out how long this will take…";
  $("#bPause").textContent = s.status === "paused" ? "▶ Resume" : "⏸ Pause";
  $("#bPause").hidden = $("#bStop").hidden = !["running", "paused"].includes(s.status);
  $("#bNew").hidden = s.status !== "done";
  $("#bToReview").hidden = !items.some((x) => x.state === "done");
  $("#bQueue tbody").innerHTML = items.map((x) => {
    const st = x.state === "done" ? `<span class="pill ok">✓ done</span>` : x.state === "error" ? `<span class="pill bad">failed</span> <span class="note">${esc(x.error || "")}</span>`
      : x.state === "running" ? `<span class="pill blue">⏳ ${STAGE[x.job_status] || "Starting"}</span>${x.job_status === "transcribing" ? ` <span class="bar"><i style="width:${Math.round((x.progress || 0) * 100)}%"></i></span> ${Math.round((x.progress || 0) * 100)}%` : ""}`
      : `<span class="pill">${x.state === "skipped" ? "skipped" : "waiting"}</span>`;
    const res = x.state === "done" && x.cut ? `${fmt(x.cut.start)} → ${fmt(x.cut.end)} · ${esc(x.cut.speaker || "?")}${x.speaker_source === "voice" ? " (voice)" : ""}` : "";
    return `<tr><td><b>${short(x.date)}</b><div class="note">${esc(x.post?.title || x.name)}</div></td><td>${st}</td><td class="note">${res}</td></tr>`;
  }).join("");
}
$("#bPause").onclick = async () => { B.state = await api("/api/batch/control", { method: "POST", body: { action: B.state.status === "paused" ? "resume" : "pause" } }); renderQueue(); };
$("#bStop").onclick = async () => {
  if (!confirm("Stop the batch? Finished results are kept and can still be reviewed and applied.")) return;
  B.state = await api("/api/batch/control", { method: "POST", body: { action: "stop" } }); renderQueue();
};
$("#bToReview").onclick = () => batchGo(4);

// ---- step 4
const needsLook = (x) => x.state === "error" || (x.flags || []).length > 0;
function renderReview(full) {
  const items = B.state.items.filter((x) => x.state === "done" || x.state === "error");
  const good = items.filter((x) => !needsLook(x)).length, look = items.length - good;
  const diffs = items.filter((x) => x.diff).flatMap((x) => [Math.abs(x.diff.start), Math.abs(x.diff.end)]).sort((a, b) => a - b);
  $("#rSummary").innerHTML = `<div><b class="ok">${good}</b>look good</div><div><b style="color:#8a6100">${look}</b>need a look</div>
    <div><b class="bad">${items.filter((x) => (x.flags || []).includes("speaker_differs")).length}</b>speaker differs from WordPress</div>
    ${diffs.length ? `<div><b>±${Math.round(diffs[Math.floor(diffs.length / 2)])} s</b>typical difference from your hand cuts</div>` : ""}
    <div><b>${items.filter((x) => x.approved).length}</b>approved</div>`;
  $("#rApprovedCount").textContent = `${items.filter((x) => x.approved && x.state === "done").length} approved`;
  if (!full && document.activeElement && document.activeElement.closest("#rTable .editor")) return;  // don't disturb typing
  const sort = $("#rSort").value, f = $("#rFilter").value;
  let rows = items.filter((x) => f === "all" || (f === "look" && needsLook(x)) || (f === "approved" && x.approved) || (f === "unapproved" && !x.approved)
    || (x.flags || []).includes(f));
  const maxd = (x) => x.diff ? Math.max(Math.abs(x.diff.start), Math.abs(x.diff.end)) : 0;
  rows.sort((a, b) => sort === "newest" ? b.date.localeCompare(a.date) : sort === "diff" ? maxd(b) - maxd(a)
    : (needsLook(b) - needsLook(a)) || b.date.localeCompare(a.date));
  const tb = $("#rTable tbody");
  const keepOpen = [...B.open];
  tb.innerHTML = rows.map((x) => {
    const c = x.cut || {}, how = x.speaker_source === "voice" ? "🎙 by voice" : x.speaker_source === "transcript" ? "📝 transcript" : "not recognized";
    const differs = (x.flags || []).includes("speaker_differs");
    const fl = x.state === "error" ? `<span class="pill bad">failed</span>` : (x.flags || []).length
      ? x.flags.map((k) => `<span class="pill ${FLAG[k][0]}">${FLAG[k][1]}</span>`).join(" ") : `<span class="pill ok">Looks good</span>`;
    const yours = x.hand ? `${fmt(x.hand.start)} → ${fmt(x.hand.end)}<div class="note">your MP3</div>` : `—<div class="note">${x.mp3 ? "MP3 not found in recording" : "no MP3"}</div>`;
    const se = x.diff ? `${sgn(x.diff.start)} / ${sgn(x.diff.end)}<div class="note">AI vs yours</div>` : c.start != null ? `${fmt(c.start)} → ${fmt(c.end)}` : "";
    return `<tr class="clickable ${B.open.has(x.i) ? "open" : ""}" data-i="${x.i}"><td class="chev">${B.open.has(x.i) ? "▾" : "▸"}</td>
      <td><input type="checkbox" class="appr" ${x.approved ? "checked" : ""} ${x.state !== "done" ? "disabled" : ""}></td>
      <td><b>${short(x.date)}</b><div class="note">${esc(x.post?.title || c.title || x.name)}</div></td><td>${yours}</td><td>${se}</td>
      <td>${differs ? `<b class="bad">${esc(c.speaker || "")}</b>` : esc(c.speaker || "—")}<div class="note">${how}${differs ? ` · WordPress says ${esc(x.post?.speaker || "")}` : ""}</div></td>
      <td>${fl}</td><td><a class="button secondary small" href="/#${x.job}/3" target="_blank" rel="noopener">Open ↗</a></td></tr>
      ${B.open.has(x.i) ? `<tr class="editor" data-i="${x.i}"><td colspan="8"><div class="ed">Loading…</div></td></tr>` : ""}`;
  }).join("") || `<tr><td colspan="8" class="note" style="text-align:center">Nothing here yet.</td></tr>`;
  $$("#rTable tr.clickable").forEach((tr) => tr.onclick = (e) => {
    if (e.target.closest("a, input")) return;
    const i = +tr.dataset.i; B.open.has(i) ? B.open.delete(i) : B.open.add(i); renderReview(true);
  });
  $$("#rTable .appr").forEach((cb) => cb.onchange = async () => {
    const i = +cb.closest("tr").dataset.i;
    await api("/api/batch/approve", { method: "POST", body: { indices: [i], approved: cb.checked } });
    B.state.items[i].approved = cb.checked; renderReview(false);
  });
  $$("#rTable tr.editor").forEach((tr) => buildEditor(tr, B.state.items[+tr.dataset.i]));
}
["#rSort", "#rFilter"].forEach((s) => $(s).onchange = () => renderReview(true));
$("#rExpand").onclick = () => { B.state.items.filter((x) => x.state === "done" && needsLook(x)).forEach((x) => B.open.add(x.i)); renderReview(true); };
$("#rApproveGood").onclick = async () => {
  const idx = B.state.items.filter((x) => x.state === "done" && !needsLook(x)).map((x) => x.i);
  await api("/api/batch/approve", { method: "POST", body: { indices: idx, approved: true } });
  B.state = await api("/api/batch"); renderReview(true);
};
$("#rToApply").onclick = () => batchGo(5);

async function buildEditor(tr, x) {
  const box = $(".ed", tr), c = x.cut || {}, total = x.duration || 1;
  if (!B.tx[x.job]) B.tx[x.job] = await api(`/api/jobs/${x.job}/transcript`).catch(() => []);
  const segs = B.tx[x.job];
  const pct = (t) => `${(t / total) * 100}%`;
  const series = (B.meta.series || []).map((s) => `<option value="${s.id}" ${String(s.id) === String(c.series) ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  const d = x.speaker_detail || {};
  const spHint = x.speaker_source === "voice" ? `🎙 voice match ${d.score}` : x.speaker_source === "transcript" ? `📝 ${esc(d.evidence || "from the transcript")}` : "⚠ voice not recognized";
  box.innerHTML = `<div>
      <h4>Sermon audio</h4>
      <div class="mwave"><img src="/api/jobs/${x.job}/waveform.png" alt=""><div class="reg"></div>${x.hand ? `<div class="old" style="left:${pct(x.hand.start)};width:${pct(x.hand.end - x.hand.start)}"></div>` : ""}<div class="ph"></div></div>
      <div class="note" style="margin-top:.3rem">Shaded: the cut that will be used${x.hand ? " · dashed red: your hand-made MP3" : ""} · click the waveform to listen anywhere</div>
      <audio controls preload="none" src="/api/jobs/${x.job}/preview.mp3" style="width:100%;margin:.4rem 0 0"></audio>
      <div class="edges">
        <div class="edge"><div class="row1"><b>Start</b><input class="time e-start" value="${fmt(c.start)}"><button class="secondary small" data-a="ls">▶ Listen</button><button class="secondary small" data-a="s-5">− 5 s</button><button class="secondary small" data-a="s+5">+ 5 s</button></div><ul class="lines l-start"></ul></div>
        <div class="edge"><div class="row1"><b>End</b><input class="time e-end" value="${fmt(c.end)}"><button class="secondary small" data-a="le">▶ Last 10 s</button><button class="secondary small" data-a="e-5">− 5 s</button><button class="secondary small" data-a="e+5">+ 5 s</button></div><ul class="lines l-end"></ul></div>
      </div></div>
    <div><h4>Details</h4><div class="fields">
      <label class="wide">Title <input class="f-title" value="${esc(c.title || "")}"><small class="note">${x.post ? "from WordPress (kept)" : "suggested by the AI"}</small></label>
      <label>Speaker <input class="f-speaker" list="speakers" value="${esc(c.speaker || "")}"><small class="note ${(x.flags || []).includes("speaker_differs") ? "bad" : ""}">${spHint}${x.post?.speaker && x.post.speaker !== c.speaker ? ` · WordPress says ${esc(x.post.speaker)}` : ""}</small></label>
      <label>Series <select class="f-series"><option value="">(no series)</option>${series}</select></label>
      <label>Scripture <input class="f-scripture" value="${esc(c.scripture || "")}"></label>
      <label>Date <input type="date" class="f-date" value="${c.date || x.date}"></label>
      <label class="wide">Description <textarea rows="3" class="f-description">${esc(c.description || "")}</textarea><small class="note">✨ written by the AI${x.post ? " (only added if the post has none)" : ""}</small></label>
    </div>
    <div class="edact"><button data-a="save">✓ Save &amp; approve</button><button class="secondary" data-a="reset">Reset</button><button class="secondary" data-a="redetect">↻ Re-run detection</button>
      <span class="grow"></span><a class="button secondary small" href="/#${x.job}/3" target="_blank" rel="noopener">Open ↗</a></div></div>`;
  const au = $("audio", box), sIn = $(".e-start", box), eIn = $(".e-end", box);
  const lines = (ul, lo, hi, cur, which) => {
    const near = segs.filter((s) => s.start >= lo && s.start <= hi);
    ul.innerHTML = near.map((s) => `<li data-t="${which === "start" ? s.start : s.end + 1}" data-play="${s.start}" class="${Math.abs((which === "start" ? s.start : s.end) - cur) < 1.5 ? "cur" : ""}">
      <span class="t">${fmt(s.start).replace(/^0:/, "")}</span><span class="v ${s.voice === "P" ? "" : "o"}">${s.voice === "P" ? "preacher" : "other"}</span>
      <span>${esc(s.text)}</span><span class="b" data-set="${which}">${which} here</span></li>`).join("") || `<li class="note">no speech nearby</li>`;
  };
  const draw = () => {
    const s = parse(sIn.value), e = parse(eIn.value);
    $(".reg", box).style.left = pct(s); $(".reg", box).style.width = pct(Math.max(0, e - s));
    lines($(".l-start", box), s - 45, s + 30, s, "start"); lines($(".l-end", box), e - 45, e + 30, e, "end");
    $$(".lines", box).forEach((ul) => { const c = $("li.cur", ul); if (c) ul.scrollTop = c.offsetTop - ul.offsetTop - 40; });
  };
  draw();
  [sIn, eIn].forEach((i) => i.onchange = draw);
  au.ontimeupdate = () => $(".ph", box).style.left = pct(au.currentTime);
  const play = (t) => { au.currentTime = Math.max(0, t); au.play(); };
  $(".mwave", box).onclick = (ev) => { const r = ev.currentTarget.getBoundingClientRect(); play((ev.clientX - r.left) / r.width * total); };
  box.onclick = async (ev) => {
    const li = ev.target.closest(".lines li[data-t]");
    if (li) {
      if (ev.target.dataset.set) { (ev.target.dataset.set === "start" ? sIn : eIn).value = fmt(+li.dataset.t); draw(); }
      else play(+li.dataset.play);
      return;
    }
    const a = ev.target.dataset.a;
    if (!a) return;
    if (a === "ls") play(parse(sIn.value));
    if (a === "le") play(parse(eIn.value) - 10);
    if (a[1] === "-" || a[1] === "+") { const inp = a[0] === "s" ? sIn : eIn; inp.value = fmt(parse(inp.value) + (a[1] === "+" ? 5 : -5)); draw(); }
    if (a === "save") {
      const sel = $(".f-series", box);
      const fields = { start: parse(sIn.value), end: parse(eIn.value), title: $(".f-title", box).value.trim(), speaker: $(".f-speaker", box).value.trim(),
        series: sel.value, series_name: sel.value ? sel.selectedOptions[0].text : "", scripture: $(".f-scripture", box).value.trim(),
        date: $(".f-date", box).value, description: $(".f-description", box).value.trim() };
      if (fields.end - fields.start < 60) return alert("The sermon needs to be at least a minute long.");
      await api(`/api/batch/items/${x.i}`, { method: "POST", body: { fields, approved: true } });
      B.open.delete(x.i);
      B.state = await api("/api/batch");
      const next = B.state.items.find((y) => y.state === "done" && needsLook(y) && !y.approved);  // open the next one
      if (next) B.open.add(next.i);
      renderReview(true);
    }
    if (a === "reset") { await api(`/api/batch/items/${x.i}/reset`, { method: "POST" }); B.state = await api("/api/batch"); renderReview(true); }
    if (a === "redetect") {
      if (!confirm("Run the sermon detection again for this service? It takes a few minutes.")) return;
      await api(`/api/batch/items/${x.i}/redetect`, { method: "POST" }); delete B.tx[x.job];
      box.innerHTML = `<div class="note">Re-running detection… this row updates when it's done.</div>`;
    }
  };
}

// ---- step 5
async function loadPlan() {
  const p = await api("/api/batch/plan"), o = p.options;
  const L = [];
  L.push(["NAS", o.nas_mp3 ? `Save <b>${p.mp3s} MP3s</b>${o.nas_slide ? ` and <b>${p.slides} slides</b>` : ""} next to their recordings.${p.kept ? ` ${p.kept} hand-made MP3s are kept; the new ones are saved alongside as <code>… (2).mp3</code>.` : ""}${o.existing === "replace" ? " Hand-made MP3s are renamed to <code>….original.mp3</code>." : ""}`
    : "Nothing is saved to the NAS."]);
  const wpx = [];
  if (o.wp_speaker) wpx.push(`change the speaker on <b>${p.speaker_changes.length} posts</b>`);
  if (o.wp_description) wpx.push(`add a description to posts that have none (up to <b>${p.descriptions}</b>)`);
  if (o.wp_audio) wpx.push(`<b>replace the audio</b> on ${p.audio} posts`);
  L.push(["WordPress", wpx.length ? wpx.join("; ") + ". Nothing is published." : "No changes."]);
  if (o.wp_drafts) L.push(["Drafts", `Create <b>${p.drafts} draft posts</b> for services with no post.`]);
  L.push(["Voices", o.teach ? Object.entries(p.learn).map(([n, k]) => `${esc(n)} +${k}`).join(", ") || "nothing to learn" : "off"]);
  $("#aPlan").innerHTML = `<li><span class="k">Approved</span><span><b>${p.approved}</b> services</span></li>` + L.map(([k, v]) => `<li><span class="k">${k}</span><span>${v}</span></li>`).join("");
  $("#aSpeakersCard").hidden = !(o.wp_speaker && p.speaker_changes.length);
  $("#aSpeakers tbody").innerHTML = p.speaker_changes.map((c) => `<tr><td>${short(c.date)} · ${esc(c.title || "")}</td><td>${esc(c.now || "—")}</td><td><b>${esc(c.new)}</b></td><td class="note">${esc(c.why)}</td></tr>`).join("");
  $("#aApply").disabled = $("#aNasOnly").disabled = !p.approved;
  renderApply();
}
function renderApply() {
  const a = B.state.apply;
  $("#aRun").hidden = !a;
  const running = a && a.status === "running";
  $("#aApply").hidden = $("#aNasOnly").hidden = !!running;
  $("#aNew").hidden = !(B.state.status === "done" && (!a || a.status === "done"));
  if (!a) return;
  $("#aNow").textContent = running ? `Applying ${a.done + 1 > a.total ? a.total : a.done + 1} of ${a.total}…` : "Done";
  $("#aCounts").textContent = `${a.done} / ${a.total}`;
  $("#aProg").value = a.total ? a.done / a.total : 1;
  $("#aLog").innerHTML = a.log.slice().reverse().map((l) => `<li>${esc(l)}</li>`).join("");
}
async function doApply(nasOnly) {
  if (!confirm(nasOnly ? "Save the approved MP3s (and slides) to the NAS now? WordPress won't be touched." : "Apply the changes listed above?")) return;
  await api("/api/batch/apply", { method: "POST", body: { nas_only: nasOnly } });
  B.state = await api("/api/batch"); renderApply();
}
$("#aApply").onclick = () => doApply(false);
$("#aNasOnly").onclick = () => doApply(true);
$("#bNew").onclick = () => $("#aNew").click();
$("#aNew").onclick = async () => {
  try { await api("/api/batch/control", { method: "POST", body: { action: "clear" } }); } catch (e) { return alert(e.message); }
  B.open.clear(); B.tx = {}; await batchOpen();
};

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
