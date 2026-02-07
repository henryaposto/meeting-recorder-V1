import os
import time
import traceback
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"

session_data = {"transcript": "", "summary": "", "email": ""}

MAX_CHARS_PER_CHUNK = 400_000
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2


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


# ─── Routes ───

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/summarize", methods=["POST"])
def summarize():
    transcript = request.json.get("transcript", "")
    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    app.logger.info(f"Summarize: {len(transcript)} chars")

    prompt = f"""You are an expert meeting analyst. Summarize this transcript into a brief, scannable outline.

RULES:
- Maximum 8-10 bullet points total across all sections
- Include specific names: people, companies, tools, technologies mentioned
- Remove all filler words and generic statements
- Every bullet must contain concrete, specific information
- Be direct and concise — no introductions or conclusions

FORMAT (use exactly this):
## Summary

### Key Topics
- [specific topic with names/details]
- [specific topic with names/details]
- [specific topic with names/details]

### Decisions
- [specific decision made, with who decided if mentioned]
- [specific decision made]

### Action Items
- [ ] [specific task] — [owner if mentioned]
- [ ] [specific task] — [owner if mentioned]

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
                msg = call_claude(lambda c=chunk, idx=i: client.messages.create(
                    model="claude-sonnet-4-5-20250929", max_tokens=1024,
                    messages=[{"role": "user", "content": f"Summarize part {idx+1}/{len(chunks)} of a meeting. Include specific names, decisions, action items. Be concise.\n\nTranscript:\n{c}"}],
                ))
                partials.append(msg.content[0].text)

            merged = "\n---\n".join([f"Part {i+1}:\n{s}" for i, s in enumerate(partials)])
            msg = call_claude(lambda: client.messages.create(
                model="claude-sonnet-4-5-20250929", max_tokens=2048,
                messages=[{"role": "user", "content": f"Merge these partial meeting summaries into one cohesive summary. Max 8-10 bullets. Use the format: Key Topics, Decisions, Action Items.\n\n{merged}"}],
            ))
            summary = msg.content[0].text

        session_data["summary"] = summary
        return jsonify({"summary": summary})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/email", methods=["POST"])
def generate_email():
    transcript = request.json.get("transcript", "")
    summary = request.json.get("summary", "")
    if not transcript:
        return jsonify({"error": "No transcript provided"}), 400

    prompt = f"""Write a follow-up email in 50 words or less. JC Pollard / Josh Braun LinkedIn style: warm, conversational, no corporate jargon. Use "you/your" language.

Structure: Subject line → Hook → 2 key points → clear CTA. Reference specifics from the meeting.

Meeting summary:
{summary}

Context:
{transcript[:200000]}"""

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ))
        email = msg.content[0].text
        session_data["email"] = email
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

    base_style = "JC Pollard / Josh Braun style: warm, conversational, direct, no corporate jargon. Use you/your language."

    if style == "shorter":
        instruction = f"Rewrite in 30 words max. {base_style} Keep only the essential point and CTA.\n\nCurrent email:\n{current_email}"
    elif style == "longer":
        instruction = f"Expand to 100-120 words. {base_style} Add more context, detail, and a stronger CTA. Reference specifics.\n\nMeeting summary:\n{summary}\n\nCurrent email:\n{current_email}"
    elif style == "casual":
        instruction = f"Rewrite casually. Like texting a work friend. ~50 words. {base_style}\n\nCurrent email:\n{current_email}"
    elif style == "professional":
        instruction = f"Rewrite in polished professional tone. Suitable for C-suite. ~50 words.\n\nCurrent email:\n{current_email}"
    elif style == "urgent":
        instruction = f"Rewrite with urgency. Time-sensitive CTA. ~50 words. {base_style}\n\nCurrent email:\n{current_email}"
    elif style == "retry":
        instruction = f"Write a completely different follow-up email. Different angle, different hook. 50 words max. {base_style}\n\nMeeting summary:\n{summary}\n\nContext:\n{transcript[:100000]}"
    else:
        instruction = f"Rewrite this email. 50 words max. {base_style}\n\nCurrent email:\n{current_email}"

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": instruction}],
        ))
        email = msg.content[0].text
        session_data["email"] = email
        return jsonify({"email": email})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/email/quick-edit", methods=["POST"])
def quick_edit_email():
    current_email = request.json.get("current_email", "")
    edit_instruction = request.json.get("instruction", "")

    if not current_email or not edit_instruction:
        return jsonify({"error": "Email and instruction required"}), 400

    prompt = f"""Edit this email based on the instruction. Return only the edited email, no explanation.

Instruction: {edit_instruction}

Current email:
{current_email}"""

    try:
        client = get_claude()
        msg = call_claude(lambda: client.messages.create(
            model="claude-sonnet-4-5-20250929", max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ))
        email = msg.content[0].text
        session_data["email"] = email
        return jsonify({"email": email})
    except Exception as e:
        return handle_api_error(e)


@app.route("/api/chat", methods=["POST"])
def chat():
    question = request.json.get("question", "")
    transcript = request.json.get("transcript", "")
    chat_history = request.json.get("history", [])

    if not question:
        return jsonify({"error": "No question provided"}), 400

    system_prompt = f"""You are a sharp, senior sales intelligence assistant built for AEs, SDRs, and sales engineers. You analyze meeting transcripts and give actionable answers.

RESPONSE RULES:
- Ultra-concise: 2-4 sentences max unless the user asks for detail
- Bottom-line-up-front: answer first, supporting context second
- Use bullet points (•) for any list, never dashes
- Bold key terms, names, and numbers with **text**
- Use line breaks between distinct points for readability
- Business vocabulary: pipeline, qualified, discovery, ROI, stakeholder, next steps, blocker
- Anticipate what a sales professional would ask next
- If something wasn't discussed, say so in one sentence — don't pad

Meeting Transcript:
{transcript[:MAX_CHARS_PER_CHUNK]}

Meeting Summary:
{session_data.get('summary', 'Not generated yet.')}"""

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
    app.run(debug=True, port=5050)
