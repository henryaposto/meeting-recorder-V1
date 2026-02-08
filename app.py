import os
import time
import tempfile
import traceback
import json as json_mod
import sqlite3
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, g
from dotenv import load_dotenv
import anthropic
from openai import OpenAI
from pydub import AudioSegment

load_dotenv()

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"

DATABASE = "recordings.db"
active_recording_id = None

MAX_CHARS_PER_CHUNK = 400_000
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2


# ─── Database ───

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recordings (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'Untitled Recording',
            transcript TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            email TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            duration INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def update_active_recording(field, value):
    global active_recording_id
    if not active_recording_id:
        return
    db = get_db()
    db.execute(f"UPDATE recordings SET {field} = ? WHERE id = ?", (value, active_recording_id))
    db.commit()


init_db()


def get_claude():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def call_claude(fn):
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except anthropic.RateLimitError as e:
            last_error = e
            time.sleep(RETRY_DELAY * (attempt + 1))
        except anthropic.APITimeoutError as e:
            last_error = e
            time.sleep(RETRY_DELAY * (attempt + 1))
        except anthropic.BadRequestError:
            raise
        except anthropic.APIError as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)
            else:
                raise
    raise last_error


def chunk_transcript(transcript):
    if len(transcript) <= MAX_CHARS_PER_CHUNK:
        return [transcript]
    chunks, current, current_len = [], [], 0
    for word in transcript.split():
        wl = len(word) + 1
        if current_len + wl > MAX_CHARS_PER_CHUNK:
            chunks.append(" ".join(current))
            current, current_len = [word], wl
        else:
            current.append(word)
            current_len += wl
    if current:
        chunks.append(" ".join(current))
    return chunks


def handle_api_error(e):
    error_msg = str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return jsonify({"error": "Invalid API key. Check ANTHROPIC_API_KEY in .env"}), 401
    if isinstance(e, anthropic.BadRequestError):
        if "credit balance" in error_msg.lower():
            return jsonify({"error": "No API credits. Add credits at console.anthropic.com -> Plans & Billing"}), 402
        return jsonify({"error": f"API error: {error_msg}"}), 400
    if isinstance(e, anthropic.RateLimitError):
        return jsonify({"error": "Rate limited. Wait a moment and try again."}), 429
    if isinstance(e, anthropic.APITimeoutError):
        return jsonify({"error": "API timed out after retries. Try again."}), 504
    app.logger.error(f"Error: {traceback.format_exc()}")
    return jsonify({"error": f"Unexpected error: {error_msg}"}), 500


# ─── Transcription ───

WHISPER_MAX_BYTES = 24 * 1024 * 1024  # 24MB (Whisper limit is 25MB)
CHUNK_DURATION_MS = 10 * 60 * 1000    # 10 minutes per chunk


def _transcribe_file(path):
    """Send a single audio file to Whisper and return trimmed text."""
    with open(path, "rb") as f:
        result = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="text",
        )
    return result.strip() if isinstance(result, str) else result.text.strip()


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    tmp_webm = None
    tmp_files = []
    try:
        # Save uploaded webm
        tmp_webm = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
        audio_file.save(tmp_webm)
        tmp_webm.close()

        # Convert to mp3 (much smaller than webm)
        audio = AudioSegment.from_file(tmp_webm.name, format="webm")
        tmp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_mp3.close()
        tmp_files.append(tmp_mp3.name)
        audio.export(tmp_mp3.name, format="mp3", bitrate="64k")

        mp3_size = os.path.getsize(tmp_mp3.name)
        app.logger.info(f"Audio: {len(audio)/1000:.0f}s, mp3={mp3_size/1024/1024:.1f}MB")

        if mp3_size <= WHISPER_MAX_BYTES:
            text = _transcribe_file(tmp_mp3.name)
        else:
            # Split into ~10 minute chunks
            chunk_paths = []
            for i in range(0, len(audio), CHUNK_DURATION_MS):
                chunk = audio[i:i + CHUNK_DURATION_MS]
                cf = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                cf.close()
                tmp_files.append(cf.name)
                chunk.export(cf.name, format="mp3", bitrate="64k")
                chunk_paths.append(cf.name)

            app.logger.info(f"Split into {len(chunk_paths)} chunks")
            transcripts = [_transcribe_file(p) for p in chunk_paths]
            text = " ".join(t for t in transcripts if t)

        return jsonify({"transcript": text, "length": len(text)})
    except Exception as e:
        app.logger.error(f"Transcription error: {traceback.format_exc()}")
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500
    finally:
        if tmp_webm:
            try:
                os.unlink(tmp_webm.name)
            except OSError:
                pass
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass


# ─── Routes ───

@app.route("/")
def index():
    return render_template("index.html")


# ─── Recording CRUD ───

@app.route("/api/save_recording", methods=["POST"])
def save_recording():
    global active_recording_id
    data = request.json
    rec_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    db = get_db()
    db.execute(
        "INSERT INTO recordings (id, name, transcript, created_at, duration) VALUES (?, ?, ?, ?, ?)",
        (rec_id, "Untitled Recording", data.get("transcript", ""), now, data.get("duration", 0))
    )
    db.commit()
    active_recording_id = rec_id
    return jsonify({"id": rec_id, "name": "Untitled Recording", "created_at": now})


@app.route("/api/recordings", methods=["GET"])
def list_recordings():
    db = get_db()
    rows = db.execute(
        "SELECT id, name, created_at, duration FROM recordings ORDER BY created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/recording/<rec_id>", methods=["GET"])
def get_recording(rec_id):
    global active_recording_id
    db = get_db()
    row = db.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
    if not row:
        return jsonify({"error": "Recording not found"}), 404
    active_recording_id = rec_id
    return jsonify(dict(row))


@app.route("/api/recording/<rec_id>", methods=["DELETE"])
def delete_recording(rec_id):
    global active_recording_id
    db = get_db()
    db.execute("DELETE FROM recordings WHERE id = ?", (rec_id,))
    db.commit()
    if active_recording_id == rec_id:
        active_recording_id = None
    return jsonify({"ok": True})


@app.route("/api/generate_name", methods=["POST"])
def generate_name():
    data = request.json
    rec_id = data.get("id")
    text = data.get("transcript", "")[:2000]
    if not rec_id or not text:
        return jsonify({"error": "id and transcript required"}), 400

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=60,
            messages=[{"role": "user", "content": f"Generate a short descriptive name (max 30 characters) for this meeting recording based on the transcript. Return ONLY the name, nothing else. No quotes.\n\nTranscript:\n{text}"}],
        ))
        name = msg.content[0].text.strip()[:30]
        db = get_db()
        db.execute("UPDATE recordings SET name = ? WHERE id = ?", (name, rec_id))
        db.commit()
        return jsonify({"name": name})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/rename_recording", methods=["POST"])
def rename_recording():
    data = request.json
    rec_id = data.get("id")
    new_name = data.get("name", "").strip()
    if not rec_id or not new_name:
        return jsonify({"error": "id and name required"}), 400
    new_name = new_name[:60]
    db = get_db()
    db.execute("UPDATE recordings SET name = ? WHERE id = ?", (new_name, rec_id))
    db.commit()
    return jsonify({"ok": True, "name": new_name})


# ─── Analyze (intelligence layer) ───

@app.route("/api/analyze", methods=["POST"])
def analyze():
    transcript = request.json.get("transcript", "")[:4000]
    defaults = {
        "meeting_type": "sales",
        "email_default": "customer",
        "pills": ["What are the key next steps?", "Any risks to flag?", "Who owns what?"],
        "alerts": []
    }
    if not transcript:
        return jsonify(defaults)

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=400,
            messages=[{"role": "user", "content": f"""Analyze this meeting transcript. Return ONLY valid JSON, no other text.

{{
  "meeting_type": "sales | internal | learning | one_on_one",
  "email_default": "customer | team",
  "pills": ["question 1?", "question 2?", "question 3?"],
  "alerts": [
    {{"type": "urgent", "text": "brief alert"}},
    {{"type": "positive", "text": "brief alert"}}
  ]
}}

Rules:
- meeting_type: "sales" if external customer/prospect, "internal" if team meeting, "learning" if training/lecture, "one_on_one" if 1:1
- email_default: "customer" if external people present, "team" if internal only
- pills: 3 most relevant follow-up questions (short, 3-6 words each). Sales calls → deal/budget/stakeholder questions. Internal → decisions/ownership/blockers. Learning → concepts/assignments.
- alerts: 1-3 key signals from the call. Types:
  - "urgent": tight deadlines, time-sensitive items
  - "positive": buying signals, enthusiasm, agreement
  - "risk": missing stakeholders, vague commitments, objections
  - "insight": competitive intel, hidden concerns, opportunities
- Keep alert text under 12 words each
- Return ONLY valid JSON

Transcript:
{transcript}"""}],
        ))
        result = json_mod.loads(msg.content[0].text)
        return jsonify(result)
    except Exception:
        return jsonify(defaults)


# ─── Summarize ───

