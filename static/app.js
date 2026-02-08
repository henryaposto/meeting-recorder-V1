// ═══ State ═══
let mr = null, chunks = [], blob = null;
let transcript = "", summary = "", email = "", chatHist = [];
let ti = null, sec = 0, sr = null, live = "", interim = "";
let actx = null, anl = null, af = null;
const tones = ["casual", "professional", "urgent"];
let toneIdx = 0;
let dict = null, isDictating = false;
let activeRecordingId = null;
let recordings = [];
let meetingType = "sales";
let emailType = "customer";
const defaultPills = ["What are the key next steps?", "Any risks to flag?", "Who owns what?"];

// ═══ DOM ═══
const $ = id => document.getElementById(id);
const recBtn = $("recordBtn"), timer = $("timer"), liveTag = $("liveTag");
const waveWrap = $("waveWrap"), waveC = $("waveCanvas"), player = $("player");
const txArea = $("transcriptArea"), sumArea = $("summaryArea"), sumBtn = $("summarizeBtn");
const emArea = $("emailArea"), emBtn = $("emailBtn"), emTools = $("emailTools");
const shorterBtn = $("shorterBtn"), longerBtn = $("longerBtn");
const toneBtn = $("toneBtn"), toneLabel = $("toneLabel");
const retryBtn = $("retryBtn"), copyBtn = $("copyBtn");
const qeIn = $("qeInput"), qeBtn = $("qeBtn");
const chatThread = $("chatThread"), chatPills = $("chatPills");
const chatIn = $("chatIn"), sendBtn = $("sendBtn"), micBtn = $("micBtn"), micDot = $("micDot");
const sidebar = $("sidebar"), recordingsList = $("recordingsList");
const newCallBtn = $("newCallBtn"), sidebarToggle = $("sidebarToggle");
const sidebarOverlay = $("sidebarOverlay");
const ctxMenu = $("ctxMenu"), ctxRename = $("ctxRename");
const alertsArea = $("alertsArea");
const transcriptToggle = $("transcriptToggle"), transcriptPreview = $("transcriptPreview");
const expandIcon = $("expandIcon"), wordCount = $("wordCount");
let txExpanded = false;
let txLocked = false;
let ctxTargetId = null;


// ═══ Helpers ═══
function esc(t) {
  const d = document.createElement("div");
  d.textContent = t;
  return d.innerHTML;
}

function showErr(el, m) {
  el.innerHTML = '<div class="error-msg">' + esc(m) + '</div>';
}

function skel(el) {
  el.innerHTML = '<div class="skeleton" style="width:92%"></div>' +
    '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
}

function fmt(s) {
  return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

function md(t) {
  return t
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^- \[( |x)\] (.+)$/gm, function(_, c, i) {
      return '<li><input type=checkbox ' + (c === "x" ? "checked" : "") + ' disabled> ' + i + '</li>';
    })
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>")
    .replace(/\n{2,}/g, "<br><br>")
    .replace(/\n/g, "<br>");
}

// ═══ Speech Recognition ═══
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

function startSR() {
  sr = new SR();
  sr.continuous = true;
  sr.interimResults = true;
  sr.lang = "en-US";

  sr.onresult = function(e) {
    let f = "", im = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) f += t + " ";
      else im = t;
    }
    if (f) live += f;
    interim = im;
    txArea.innerHTML = esc(live) +
      (interim ? '<span style="color:#9CA3AF">' + esc(interim) + '</span>' : "");
    txArea.scrollTop = txArea.scrollHeight;
    txArea.scrollLeft = txArea.scrollWidth;
  };

  sr.onerror = function(e) {
    if (e.error !== "no-speech") console.error(e.error);
  };

  sr.onend = function() {
    if (mr && mr.state === "recording") sr.start();
  };

  sr.start();
}

