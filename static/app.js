// ═══ State ═══
let mr=null,chunks=[],blob=null,transcript="",summary="",email="",chatHist=[];
let ti=null,sec=0,sr=null,live="",interim="";
let actx=null,anl=null,af=null;
const tones=["casual","professional","urgent"];let toneIdx=0;
let dict=null,isDictating=false;

// ═══ DOM ═══
const $=id=>document.getElementById(id);
const recBtn=$("recordBtn"),timer=$("timer"),liveTag=$("liveTag");
const waveWrap=$("waveWrap"),waveC=$("waveCanvas"),player=$("player");
const txArea=$("transcriptArea"),sumArea=$("summaryArea"),sumBtn=$("summarizeBtn");
const emArea=$("emailArea"),emBtn=$("emailBtn"),emTools=$("emailTools");
const shorterBtn=$("shorterBtn"),longerBtn=$("longerBtn");
const toneBtn=$("toneBtn"),toneLabel=$("toneLabel");
const retryBtn=$("retryBtn"),copyBtn=$("copyBtn");
const qeIn=$("qeInput"),qeBtn=$("qeBtn");
const chatThread=$("chatThread"),chatPills=$("chatPills");
const chatIn=$("chatIn"),sendBtn=$("sendBtn"),micBtn=$("micBtn"),micDot=$("micDot");

// ═══ Speech Recognition ═══
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;

function startSR(){
  sr=new SR();sr.continuous=true;sr.interimResults=true;sr.lang="en-US";
  sr.onresult=e=>{
    let f="",im="";
    for(let i=e.resultIndex;i<e.results.length;i++){
      const t=e.results[i][0].transcript;
      e.results[i].isFinal?f+=t+" ":im=t;
    }
    if(f)live+=f;interim=im;
    txArea.innerHTML=esc(live)+(interim?`<span style="color:#9CA3AF">${esc(interim)}</span>`:"");
    txArea.scrollTop=txArea.scrollHeight;
  };
  sr.onerror=e=>{if(e.error!=="no-speech")console.error(e.error)};
  sr.onend=()=>{if(mr&&mr.state==="recording")sr.start()};
  sr.start();
}

// ═══ Waveform ═══
function startWave(stream){
  actx=new(window.AudioContext||window.webkitAudioContext)();
  anl=actx.createAnalyser();actx.createMediaStreamSource(stream).connect(anl);
  anl.fftSize=256;const buf=new Uint8Array(anl.frequencyBinCount);
  const c=waveC,ctx=c.getContext("2d");
  (function draw(){
    af=requestAnimationFrame(draw);anl.getByteFrequencyData(buf);
    c.width=c.offsetWidth*2;c.height=c.offsetHeight*2;ctx.scale(2,2);
    const w=c.offsetWidth,h=c.offsetHeight,n=48,bw=w/n-2,step=Math.floor(buf.length/n);
    ctx.clearRect(0,0,w,h);
    for(let i=0;i<n;i++){
      const v=buf[i*step]/255,bh=Math.max(1.5,v*h*.8),x=i*(bw+2),y=(h-bh)/2;
      ctx.fillStyle=`rgba(99,102,241,${.2+v*.6})`;
      ctx.beginPath();ctx.roundRect(x,y,bw,bh,1.5);ctx.fill();
    }
  })();
}
function stopWave(){if(af)cancelAnimationFrame(af);if(actx)actx.close();actx=anl=null}

// ═══ Recording ═══
recBtn.onclick=async()=>{mr&&mr.state==="recording"?stopRec():await startRec()};