@app.route("/api/summarize", methods=["POST"])
def summarize():
    transcript = request.json.get("transcript", "")
    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    app.logger.info(f"Summarize: {len(transcript)} chars")

    prompt = f"""Generate a meeting summary that tells the story of what happened.

Think about who will read this:
- The AE who was there (needs a refresh and action items)
- Their manager (needs to understand deal health and progression)
- Team members who weren't there (need context about what happened)
- Future you in 2 weeks (needs to remember what this was about)

FORMAT:

[SYNOPSIS - 2-3 sentences, no header]
Tell the story: Who met, why they met, what was discussed, what came out of it.
Write like you're telling a colleague what happened:
- "Had a discovery call with Jon (VP Ops) at RCA Partners about their infrastructure challenges. They're experiencing 3-4 week deployment delays that are costing them customer deals. They have $500K approved for Q1 and are looking for a solution before quarter end."
- "Team meeting to discuss MR rate tracking across departments at GitLab. The current taxonomy is confusing because we're mixing overall company rates with department-specific rates."
- "Follow-up with Sarah about the security review. She's gotten approval from legal but needs our compliance docs before the next board meeting."

Include: WHO (names/roles), WHAT (topic), WHY (situation/problem), OUTCOME (decision/next step)

**Key Takeaways:**
- [Insight - be specific, 8-12 words, complete thought]
- [Insight - include numbers, names, context]
- [Insight - explain why it matters]
(3-5 bullets based on meeting importance)

Write takeaways that are:
- Specific: "$500K Q1 budget already approved, but timeline is tight (ends March 31)" NOT "Budget approved for Q1"
- Contextual: "CFO Sarah has final approval but hasn't been involved yet - need to loop her in" NOT "CFO needs loop-in"
- Useful: information that affects decisions, buying signals, risks, specific details

Prioritize: 1) Decision-affecting info 2) Specific details (numbers, names, dates) 3) Concerns or blockers 4) Positive signals 5) Competitive intel

**Next Steps:**
- [Single clear line: what happens next, 8-12 words]

**Action Items:**
- [Name]: [Specific action, 6-10 words]
- [Name]: [Specific action, 6-10 words]

**My Tasks:**
1. [Specific task before next meeting, 4-8 words]
2. [Another task if needed]
3. [Another task if needed]

EXTRACTION RULES:
- Use exact names, roles/titles, companies from the transcript
- Include specific numbers (budget amounts, timelines, metrics, team sizes)
- Capture pain points WITH their business impact
- Note decision makers and approval processes
- Flag buying signals, concerns, and competitor mentions
- Include next meeting date/time if mentioned
- Extract commitments made by both sides
- "Henry" is the user — extract what Henry committed to do
- If no tasks for Henry, omit My Tasks section entirely
- Do not fabricate — only include what's in the transcript

CLARITY OVER BREVITY:
- A 10-word clear sentence beats a 5-word cryptic fragment
- Someone reading this should understand what happened without being in the meeting
- Include context that makes each point useful and actionable
- Write like you're telling a colleague what happened, not filling out a form

Transcript:
{transcript}"""

    try:
        client = get_claude()
        chunks = chunk_transcript(transcript)

        if len(chunks) == 1:
            msg = call_claude(lambda: client.messages.create(
                model="claude-sonnet-4-5-20250929", max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ))
            summary = msg.content[0].text
        else:
            partials = []
            for i, chunk in enumerate(chunks):
                chunk_prompt = f"""Extract key information from part {i+1}/{len(chunks)} of a meeting transcript.

For this segment, identify:
- Who was in the meeting (names, roles, companies)
- Key insights or decisions (≤5 words each)
- Action items with owners and deadlines
- What Henry committed to do
- Any deadlines or next meeting dates
- What happens next (progression/flow of the deal)

Be specific — use exact names, numbers, dates. No filler.

Transcript segment:
{chunk}"""
                msg = call_claude(lambda p=chunk_prompt: client.messages.create(
                    model="claude-sonnet-4-5-20250929", max_tokens=1024,
                    messages=[{"role": "user", "content": p}],
                ))
                partials.append(msg.content[0].text)

            merged = "\n---\n".join([f"Part {i+1}:\n{s}" for i, s in enumerate(partials)])
            msg = call_claude(lambda: client.messages.create(
                model="claude-sonnet-4-5-20250929", max_tokens=2048,
                messages=[{"role": "user", "content": f"""Merge these partial meeting extracts into one summary that tells the story of what happened.

FORMAT:

[SYNOPSIS - 2-3 sentences, no header]
Tell the story: Who met, why they met, what was discussed, what came out of it.
Write like telling a colleague: "Had a call with X about Y. They're dealing with Z. We discussed..."
Include: WHO (names/roles), WHAT (topic), WHY (situation/problem), OUTCOME (decision/next step)

**Key Takeaways:**
- [3-5 bullets, 8-12 words each, specific and contextual]
- Include numbers, names, dates — be specific not vague
- Each bullet should be a complete thought someone can understand without context

**Next Steps:**
- [Single clear line: what happens next, 8-12 words]

**Action Items:**
- [Name]: [Specific action, 6-10 words]

**My Tasks:**
1. [Henry's tasks only, 4-8 words each, numbered 1-3]

Rules:
- Deduplicate across parts
- "Henry" is the user — extract what Henry committed to do
- If no tasks for Henry, omit My Tasks section
- Use exact names, numbers, dates from the extracts
- Prioritize clarity over brevity — someone not in the meeting should understand what happened

Partial extracts:
{merged}"""}],
            ))
            summary = msg.content[0].text

        update_active_recording("summary", summary)
        return jsonify({"summary": summary})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/email", methods=["POST"])