// ═══ Waveform ═══
function startWave(stream) {
  actx = new (window.AudioContext || window.webkitAudioContext)();
  anl = actx.createAnalyser();
  actx.createMediaStreamSource(stream).connect(anl);
  anl.fftSize = 256;
  const buf = new Uint8Array(anl.frequencyBinCount);
  const c = waveC, ctx = c.getContext("2d");

  (function draw() {
    af = requestAnimationFrame(draw);
    anl.getByteFrequencyData(buf);
    c.width = c.offsetWidth * 2;
    c.height = c.offsetHeight * 2;
    ctx.scale(2, 2);
    const w = c.offsetWidth, h = c.offsetHeight;
    const n = 48, bw = w / n - 2, step = Math.floor(buf.length / n);
    ctx.clearRect(0, 0, w, h);
    for (let i = 0; i < n; i++) {
      const v = buf[i * step] / 255;
      const bh = Math.max(1.5, v * h * .8);
      const x = i * (bw + 2), y = (h - bh) / 2;
      ctx.fillStyle = "rgba(99,102,241," + (.2 + v * .6) + ")";
      ctx.beginPath();
      ctx.roundRect(x, y, bw, bh, 1.5);
      ctx.fill();
    }
  })();
}

function stopWave() {
  if (af) cancelAnimationFrame(af);
  if (actx) actx.close();
  actx = null;
  anl = null;
}

// ═══ Transcript Toggle ═══
function toggleTranscript(forceState) {
  txExpanded = typeof forceState === "boolean" ? forceState : !txExpanded;
  if (txExpanded) {
    txArea.classList.remove("hidden");
    transcriptPreview.style.display = "none";
    expandIcon.classList.add("open");
  } else {
    txArea.classList.add("hidden");
    transcriptPreview.style.display = "";
    expandIcon.classList.remove("open");
  }
}

function updateTranscriptPreview(text) {
  if (!text) {
    transcriptPreview.textContent = "";
    wordCount.textContent = "";
    return;
  }
  var preview = text.length > 80 ? text.substring(0, 80) + "\u2026" : text;
  transcriptPreview.textContent = preview;
  var wc = text.trim().split(/\s+/).length;
  wordCount.textContent = "(" + wc + " words)";
}

transcriptToggle.onclick = function() { if (!txLocked) toggleTranscript(); };
transcriptPreview.onclick = function() { if (!txLocked) toggleTranscript(true); };

// ═══ Recording ═══
recBtn.onclick = async function() {
  if (mr && mr.state === "recording") stopRec();
  else await startRec();
};

async function startRec() {
  try {
    const s = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false
      }
    });
    mr = new MediaRecorder(s, { mimeType: "audio/webm" });
    chunks = [];
    live = "";
    interim = "";
    mr.ondataavailable = function(e) {
      if (e.data.size > 0) chunks.push(e.data);
    };

    mr.onstop = async function() {
      console.log("STOP FIRED - onstop called");
      blob = new Blob(chunks, { type: "audio/webm" });
      player.src = URL.createObjectURL(blob);
      player.classList.remove("hidden");
      s.getTracks().forEach(function(t) { t.stop(); });
      stopWave();
      txLocked = false;
      txArea.classList.remove("tx-ticker");
      expandIcon.style.display = "";
      wordCount.textContent = "";

      // Send audio to Whisper instead of using browser transcript
      txArea.innerHTML = '<span class="empty">Transcribing with AI...</span>';
      recBtn.disabled = true;

      try {
        var fd = new FormData();
        fd.append("audio", blob, "recording.webm");
        console.log("Sending audio to Whisper...", blob.size, "bytes");
        var r = await fetch("/api/transcribe", { method: "POST", body: fd });
        var d = await r.json();
        console.log("Whisper response:", d);

        if (d.error) {
          txArea.innerHTML = '<span class="empty">Transcription failed: ' + esc(d.error) + '</span>';
          recBtn.disabled = false;
          return;
        }

        transcript = d.transcript || "";
        console.log("Transcript:", transcript.substring(0, 100), "(" + transcript.length + " chars)");
        if (transcript) {
          console.log("Setting UI with transcript...");
          txArea.textContent = transcript;
          updateTranscriptPreview(transcript);
          toggleTranscript(false);
          sumBtn.disabled = false;
          chatIn.disabled = false;
          sendBtn.disabled = false;
          chatPills.classList.remove("hidden");
          saveRecording(transcript, sec);
          analyzeTranscript(transcript);
        } else {
          txArea.innerHTML = '<span class="empty">No speech detected</span>';
          transcriptPreview.textContent = "";
          wordCount.textContent = "";
        }
      } catch (e) {
        txArea.innerHTML = '<span class="empty">Transcription error: ' + esc(e.message) + '</span>';
      }

      recBtn.disabled = false;
    };

    console.log("mr exists:", typeof mr !== "undefined");
    console.log("mr state:", mr?.state);
    console.log("mr.onstop is set:", typeof mr?.onstop === "function");

    mr.start(1000);
    startSR();
    startWave(s);
    waveWrap.classList.remove("hidden");
    recBtn.classList.add("on");
    liveTag.classList.remove("hidden");
    timer.classList.add("on");
    txArea.innerHTML = "";
    transcriptPreview.textContent = "";
    wordCount.innerHTML = '<span class="tx-live-dot"></span> Live';
    txLocked = true;
    expandIcon.style.display = "none";
    toggleTranscript(true);
    txArea.classList.add("tx-ticker");

    // Reset downstream sections
    sumArea.innerHTML = "";
    emArea.innerHTML = "";
    emTools.classList.add("hidden");
    sumBtn.disabled = true;
    emBtn.disabled = true;
    chatPills.classList.add("hidden");
    sec = 0;
    timer.textContent = "00:00";
    ti = setInterval(function() {
      sec++;
      timer.textContent = fmt(sec);
    }, 1000);
  } catch (e) {
    alert("Microphone access denied.");
    console.error(e);
  }
}