async function startRec(){
  try{
    const s=await navigator.mediaDevices.getUserMedia({audio:true});
    mr=new MediaRecorder(s,{mimeType:"audio/webm"});
    chunks=[];live="";interim="";
    mr.ondataavailable=e=>{if(e.data.size>0)chunks.push(e.data)};
    mr.onstop=()=>{
      blob=new Blob(chunks,{type:"audio/webm"});
      player.src=URL.createObjectURL(blob);player.classList.remove("hidden");
      s.getTracks().forEach(t=>t.stop());stopWave();
      transcript=live.trim();
      if(transcript){txArea.textContent=transcript;sumBtn.disabled=false;chatIn.disabled=false;sendBtn.disabled=false;chatPills.classList.remove("hidden")}
      else txArea.innerHTML='<span class="empty">No speech detected</span>';
    };
    mr.start(1000);startSR();startWave(s);
    waveWrap.classList.remove("hidden");
    recBtn.classList.add("on");liveTag.classList.remove("hidden");
    timer.classList.add("on");txArea.innerHTML="";
    // Reset
    sumArea.innerHTML="";emArea.innerHTML="";emTools.classList.add("hidden");
    sumBtn.disabled=true;emBtn.disabled=true;chatPills.classList.add("hidden");
    sec=0;timer.textContent="00:00";
    ti=setInterval(()=>{sec++;timer.textContent=fmt(sec)},1000);
  }catch(e){alert("Microphone access denied.");console.error(e)}
}

function stopRec(){
  if(!mr)return;mr.stop();if(sr)sr.stop();
  recBtn.classList.remove("on");liveTag.classList.add("hidden");
  timer.classList.remove("on");waveWrap.classList.add("hidden");
  clearInterval(ti);
}

function fmt(s){return`${String(Math.floor(s/60)).padStart(2,"0")}:${String(s%60).padStart(2,"0")}`}

// ═══ Helpers ═══
function esc(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}
function showErr(el,m){el.innerHTML=`<div class="error-msg">${esc(m)}</div>`}
function skel(el){el.innerHTML='<div class="skeleton" style="width:92%"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>'}
function md(t){
  return t.replace(/^### (.+)$/gm,"<h3>$1</h3>").replace(/^## (.+)$/gm,"<h2>$1</h2>")
    .replace(/^- \[( |x)\] (.+)$/gm,(_,c,i)=>`<li><input type=checkbox ${c==="x"?"checked":""} disabled> ${i}</li>`)
    .replace(/^- (.+)$/gm,"<li>$1</li>").replace(/(<li>.*<\/li>)/s,"<ul>$1</ul>")
    .replace(/\n{2,}/g,"<br><br>").replace(/\n/g,"<br>");
}

// ═══ Summarize ═══
sumBtn.onclick=async()=>{
  if(!transcript)return;sumBtn.disabled=true;sumBtn.classList.add("loading");skel(sumArea);
  const t0=Date.now();
  try{
    const r=await fetch("/api/summarize",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({transcript})});
    const d=await r.json(),el=((Date.now()-t0)/1000).toFixed(1);
    if(d.error)showErr(sumArea,d.error);
    else{summary=d.summary;sumArea.innerHTML=md(summary)+`<div class="meta"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>${el}s</div>`;emBtn.disabled=false}
  }catch(e){showErr(sumArea,e.message)}
  sumBtn.classList.remove("loading");sumBtn.disabled=false;
};

// ═══ Email ═══
emBtn.onclick=async()=>{
  if(!transcript)return;emBtn.disabled=true;emBtn.classList.add("loading");skel(emArea);
  try{
    const r=await fetch("/api/email",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({transcript,summary})});
    const d=await r.json();
    if(d.error)showErr(emArea,d.error);
    else{email=d.email;emArea.textContent=email;emTools.classList.remove("hidden")}
  }catch(e){showErr(emArea,e.message)}
  emBtn.classList.remove("loading");emBtn.disabled=false;
};