def generate_email():
    transcript = request.json.get("transcript", "")
    summary = request.json.get("summary", "")
    email_type = request.json.get("email_type", "customer")
    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    if email_type == "team_update":
        prompt = f"""Write an INTERNAL team update email summarizing this customer call. NOT sent to the customer — sent to your internal team.

EXACT FORMAT:

Subject: [Customer Company] - Call recap

Team -

Key points and action items from today's call:

**Discussion:**
- [Key point 1]
- [Key point 2]
- [Key point 3]

**Action Items:**
- [Name]: [action]
- [Name]: 1.) [action] 2.) [action]
- [Name]: [action]

Next meeting: [Date/Time or "TBD"]

Henry

RULES:
- Discussion: exactly 3 bullets, concise, prioritize by business impact
- Action items: max 2 per person on ONE line. 1 action: "Name: [action]". 2 actions: "Name: 1.) [action] 2.) [action]"
- Extract customer company from transcript for subject line
- Listen for commitment language: "I'll...", "I'm going to...", "Can you..."
- End with "Next meeting: [Date/Time]" or "Next meeting: TBD"
- Sign off with just "Henry" — no "Best,"

Meeting summary:
{summary}

Meeting transcript:
{transcript[:100000]}"""

        try:
            client = get_claude()
            msg = call_claude(lambda: client.messages.create(
                model="claude-sonnet-4-5-20250929", max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ))
            email_text = msg.content[0].text
            update_active_recording("email", email_text)
            return jsonify({"email": email_text})
        except Exception as e:
            return handle_api_error(e)

    if email_type == "sales_followup":
        prompt = f"""You are a top-performing enterprise sales rep writing a brief follow-up email based on the meeting transcript. Analyze the transcript and pick ONE of these 3 styles:

STYLE 1 - CASUAL (use when: next meeting already scheduled, no urgent actions, early stage/discovery):
Subject: Good connecting
Hey [name] - Good connecting today. Nice learning more about you and [Company].
Speak next week. Have a good weekend. Enjoy.
Best,
[Your name]
(25-35 words, NO asks, NO action items)

STYLE 2 - INFORMATIONAL (use when: you have an action to do, but no ask for them):
Subject: Good connecting
Hi [name].
Good connecting today. I'll [your specific action from transcript] for our call [next meeting time from transcript] and come with [deliverable].
Have a good week.
Best,
[Your name]
(20-30 words)

STYLE 3 - ACTION (use when: deal advancing, need them to do something, negotiation/pricing discussed):
Subject: [2 words max, topic-specific, e.g. "Quick question", "Pricing update", "Following up"]
Hi [name] - Good connecting today. Speak with you next week.
I'll [your action] on [day/timeframe from transcript].
Do you think you can [their action] before [meeting/deadline from transcript]?
Best,
[Your name]
(40-60 words)

CRITICAL STYLE RULES - FOLLOW EXACTLY:
- Extract the prospect's name from the transcript. If not found, use [name] as placeholder.
- Sign off with [Your name] - the user will fill in their own name.
- Peer-to-peer tone, NOT vendor-to-customer
- Confident, assumes deal is moving forward
- Write like texting a colleague
- NEVER use: "Let me know if you have questions", "Looking forward to", "Hope this finds you well", "circle back", "touch base", "reach out"
- NEVER use corporate jargon
- Extract specific names, dates, actions, stakeholders from the transcript
- Subject lines for casual/informational: "Good connecting" or "[Your Company] / [Prospect Company]"
- Subject lines for action: 2 words maximum, topic-focused

Meeting summary:
{summary}

Meeting transcript:
{transcript[:200000]}"""

        try:
            client = get_claude()
            msg = call_claude(lambda: client.messages.create(
                model="claude-sonnet-4-5-20250929", max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ))
            email_text = msg.content[0].text
            update_active_recording("email", email_text)
            return jsonify({"email": email_text})
        except Exception as e:
            return handle_api_error(e)

    prompt = f"""You are a top-performing enterprise sales rep writing a brief follow-up email. Your style is confident, peer-to-peer, and action-oriented. You never sound desperate or overly formal. You sound like a colleague, not a vendor.

STEP 1 — CHOOSE THE RIGHT STYLE:

Read the transcript and summary. Pick ONE style based on context:

**STYLE A — CASUAL** (use when):
- Pure relationship building
- No action items at all
- First meeting or early discovery call focused on rapport

**STYLE B — INFORMATIONAL** (use when):
- You're doing something for the next meeting (pulling numbers, sending docs, checking internally)
- No ask needed from them
- Next meeting is already scheduled

**STYLE C — ACTION** (use when):
- Need them to complete something
- Time-sensitive or advancing the deal
- Both sides have action items

If unsure between B and C, pick B if only you have action, pick C if they also need to do something.

═══════════════════════════════════════════════════════════════════

STYLE A — CASUAL / RELATIONSHIP EMAIL:

Structure:
- Subject: "Good connecting" (DEFAULT) or "Comcast / [Prospect Company]" (alternative)
- Greeting: "Hey [Name] -" (use "Hey", not "Hi")
- Line 1: "Good connecting today." or "Great call this morning."
- Line 2: Personal touch — "Nice learning more about you and [Company]."
- Line 3: Future reference — "Speak next week." or "Talk [day]."
- Line 4: Friendly close — "Have a good weekend." or "Enjoy." or "Have a great week."
- Sign-off: "Best,\\nHenry"

Length: 25-35 words. NO asks, NO action items, NO deliverables.

Subject line options:
- "Good connecting" (DEFAULT — use this first)
- "Comcast / [Prospect Company]" (alternative — extract prospect company name from transcript)

Example:
Subject: Good connecting

Hey Sarah - Good connecting today. Nice learning more about you and Acme Corp.

Speak next week. Have a good weekend. Enjoy.

Best,
Henry

═══════════════════════════════════════════════════════════════════

STYLE B — INFORMATIONAL EMAIL (you have action, no ask for them):

Structure:
- Subject: "Good connecting"
- Greeting: "Hi [Name]." (use "Hi" with a PERIOD, not a dash)
- Combined acknowledgment + your action: "Good connecting today. I'll [action] for our call [when]."
- Friendly close: "Have a good week." or "Have a great weekend."
- Sign-off: "Best,\\nHenry"

Length: 20-30 words. State what YOU'RE doing. NO asks, NO questions for them.

Example:
Subject: Good connecting

Hi Jon.

Good connecting today. I'll grab updated numbers from finance for our call next week and come with breakdown.

Have a good week.

Best,
Henry

═══════════════════════════════════════════════════════════════════

STYLE C — ACTION-ORIENTED EMAIL:

Structure:
- Subject: MAXIMUM 2 WORDS. Topic-focused and specific. (e.g., "Quick question", "Pricing update", "Q1 timeline", "Integration question")
- Greeting: "Hi [Name] -" (use "Hi" with dash)
- Line 1: Brief call acknowledgment, 5-8 words (e.g., "Good connecting today. Speak with you next week.")
- Line 2: YOUR next action with timeline — start with "I'll" (e.g., "I'll speak with finance on Monday/Tuesday to see if they can hold pricing till the end of the month.")
- Line 3: THEIR action as a question — use "Do you think you can..." or "Can you..." with a deadline (e.g., "Do you think you can speak with your CFO about the project before our meeting Thursday?")
- Sign-off: "Best,\\nHenry"

Length: 40-60 words. Every line drives the deal forward.

SUBJECT LINE RULES — 2 WORDS MAX:
Good: "Quick question" | "Pricing update" | "Following up" | "Next steps" | "Q1 timeline" | "Integration question"
Bad: "A Quick moving deal" | "Following up - Q1 timeline" | "Next steps for Acme" | "Re: Integration requirements"

Example:
Subject: Pricing hold

Hi Jon - Good connecting today. Speak with you next week.

I'll speak with finance on Monday/Tuesday to see if they can hold pricing till the end of the month.

Do you think you can speak with your CFO about the project before our meeting Thursday?

Best,
Henry

═══════════════════════════════════════════════════════════════════

EXTRACT FROM THE TRANSCRIPT:
- Prospect's first name
- Their company name
- Next meeting date/time if mentioned
- Commitments YOU made during the call
- Things THEY need to do or people they need to loop in
- Timelines, deadlines, milestones
- Internal stakeholders mentioned (CFO, VP, legal, procurement)
- Day of week (for "Have a good weekend" vs "Have a great week")

UNIVERSAL RULES (all styles):
- Always sign as "Henry"
- Greeting punctuation: Style A uses "Hey [Name] -" (dash), Style B uses "Hi [Name]." (period), Style C uses "Hi [Name] -" (dash). Never use a comma.
- NO "let me know if you have questions"
- NO "looking forward to..."
- NO "hope this finds you well"
- NO "per our conversation" or "as discussed"
- NO exclamation marks
- NO bullet points in the email
- Assume the deal is moving — confidence, not hope
- If you can't find a name, use "[Name]"

Meeting summary:
{summary}

Meeting transcript:
{transcript[:200000]}"""

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ))
        email = msg.content[0].text
        update_active_recording("email", email)
        return jsonify({"email": email})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/email/regenerate", methods=["POST"])