function stopRec() {
  if (!mr) return;
  mr.stop();
  if (sr) sr.stop();
  recBtn.classList.remove("on");
  liveTag.classList.add("hidden");
  timer.classList.remove("on");
  waveWrap.classList.add("hidden");
  clearInterval(ti);
}

// ═══ Sidebar: Save / Load / Delete ═══

async function saveRecording(text, duration) {
  try {
    const r = await fetch("/api/save_recording", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: text, duration: duration })
    });
    const d = await r.json();
    if (d.id) {
      activeRecordingId = d.id;
      await loadRecordings();
      generateName(d.id, text);
    }
  } catch (e) {
    console.error("Failed to save recording:", e);
  }
}

async function analyzeTranscript(text) {
  try {
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: text })
    });
    const d = await r.json();
    meetingType = d.meeting_type || "sales";
    emailType = d.email_default || "customer";
    updateEmailTypeChips();
    renderPills(d.pills || defaultPills);
    renderAlerts(d.alerts || []);
  } catch (e) {
    console.error("Failed to analyze:", e);
    renderPills(defaultPills);
  }
}

function renderPills(pills) {
  var html = "";
  pills.forEach(function(q) {
    html += '<button class="pill" data-q="' + esc(q) + '">' + esc(q) + '</button>';
  });
  chatPills.innerHTML = html;
  chatPills.querySelectorAll(".pill").forEach(function(b) {
    b.onclick = function() { chatIn.value = b.dataset.q; sendChat(); };
  });
}

function renderAlerts(alerts) {
  if (!alerts || alerts.length === 0) {
    alertsArea.classList.add("hidden");
    return;
  }
  var icons = {
    urgent: "\ud83d\udd34",
    positive: "\u2705",
    risk: "\u26a0\ufe0f",
    insight: "\ud83d\udca1"
  };
  var html = "";
  alerts.forEach(function(a) {
    var icon = icons[a.type] || "\ud83d\udca1";
    var cls = a.type || "insight";
    html += '<div class="alert-badge ' + cls + '">' + icon + ' ' + esc(a.text) + '</div>';
  });
  alertsArea.innerHTML = html;
  alertsArea.classList.remove("hidden");
}

function updateEmailTypeChips() {
  document.querySelectorAll(".email-type-chip").forEach(function(chip) {
    chip.classList.toggle("active", chip.dataset.type === emailType);
  });
}

async function generateName(id, text) {
  try {
    const r = await fetch("/api/generate_name", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id, transcript: text })
    });
    const d = await r.json();
    if (d.name) {
      loadRecordings();
    }
  } catch (e) {
    console.error("Failed to generate name:", e);
  }
}