async function regenEmail(style,btn){
  btn.classList.add("loading");emArea.innerHTML='<div class="skeleton" style="width:90%"></div><div class="skeleton"></div>';
  try{
    const r=await fetch("/api/email/regenerate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({transcript,summary,current_email:email,style})});
    const d=await r.json();
    if(d.error)showErr(emArea,d.error);else{email=d.email;emArea.textContent=email}
  }catch(e){showErr(emArea,e.message)}
  btn.classList.remove("loading");
}

shorterBtn.onclick=()=>regenEmail("shorter",shorterBtn);
longerBtn.onclick=()=>regenEmail("longer",longerBtn);
retryBtn.onclick=()=>regenEmail("retry",retryBtn);
toneBtn.onclick=()=>{
  const t=tones[toneIdx];toneIdx=(toneIdx+1)%tones.length;
  toneLabel.textContent=tones[toneIdx][0].toUpperCase()+tones[toneIdx].slice(1);
  regenEmail(t,toneBtn);
};
copyBtn.onclick=()=>{
  navigator.clipboard.writeText(emArea.textContent);
  const o=copyBtn.innerHTML;copyBtn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="3" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>Copied';
  setTimeout(()=>copyBtn.innerHTML=o,1200);
};

qeBtn.onclick=doQE;qeIn.onkeydown=e=>{if(e.key==="Enter")doQE()};
async function doQE(){
  const ins=qeIn.value.trim();if(!ins||!email)return;
  qeBtn.classList.add("loading");emArea.innerHTML='<div class="skeleton" style="width:88%"></div><div class="skeleton"></div>';
  try{
    const r=await fetch("/api/email/quick-edit",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({current_email:email,instruction:ins})});
    const d=await r.json();
    if(d.error)showErr(emArea,d.error);else{email=d.email;emArea.textContent=email;qeIn.value=""}
  }catch(e){showErr(emArea,e.message)}
  qeBtn.classList.remove("loading");
}

// ═══ Chat ═══
sendBtn.onclick=sendChat;chatIn.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey)sendChat()};
document.querySelectorAll(".pill").forEach(b=>b.onclick=()=>{chatIn.value=b.dataset.q;sendChat()});

// Voice dictation
micBtn.onclick=()=>{
  if(!SR)return;
  if(isDictating){stopDict();return}
  dict=new SR();dict.continuous=false;dict.interimResults=true;dict.lang="en-US";
  const before=chatIn.value;
  dict.onresult=e=>{let r="";for(let i=0;i<e.results.length;i++)r=e.results[i][0].transcript;chatIn.value=before+(before?" ":"")+r};
  dict.onend=()=>stopDict();dict.onerror=()=>stopDict();
  dict.start();isDictating=true;micBtn.classList.add("mic-on");micDot.classList.remove("hidden");
};
function stopDict(){if(dict)try{dict.stop()}catch(e){}isDictating=false;micBtn.classList.remove("mic-on");micDot.classList.add("hidden")}

async function sendChat(){
  const q=chatIn.value.trim();if(!q||!transcript)return;
  if(isDictating)stopDict();
  addBub("user",q);chatIn.value="";sendBtn.disabled=true;
  const tid=addDots();
  try{
    const r=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q,transcript,history:chatHist})});
    const d=await r.json();rmDots(tid);
    if(d.error)addBub("err",d.error);
    else{addBub("ai",d.answer);chatHist.push({role:"user",content:q},{role:"assistant",content:d.answer})}
  }catch(e){rmDots(tid);addBub("err",e.message)}
  sendBtn.disabled=false;chatIn.focus();
}

function addBub(type,text){
  const row=document.createElement("div");
  if(type==="user"){row.className="brow brow-user";row.innerHTML=`<div class="bub bub-user">${esc(text)}</div>`}
  else if(type==="ai"){row.className="brow brow-ai";row.innerHTML=`<div class="bub bub-ai">${esc(text).replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/\n/g,"<br>")}</div>`}
  else{row.className="brow brow-ai";row.innerHTML=`<div class="bub bub-err">${esc(text)}</div>`}
  chatThread.appendChild(row);row.scrollIntoView({behavior:"smooth",block:"nearest"});
}
let dc=0;
function addDots(){const id=`d${++dc}`;const r=document.createElement("div");r.className="brow brow-ai";r.id=id;r.innerHTML='<div class="bub bub-ai"><div class="dots"><span></span><span></span><span></span></div></div>';chatThread.appendChild(r);r.scrollIntoView({behavior:"smooth",block:"nearest"});return id}
function rmDots(id){const e=document.getElementById(id);if(e)e.remove()}