def regenerate_email():
    transcript = request.json.get("transcript", "")
    summary = request.json.get("summary", "")
    current_email = request.json.get("current_email", "")
    style = request.json.get("style", "shorter")

    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    base_rules = """UNIVERSAL RULES:
- Always sign as "Henry"
- Greeting punctuation: casual uses "Hey [Name] -" (dash), informational uses "Hi [Name]." (period), action uses "Hi [Name] -" (dash). Never comma.
- NO "let me know if you have questions", "looking forward to...", "hope this finds you well", "per our conversation", "as discussed"
- NO exclamation marks, NO bullet points in the email body
- Sound like a real human — confident, peer-to-peer, not a vendor
- Subject line: casual/informational emails use 'Good connecting' (default) or 'Comcast / [Prospect Company]' (alternative); action emails use a 2-word topic (e.g., 'Quick question', 'Pricing update', 'Following up', 'Next steps', 'Q1 timeline')"""

    if style == "shorter":
        instruction = f"""Make this email significantly shorter. Cut word count by ~40-50%.

DETECT THE EMAIL TYPE and condense accordingly:

═══ IF THIS IS A TEAM UPDATE EMAIL (starts with "Team -" or has **Discussion:**/**Action Items:**) ═══

SHORTER VERSION RULES:
- Subject: keep same [Company] - Call recap
- Opening: "Team -" (no extra intro)
- Discussion: ONLY 2 bullets (down from 3), tightest possible wording
- Action Items: Max 1 action per person (prioritize most critical)
- Next meeting: One line only, simplified (e.g. "Thursday 2pm" not "Thursday, February 13 at 2pm EST")
- Sign: just "Henry"
- WORD COUNT TARGET: 40-50 words total (excluding subject)

Condensing techniques for team updates:
- Remove any explanatory text or context
- Use shorter phrasing: "Get pricing Monday" not "Get pricing from finance by Monday"
- Combine related points if possible
- Eliminate redundancy between discussion and action items
- Keep only mission-critical information
- Consolidate actions: "Send pricing and docs" not "1.) Get pricing 2.) Send docs"

CRITICAL: Even when shortening, must still have 2 discussion bullets minimum, at least 1 action item, and next meeting info.

═══ IF THIS IS A CUSTOMER EMAIL ═══

SHORTER VERSION RULES — apply based on detected style:

CASUAL STYLE (shorter): 15-20 words
- Remove "Nice learning more about..." line
- Remove extra pleasantries
- Keep just: greeting + "Good connecting" + "Speak next week." + name

INFORMATIONAL STYLE (shorter): 20-25 words
- Remove "Good connecting" opener
- Combine your actions into one sentence
- Remove "Have a good week" closer

ACTION STYLE (shorter): 25-35 words
- Remove "Good connecting today. Speak with you next week."
- Combine "I'll [action]" into shortest possible form
- Make ask direct: "Can you..." not "Do you think you can..."
- Remove "Best," — just "Henry"

Condensing techniques for customer emails:
- Remove pleasantries: "Good connecting today. Nice learning more about you." → [Cut entirely]
- Combine sentences: "I'll get pricing. I'll send it Monday." → "I'll send pricing Monday."
- Direct language: "Do you think you could possibly check with..." → "Can you check with..."
- Eliminate redundancy: "speak with finance on Monday/Tuesday to see if they can hold pricing" → "get pricing Monday"
- Shorter phrasing: "for our call next week" → "for Thursday"
- Remove transitional phrases: "Speak with you next week." → [Cut if next meeting already stated]
- Cut sign-off padding: "Best, Henry" → "Henry"
- Remove qualifiers: "I think we should probably..." → "We should..."

UNIVERSAL RULES:
- Do NOT change the email type (don't turn a team update into a customer email)
- Keep all names and specific details (dates, times, companies)
- Make it punchier and more scannable
- Remove filler words and redundant phrases

{base_rules}

Current email:
{current_email}"""
    elif style == "longer":
        instruction = f"""Expand this email to 80-100 words while keeping the same structure and tone.

Add:
- A second specific reference from the meeting (a different topic, pain point, or detail)
- More context on what you're doing and why it matters for them
- Keep the same "I'll [action]" + "Can you [ask]?" pattern
- Same greeting style, same sign-off

Longer doesn't mean more formal. Keep it peer-to-peer.

{base_rules}

Meeting summary:
{summary}

Current email:
{current_email}"""
    elif style == "casual":
        instruction = f"""Rewrite this as a casual relationship email. Think: you just had a great first call and the next meeting is booked. No asks needed.

Structure:
- Subject: "Good connecting" (default) or "Comcast / [Prospect Company]" (alternative)
- "Hey [Name] -" (use Hey, not Hi)
- "Good connecting today." or similar
- Personal touch referencing them or their company
- "Speak next week." / "Talk [day]."
- "Have a good weekend." / "Enjoy." / "Have a great week."
- "Best,\\nHenry"

25-35 words. NO action items, NO deliverables, NO questions. Pure warmth.

{base_rules}

Current email:
{current_email}"""
    elif style == "professional":
        instruction = f"""Rewrite this email for a C-suite audience. ~50 words.

Keep the same structure but polish the language:
- "Hi [Name] -" (Hi, not Hey — slightly more formal)
- Crisp acknowledgment
- Your action should reference business impact, not just tasks
- Their ask should reference a decision, not busy work
- Tone: peer-to-peer advisor, not deferential vendor
- Sign: "Best,\\nHenry"

{base_rules}

Current email:
{current_email}"""
    elif style == "urgent":
        instruction = f"""Rewrite this email with urgency anchored to something REAL — a deadline from the meeting, a competitive move, a quarterly goal, pricing expiring.

~50 words. Same structure:
- "Hi [Name] -"
- Acknowledge the call briefly
- Your action with an accelerated timeline
- Their action with a tighter deadline: "before EOD Thursday", "by Friday", "before Q2 planning kicks off"
- Sign: "Best,\\nHenry"

Don't manufacture fake urgency — it kills trust. Use real signals from the meeting.

{base_rules}

Current email:
{current_email}"""
    elif style == "team_update":
        instruction = f"""Write an INTERNAL team update email summarizing this customer call. This is NOT sent to the customer — it's sent to your internal team.

EXACT FORMAT — follow precisely:

Subject: [Customer Company] - Call recap

Team -

Key points and action items from today's call:

**Discussion:**
- [Key point 1]
- [Key point 2]
- [Key point 3]

**Action Items:**
- [Name]: [action]
- [Name]: 1.) [action] 2.) [action]
- [Name]: [action]

Next meeting: [Date/Time or "TBD"]

Henry

DISCUSSION RULES:
- Exactly 3 bullets maximum
- Extract the 3 most important topics discussed
- Keep each bullet concise (one line)
- Prioritize by business impact

ACTION ITEMS RULES:
- Maximum 2 action items per person
- All actions for one person go on ONE line
- If person has 1 action: "Name: [action]"
- If person has 2 actions: "Name: 1.) [action] 2.) [action]"
- Never exceed 2 actions per person

WHO GETS ACTION ITEMS — listen for commitment language:
- "I'll send you...", "I'm going to check...", "I will follow up..."
- "Let me get that to you...", "Can you...", "Please..."
- Include Henry, customer participants, and any team members who committed to tasks

FORMATTING:
- Subject line: extract customer company from transcript
- Opening: always "Key points and action items from today's call:"
- Use "- " for bullets in both sections
- End with "Next meeting: [Date/Time]" or "Next meeting: TBD"
- Sign off with just "Henry" — no "Best,"

Meeting summary:
{summary}

Meeting transcript:
{transcript[:100000]}

Current email (ignore this — write a fresh team update):
{current_email}"""
    elif style == "retry":
        instruction = f"""Write a completely DIFFERENT follow-up email. Different subject, different angle, different ask. 40-60 words.

First, decide the style:
- If the current email is action-oriented, try a casual/relationship version (or vice versa)
- If it referenced one topic, pick a DIFFERENT topic from the meeting
- Different subject line, different opening, different ask (or no ask if going casual)

Keep the same structural patterns: greeting with dash, sign as Henry, no corporate filler.

{base_rules}

Meeting summary:
{summary}

Meeting transcript:
{transcript[:100000]}

Current email (write something DIFFERENT):
{current_email}"""
    else:
        instruction = f"""Rewrite this email. 40-60 words. Same structure: greeting with dash, your action with "I'll", their action as a question, sign as Henry.

{base_rules}

Current email:
{current_email}"""

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": instruction}],
        ))
        email = msg.content[0].text
        update_active_recording("email", email)
        return jsonify({"email": email})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/email/quick-edit", methods=["POST"])