async function loadRecordings() {
  try {
    const r = await fetch("/api/recordings");
    recordings = await r.json();
    renderRecordingsList();
  } catch (e) {
    console.error("Failed to load recordings:", e);
  }
}

function groupByDate(recs) {
  const groups = {};
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const week = new Date(today); week.setDate(today.getDate() - 7);
  const month = new Date(today); month.setDate(today.getDate() - 30);

  recs.forEach(function(rec) {
    const d = new Date(rec.created_at);
    let label;
    if (d >= today) label = "Today";
    else if (d >= yesterday) label = "Yesterday";
    else if (d >= week) label = "Last 7 Days";
    else if (d >= month) label = "Last 30 Days";
    else label = "Older";

    if (!groups[label]) groups[label] = [];
    groups[label].push(rec);
  });

  return groups;
}

function renderRecordingsList() {
  if (recordings.length === 0) {
    recordingsList.innerHTML = '<div class="recordings-empty">No recordings yet.<br>Hit the red button to start.</div>';
    return;
  }

  const groups = groupByDate(recordings);
  const order = ["Today", "Yesterday", "Last 7 Days", "Last 30 Days", "Older"];
  let html = "";

  order.forEach(function(label) {
    const recs = groups[label];
    if (!recs) return;

    html += '<div class="sidebar-group-label">' + esc(label) + '</div>';
    recs.forEach(function(rec) {
      const isActive = rec.id === activeRecordingId;
      const dur = rec.duration ? fmt(rec.duration) : "";
      html += '<div class="recording-item' + (isActive ? ' active' : '') + '" data-id="' + esc(rec.id) + '">' +
        '<div class="recording-item-info">' +
        '<div class="recording-item-name">' + esc(rec.name) + '</div>' +
        '<div class="recording-item-meta">' + dur + '</div>' +
        '</div>' +
        '<button class="recording-delete" data-id="' + esc(rec.id) + '" title="Delete">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
        '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
        '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>' +
        '</svg></button></div>';
    });
  });

  recordingsList.innerHTML = html;

  // Attach click handlers
  recordingsList.querySelectorAll(".recording-item").forEach(function(el) {
    el.addEventListener("click", function(e) {
      if (e.target.closest(".recording-delete")) return;
      loadRecording(el.dataset.id);
    });
  });

  recordingsList.querySelectorAll(".recording-delete").forEach(function(btn) {
    btn.addEventListener("click", function(e) {
      e.stopPropagation();
      deleteRecording(btn.dataset.id);
    });
  });

  // Right-click context menu
  recordingsList.querySelectorAll(".recording-item").forEach(function(el) {
    el.addEventListener("contextmenu", function(e) {
      e.preventDefault();
      ctxTargetId = el.dataset.id;
      ctxMenu.classList.remove("hidden");
      ctxMenu.style.left = e.clientX + "px";
      ctxMenu.style.top = e.clientY + "px";
      // Keep menu in viewport
      var rect = ctxMenu.getBoundingClientRect();
      if (rect.right > window.innerWidth) ctxMenu.style.left = (window.innerWidth - rect.width - 8) + "px";
      if (rect.bottom > window.innerHeight) ctxMenu.style.top = (window.innerHeight - rect.height - 8) + "px";
    });
  });
}

async function loadRecording(id) {
  try {
    const r = await fetch("/api/recording/" + id);
    const d = await r.json();
    if (d.error) return;

    activeRecordingId = id;
    transcript = d.transcript || "";
    summary = d.summary || "";
    email = d.email || "";
    chatHist = [];

    // Update UI
    txArea.textContent = transcript || "";
    if (transcript) {
      sumBtn.disabled = false;
      chatIn.disabled = false;
      sendBtn.disabled = false;
      chatPills.classList.remove("hidden");
    }

    if (summary) {
      sumArea.innerHTML = md(summary);
      emBtn.disabled = false;
    } else {
      sumArea.innerHTML = "";
      emBtn.disabled = true;
    }

    if (email) {
      emArea.textContent = email;
      emTools.classList.remove("hidden");
    } else {
      emArea.innerHTML = "";
      emTools.classList.add("hidden");
    }

    // Reset chat
    chatThread.innerHTML = '<div class="brow brow-ai"><div class="bub bub-ai">Record a meeting, then ask me anything about it.</div></div>';

    // Reset player
    player.classList.add("hidden");
    player.src = "";
    blob = null;

    // Analyze for smart features
    if (transcript) analyzeTranscript(transcript);

    // Update sidebar active state
    renderRecordingsList();

    // Close sidebar overlay on mobile
    closeSidebarMobile();
  } catch (e) {
    console.error("Failed to load recording:", e);
  }
}

async function deleteRecording(id) {
  if (!confirm("Delete this recording?")) return;

  try {
    await fetch("/api/recording/" + id, { method: "DELETE" });
    if (activeRecordingId === id) {
      activeRecordingId = null;
      clearState();
    }
    loadRecordings();
  } catch (e) {
    console.error("Failed to delete recording:", e);
  }
}

function clearState() {
  activeRecordingId = null;
  transcript = "";
  summary = "";
  email = "";
  chatHist = [];
  blob = null;
  meetingType = "sales";
  emailType = "customer";
  updateEmailTypeChips();
  alertsArea.innerHTML = "";
  alertsArea.classList.add("hidden");

  txArea.innerHTML = "";
  sumArea.innerHTML = "";
  emArea.innerHTML = "";
  emTools.classList.add("hidden");
  sumBtn.disabled = true;
  emBtn.disabled = true;
  chatIn.disabled = true;
  sendBtn.disabled = true;
  chatPills.classList.add("hidden");
  player.classList.add("hidden");
  player.src = "";
  sec = 0;
  timer.textContent = "00:00";
  timer.classList.remove("on");
  chatThread.innerHTML = '<div class="brow brow-ai"><div class="bub bub-ai">Record a meeting, then ask me anything about it.</div></div>';

  renderRecordingsList();
  closeSidebarMobile();
}

// ═══ Context Menu & Rename ═══
document.addEventListener("click", function() {
  ctxMenu.classList.add("hidden");
  ctxTargetId = null;
});

ctxRename.onclick = function(e) {
  e.stopPropagation();
  ctxMenu.classList.add("hidden");
  if (!ctxTargetId) return;
  startInlineRename(ctxTargetId);
  ctxTargetId = null;
};

function startInlineRename(id) {
  var nameEl = recordingsList.querySelector('.recording-item[data-id="' + id + '"] .recording-item-name');
  if (!nameEl) return;
  var current = nameEl.textContent;
  var input = document.createElement("input");
  input.type = "text";
  input.className = "recording-item-name-input";
  input.value = current;
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  function finish(save) {
    var newName = input.value.trim();
    if (save && newName && newName !== current) {
      renameRecording(id, newName);
    } else {
      // Revert
      var span = document.createElement("div");
      span.className = "recording-item-name";
      span.textContent = current;
      input.replaceWith(span);
    }
  }

  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", function() { finish(true); });
}

async function renameRecording(id, name) {
  try {
    var r = await fetch("/api/rename_recording", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id, name: name })
    });
    var d = await r.json();
    if (d.ok) {
      loadRecordings();
    }
  } catch (e) {
    console.error("Failed to rename:", e);
    loadRecordings();
  }
}

// ═══ Sidebar Toggle ═══
sidebarToggle.onclick = function() {
  sidebar.classList.toggle("collapsed");
  if (window.innerWidth <= 768) {
    if (sidebar.classList.contains("collapsed")) {
      sidebarOverlay.classList.add("hidden");
    } else {
      sidebarOverlay.classList.remove("hidden");
    }
  }
};

sidebarOverlay.onclick = function() {
  sidebar.classList.add("collapsed");
  sidebarOverlay.classList.add("hidden");
};

newCallBtn.onclick = function() {
  clearState();
};

function closeSidebarMobile() {
  if (window.innerWidth <= 768) {
    sidebar.classList.add("collapsed");
    sidebarOverlay.classList.add("hidden");
  }
}