def quick_edit_email():
    current_email = request.json.get("current_email", "")
    edit_instruction = request.json.get("instruction", "")

    if not current_email or not edit_instruction:
        return jsonify({"error": "Email and instruction required"}), 400

    prompt = f"""Apply this edit to the email below. Return ONLY the edited email — no commentary, no explanation, no "Here's the updated version".

Preserve the email's tone and specificity. Keep the subject line format (Subject: ...) followed by the body. If the edit instruction conflicts with good email practice (e.g., makes it generic or adds filler), apply the edit but maintain quality.

Edit instruction: {edit_instruction}

Current email:
{current_email}"""

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ))
        email = msg.content[0].text
        update_active_recording("email", email)
        return jsonify({"email": email})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/chat", methods=["POST"])
def chat():
    question = request.json.get("question", "")
    transcript = request.json.get("transcript", "")
    chat_history = request.json.get("history", [])
    summary = request.json.get("summary", "")

    if not question:
        return jsonify({"error": "No question provided"}), 400

    system_prompt = f"""You are a senior sales strategist and deal desk analyst embedded in the user's workflow. You've closed 8-figure deals and coached hundreds of AEs. You think in terms of deal mechanics, not just information retrieval.

YOUR ROLE:
You have access to a meeting transcript and summary. When the user asks a question, don't just search the transcript — interpret it through a sales lens. Connect dots. Spot patterns. Flag risks the rep might miss.

RESPONSE FRAMEWORK:
1. **Answer first** — bottom-line-up-front, always. Lead with the answer, then support it.
2. **2-4 sentences max** unless the user explicitly asks for detail or the question requires a list.
3. **Be specific** — use exact names, numbers, quotes, and timestamps from the transcript. Never say "they discussed pricing" when you can say "**Sarah** pushed back on the $45K/year tier, asking about a pilot option."
4. **Sales-aware interpretation** — when asked "what did they think about pricing?", don't just report what was said. Interpret: Was it a real objection or a negotiation tactic? Is there budget flexibility? What's the underlying concern?

SALES INTELLIGENCE CAPABILITIES:
- **Deal qualification**: Assess BANT/MEDDPICC criteria based on what was discussed
- **Stakeholder mapping**: Identify champions, economic buyers, technical evaluators, blockers — and their dynamics
- **Competitive intel**: Surface any competitor mentions, "build vs buy" discussions, or comparison signals
- **Objection analysis**: Distinguish real objections (budget not approved) from negotiation tactics (asking for discount) from stalls ("let us think about it")
- **Next-step coaching**: When asked "what should I do next?", give specific, tactical advice — not generic "follow up" suggestions
- **Risk flagging**: Proactively note if the deal shows warning signs (single-threaded, no timeline, champion going quiet)

FORMATTING:
- Use bullet points (•) for lists, never dashes
- Bold (**text**) key terms, names, numbers, and deal-critical info
- Line breaks between distinct points
- If something wasn't discussed in the meeting, say so in one sentence — never fabricate or speculate beyond what the transcript supports
- When quoting the transcript, use exact words in quotation marks

Meeting Transcript:
{transcript[:MAX_CHARS_PER_CHUNK]}

Meeting Summary:
{summary or 'Not generated yet.'}"""

    messages = [{"role": m["role"], "content": m["content"]} for m in chat_history]
    messages.append({"role": "user", "content": question})

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            system=system_prompt, messages=messages,
        ))
        return jsonify({"answer": msg.content[0].text})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/stats", methods=["POST"])
def stats():
    transcript = request.json.get("transcript", "")
    char_count = len(transcript)
    chunks = len(chunk_transcript(transcript))
    return jsonify({
        "chars": char_count,
        "estimated_tokens": char_count // 4,
        "chunks": chunks,
        "estimated_seconds": chunks * 10,
    })


if __name__ == "__main__":
    os.makedirs("uploads", exist_ok=True)
    init_db()
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