// ═══ Summarize ═══
sumBtn.onclick = async function() {
  if (!transcript) return;
  sumBtn.disabled = true;
  sumBtn.classList.add("loading");
  skel(sumArea);
  const t0 = Date.now();

  try {
    const r = await fetch("/api/summarize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: transcript })
    });
    const d = await r.json();
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

    if (d.error) {
      showErr(sumArea, d.error);
    } else {
      summary = d.summary;
      sumArea.innerHTML = md(summary) +
        '<div class="meta"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" ' +
        'stroke="#10B981" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/>' +
        '</svg>' + elapsed + 's</div>';
      emBtn.disabled = false;
    }
  } catch (e) {
    showErr(sumArea, e.message);
  }

  sumBtn.classList.remove("loading");
  sumBtn.disabled = false;
};

// ═══ Email Type Chips ═══
document.querySelectorAll(".email-type-chip").forEach(function(chip) {
  chip.addEventListener("click", function() {
    emailType = chip.dataset.type;
    updateEmailTypeChips();
  });
});

// ═══ Email ═══
emBtn.onclick = async function() {
  if (!transcript) return;
  emBtn.disabled = true;
  emBtn.classList.add("loading");
  skel(emArea);

  try {
    const r = await fetch("/api/email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: transcript, summary: summary, email_type: emailType })
    });
    const d = await r.json();

    if (d.error) {
      showErr(emArea, d.error);
    } else {
      email = d.email;
      emArea.textContent = email;
      emTools.classList.remove("hidden");
    }
  } catch (e) {
    showErr(emArea, e.message);
  }

  emBtn.classList.remove("loading");
  emBtn.disabled = false;
};

// ── Email regeneration ──
async function regenEmail(style, btn) {
  btn.classList.add("loading");
  emArea.innerHTML = '<div class="skeleton" style="width:90%"></div><div class="skeleton"></div>';

  try {
    const r = await fetch("/api/email/regenerate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        transcript: transcript,
        summary: summary,
        current_email: email,
        style: style
      })
    });
    const d = await r.json();

    if (d.error) showErr(emArea, d.error);
    else { email = d.email; emArea.textContent = email; }
  } catch (e) {
    showErr(emArea, e.message);
  }

  btn.classList.remove("loading");
}

const teamUpdateBtn = $("teamUpdateBtn");
const salesFollowUpBtn = $("salesFollowUpBtn");
shorterBtn.onclick = function() { regenEmail("shorter", shorterBtn); };
longerBtn.onclick = function() { regenEmail("longer", longerBtn); };
teamUpdateBtn.onclick = function() { regenEmail("team_update", teamUpdateBtn); };
salesFollowUpBtn.onclick = async function() {
  if (!transcript) return;
  salesFollowUpBtn.classList.add("loading");
  emArea.innerHTML = '<div class="skeleton" style="width:90%"></div><div class="skeleton"></div>';
  try {
    var r = await fetch("/api/email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: transcript, summary: summary, email_type: "sales_followup" })
    });
    var d = await r.json();
    if (d.error) showErr(emArea, d.error);
    else { email = d.email; emArea.textContent = email; emTools.classList.remove("hidden"); }
  } catch (e) { showErr(emArea, e.message); }
  salesFollowUpBtn.classList.remove("loading");
};
retryBtn.onclick = function() { regenEmail("retry", retryBtn); };

toneBtn.onclick = function() {
  const t = tones[toneIdx];
  toneIdx = (toneIdx + 1) % tones.length;
  toneLabel.textContent = tones[toneIdx][0].toUpperCase() + tones[toneIdx].slice(1);
  regenEmail(t, toneBtn);
};

copyBtn.onclick = function() {
  navigator.clipboard.writeText(emArea.textContent);
  const orig = copyBtn.innerHTML;
  copyBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
    'stroke="#10B981" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>Copied';
  setTimeout(function() { copyBtn.innerHTML = orig; }, 1200);
};

// ── Quick edit ──
qeBtn.onclick = doQE;
qeIn.onkeydown = function(e) { if (e.key === "Enter") doQE(); };

async function doQE() {
  const ins = qeIn.value.trim();
  if (!ins || !email) return;
  qeBtn.classList.add("loading");
  emArea.innerHTML = '<div class="skeleton" style="width:88%"></div><div class="skeleton"></div>';

  try {
    const r = await fetch("/api/email/quick-edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_email: email, instruction: ins })
    });
    const d = await r.json();

    if (d.error) showErr(emArea, d.error);
    else { email = d.email; emArea.textContent = email; qeIn.value = ""; }
  } catch (e) {
    showErr(emArea, e.message);
  }

  qeBtn.classList.remove("loading");
}

// ═══ Chat ═══
sendBtn.onclick = sendChat;
chatIn.onkeydown = function(e) {
  if (e.key === "Enter" && !e.shiftKey) sendChat();
};
// Pills are now dynamic — handlers attached in renderPills()

// ── Voice dictation ──
micBtn.onclick = function() {
  if (!SR) return;
  if (isDictating) { stopDict(); return; }

  dict = new SR();
  dict.continuous = false;
  dict.interimResults = true;
  dict.lang = "en-US";
  const before = chatIn.value;

  dict.onresult = function(e) {
    let r = "";
    for (let i = 0; i < e.results.length; i++) r = e.results[i][0].transcript;
    chatIn.value = before + (before ? " " : "") + r;
  };

  dict.onend = function() { stopDict(); };
  dict.onerror = function() { stopDict(); };
  dict.start();
  isDictating = true;
  micBtn.classList.add("mic-on");
  micDot.classList.remove("hidden");
};

function stopDict() {
  if (dict) try { dict.stop(); } catch (e) {}
  isDictating = false;
  micBtn.classList.remove("mic-on");
  micDot.classList.add("hidden");
}

// ── Send message ──
async function sendChat() {
  const q = chatIn.value.trim();
  if (!q || !transcript) return;
  if (isDictating) stopDict();

  addBub("user", q);
  chatIn.value = "";
  sendBtn.disabled = true;
  const tid = addDots();

  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        transcript: transcript,
        history: chatHist,
        summary: summary
      })
    });
    const d = await r.json();
    rmDots(tid);

    if (d.error) {
      addBub("err", d.error);
    } else {
      addBub("ai", d.answer);
      chatHist.push({ role: "user", content: q }, { role: "assistant", content: d.answer });
    }
  } catch (e) {
    rmDots(tid);
    addBub("err", e.message);
  }

  sendBtn.disabled = false;
  chatIn.focus();
}

// ── Chat bubbles ──
function addBub(type, text) {
  const row = document.createElement("div");

  if (type === "user") {
    row.className = "brow brow-user";
    row.innerHTML = '<div class="bub bub-user">' + esc(text) + '</div>';
  } else if (type === "ai") {
    row.className = "brow brow-ai";
    const formatted = esc(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
    row.innerHTML = '<div class="bub bub-ai">' + formatted + '</div>';
  } else {
    row.className = "brow brow-ai";
    row.innerHTML = '<div class="bub bub-err">' + esc(text) + '</div>';
  }

  chatThread.appendChild(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

let dc = 0;

function addDots() {
  const id = "d" + (++dc);
  const r = document.createElement("div");
  r.className = "brow brow-ai";
  r.id = id;
  r.innerHTML = '<div class="bub bub-ai"><div class="dots">' +
    '<span></span><span></span><span></span></div></div>';
  chatThread.appendChild(r);
  r.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return id;
}

function rmDots(id) {
  const e = document.getElementById(id);
  if (e) e.remove();
}

// ═══ Keyboard Shortcuts ═══
document.addEventListener("keydown", function(e) {
  var mod = e.metaKey || e.ctrlKey;
  if (mod && e.key === "e") { e.preventDefault(); if (!emBtn.disabled) emBtn.click(); }
  if (mod && e.key === "s") { e.preventDefault(); if (!sumBtn.disabled) sumBtn.click(); }
  if (mod && e.key === "k") { e.preventDefault(); chatIn.focus(); }
});

// ═══ Init ═══
renderPills(defaultPills);
loadRecordings();
