import hashlib
import json
import os
import re
import sqlite3
import statistics
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


load_dotenv()


AI_LABEL = (
    "Likely AI-generated (high confidence): Multiple detection signals suggest "
    "this text was probably generated or heavily assisted by AI. Creators can "
    "appeal this label."
)
HUMAN_LABEL = (
    "Likely human-written (high confidence): The available signals suggest this "
    "text was probably written by a person. This is not a guarantee; it is a "
    "transparency signal."
)
UNCERTAIN_LABEL = (
    "Attribution uncertain: Our signals do not agree strongly enough to label "
    "this as AI-generated or human-written. We are showing uncertainty rather "
    "than forcing a guess."
)

DEFAULT_SUBMIT_LIMIT = "10 per minute;100 per day"


@dataclass
class SignalResult:
    name: str
    ai_score: float
    confidence: float
    details: dict


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def tokenize(text):
    return re.findall(r"[A-Za-z']+", text.lower())


def split_sentences(text):
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    return sentences or [text.strip()]


def local_llm_proxy_signal(text):
    """Deterministic fallback used when Groq is unavailable."""
    style = stylometric_signal(text).ai_score
    phrase = ai_phrase_signal(text).ai_score
    words = tokenize(text)
    first_person = sum(1 for w in words if w in {"i", "me", "my", "we", "our"})
    personal_offset = min(first_person / max(len(words), 1) * 4, 0.20)
    ai_score = clamp((0.65 * phrase) + (0.35 * style) - personal_offset)
    return SignalResult(
        name="local_llm_proxy_signal",
        ai_score=round(ai_score, 3),
        confidence=0.58,
        details={
            "reason": "Groq unavailable or disabled; used deterministic lexical proxy.",
            "style_component": round(style, 3),
            "phrase_component": round(phrase, 3),
            "personal_offset": round(personal_offset, 3),
        },
    )


def groq_llm_signal(text):
    if os.getenv("PROVENANCE_USE_GROQ", "true").lower() in {"0", "false", "no"}:
        return local_llm_proxy_signal(text)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return local_llm_proxy_signal(text)

    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        prompt = (
            "You are one signal in a provenance system. Assess whether the text "
            "was likely AI-generated or human-written. Return only JSON with "
            "keys ai_score (0 to 1), confidence (0 to 1), and rationale. "
            "False positives against human writers are costly, so avoid high "
            "AI scores unless the evidence is strong.\n\nTEXT:\n"
            f"{text[:4000]}"
        )
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=220,
        )
        raw = completion.choices[0].message.content or "{}"
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        data = json.loads(match.group(0) if match else raw)
        return SignalResult(
            name="groq_llm_signal",
            ai_score=round(clamp(float(data.get("ai_score", 0.5))), 3),
            confidence=round(clamp(float(data.get("confidence", 0.5))), 3),
            details={"rationale": str(data.get("rationale", ""))[:500]},
        )
    except Exception as exc:
        fallback = local_llm_proxy_signal(text)
        fallback.details["groq_error"] = str(exc)[:300]
        return fallback


def stylometric_signal(text):
    words = tokenize(text)
    sentences = split_sentences(text)
    sentence_lengths = [len(tokenize(sentence)) for sentence in sentences if sentence]
    total_words = max(len(words), 1)
    unique_ratio = len(set(words)) / total_words
    mean_sentence = statistics.mean(sentence_lengths) if sentence_lengths else total_words
    stdev_sentence = statistics.pstdev(sentence_lengths) if len(sentence_lengths) > 1 else 0

    uniformity = 1 - clamp((stdev_sentence / max(mean_sentence, 1)) / 0.75)
    low_diversity = clamp((0.68 - unique_ratio) / 0.34)
    avg_word_len = statistics.mean([len(w) for w in words]) if words else 0
    polished_formality = clamp((avg_word_len - 4.2) / 2.2)
    punctuation_density = len(re.findall(r"[,;:]", text)) / total_words
    balanced_punctuation = 1 - clamp(abs(punctuation_density - 0.045) / 0.08)

    casual_markers = len(
        re.findall(r"\b(ok|honestly|lol|kinda|gonna|wanna|yeah|super|way)\b|[A-Z]{3,}", text)
    )
    casual_offset = clamp(casual_markers / 5)

    ai_score = (
        (0.35 * uniformity)
        + (0.25 * low_diversity)
        + (0.25 * polished_formality)
        + (0.15 * balanced_punctuation)
        - (0.22 * casual_offset)
    )
    confidence = 0.62 if total_words >= 35 and len(sentences) >= 2 else 0.45

    return SignalResult(
        name="stylometric_signal",
        ai_score=round(clamp(ai_score), 3),
        confidence=confidence,
        details={
            "sentence_count": len(sentences),
            "mean_sentence_length": round(mean_sentence, 2),
            "sentence_length_stdev": round(stdev_sentence, 2),
            "type_token_ratio": round(unique_ratio, 3),
            "avg_word_length": round(avg_word_len, 2),
            "casual_markers": casual_markers,
        },
    )


def ai_phrase_signal(text):
    lowered = text.lower()
    phrases = [
        "it is important to note",
        "it is equally essential",
        "transformative paradigm",
        "modern society",
        "ethical implications",
        "stakeholders",
        "various sectors",
        "responsible deployment",
        "furthermore",
        "in conclusion",
        "numerous benefits",
        "collaborate to ensure",
        "genuine tradeoffs",
        "studies show",
    ]
    transition_words = {
        "furthermore",
        "moreover",
        "therefore",
        "however",
        "additionally",
        "consequently",
        "ultimately",
    }
    words = tokenize(text)
    phrase_hits = [phrase for phrase in phrases if phrase in lowered]
    transition_hits = sum(1 for word in words if word in transition_words)
    hedge_hits = len(re.findall(r"\b(may|might|can|could|often|typically|generally)\b", lowered))
    personal_hits = len(re.findall(r"\b(i|me|my|honestly|friend|downtown|myself)\b", lowered))

    phrase_score = clamp(len(phrase_hits) / 5)
    transition_score = clamp(transition_hits / max(len(words), 1) * 18)
    hedge_score = clamp(hedge_hits / max(len(words), 1) * 12)
    personal_offset = clamp(personal_hits / max(len(words), 1) * 8)

    ai_score = clamp(
        (0.85 * phrase_score)
        + (0.10 * transition_score)
        + (0.05 * hedge_score)
        - (0.25 * personal_offset)
    )
    return SignalResult(
        name="ai_phrase_signal",
        ai_score=round(ai_score, 3),
        confidence=0.50 if len(words) >= 20 else 0.35,
        details={
            "matched_phrases": phrase_hits,
            "transition_hits": transition_hits,
            "hedge_hits": hedge_hits,
            "personal_hits": personal_hits,
        },
    )


def run_detection_pipeline(text):
    signals = [groq_llm_signal(text), stylometric_signal(text), ai_phrase_signal(text)]
    weights = {
        "groq_llm_signal": 0.50,
        "local_llm_proxy_signal": 0.42,
        "stylometric_signal": 0.33,
        "ai_phrase_signal": 0.25,
    }
    weighted_total = 0
    weight_sum = 0
    for signal in signals:
        weight = weights[signal.name] * (0.75 + (0.25 * signal.confidence))
        weighted_total += signal.ai_score * weight
        weight_sum += weight
    ai_likelihood = round(clamp(weighted_total / weight_sum if weight_sum else 0.5), 3)
    confidence = round(max(ai_likelihood, 1 - ai_likelihood), 3)
    attribution, label_variant, label = label_for_score(ai_likelihood)
    return {
        "attribution": attribution,
        "ai_likelihood": ai_likelihood,
        "confidence": confidence,
        "label_variant": label_variant,
        "label": label,
        "signals": [asdict(signal) for signal in signals],
    }


def label_for_score(ai_likelihood):
    if ai_likelihood >= 0.70:
        return "likely_ai", "high_confidence_ai", AI_LABEL
    if ai_likelihood <= 0.30:
        return "likely_human", "high_confidence_human", HUMAN_LABEL
    return "uncertain", "uncertain", UNCERTAIN_LABEL


def get_db(app):
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def db_connection(app):
    connection = get_db(app)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(app):
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with db_connection(app) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                text_excerpt TEXT NOT NULL,
                attribution TEXT NOT NULL,
                ai_likelihood REAL NOT NULL,
                confidence REAL NOT NULL,
                label_variant TEXT NOT NULL,
                label TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                status TEXT NOT NULL,
                appeal_reasoning TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                attribution TEXT,
                ai_likelihood REAL,
                confidence REAL,
                label_variant TEXT,
                status TEXT NOT NULL,
                signals_json TEXT,
                appeal_reasoning TEXT
            );
            """
        )


def row_to_audit_entry(row):
    entry = dict(row)
    entry["signals"] = json.loads(entry.pop("signals_json")) if entry.get("signals_json") else []
    return entry


def log_audit_event(app, *, event_type, content_id, creator_id, status, classification=None, appeal_reasoning=None):
    timestamp = utc_now()
    classification = classification or {}
    with db_connection(app) as db:
        db.execute(
            """
            INSERT INTO audit_entries (
                timestamp, event_type, content_id, creator_id, attribution,
                ai_likelihood, confidence, label_variant, status, signals_json,
                appeal_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                event_type,
                content_id,
                creator_id,
                classification.get("attribution"),
                classification.get("ai_likelihood"),
                classification.get("confidence"),
                classification.get("label_variant"),
                status,
                json.dumps(classification.get("signals", [])),
                appeal_reasoning,
            ),
        )


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.getenv("PROVENANCE_DB_PATH", "data/provenance_guard.db"),
        SUBMIT_LIMIT=os.getenv("PROVENANCE_SUBMIT_LIMIT", DEFAULT_SUBMIT_LIMIT),
        RATELIMIT_STORAGE_URI="memory://",
    )
    if test_config:
        app.config.update(test_config)

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=app.config["RATELIMIT_STORAGE_URI"],
    )

    init_db(app)

    @app.get("/")
    def index():
        return jsonify(
            {
                "name": "Provenance Guard API",
                "status": "running",
                "endpoints": {
                    "health": "GET /health",
                    "submit": "POST /submit",
                    "appeal": "POST /appeal",
                    "log": "GET /log",
                    "appeals": "GET /appeals",
                    "analytics": "GET /analytics",
                },
            }
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/submit")
    @limiter.limit(lambda: app.config["SUBMIT_LIMIT"])
    def submit():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text", "")).strip()
        creator_id = str(payload.get("creator_id", "")).strip()

        if not text or not creator_id:
            return jsonify({"error": "Both text and creator_id are required."}), 400
        if len(text) < 20:
            return jsonify({"error": "Text must be at least 20 characters for attribution analysis."}), 400

        content_id = str(uuid.uuid4())
        timestamp = utc_now()
        classification = run_detection_pipeline(text)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        text_excerpt = text[:240]

        with db_connection(app) as db:
            db.execute(
                """
                INSERT INTO decisions (
                    content_id, creator_id, text_hash, text_excerpt, attribution,
                    ai_likelihood, confidence, label_variant, label, signals_json,
                    status, appeal_reasoning, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_id,
                    creator_id,
                    text_hash,
                    text_excerpt,
                    classification["attribution"],
                    classification["ai_likelihood"],
                    classification["confidence"],
                    classification["label_variant"],
                    classification["label"],
                    json.dumps(classification["signals"]),
                    "classified",
                    None,
                    timestamp,
                    timestamp,
                ),
            )

        log_audit_event(
            app,
            event_type="classification",
            content_id=content_id,
            creator_id=creator_id,
            status="classified",
            classification=classification,
        )

        return jsonify(
            {
                "content_id": content_id,
                "creator_id": creator_id,
                "status": "classified",
                **classification,
            }
        )

    @app.post("/appeal")
    def appeal():
        payload = request.get_json(silent=True) or {}
        content_id = str(payload.get("content_id", "")).strip()
        creator_reasoning = str(payload.get("creator_reasoning", "")).strip()

        if not content_id or not creator_reasoning:
            return jsonify({"error": "Both content_id and creator_reasoning are required."}), 400

        with db_connection(app) as db:
            row = db.execute("SELECT * FROM decisions WHERE content_id = ?", (content_id,)).fetchone()
            if row is None:
                return jsonify({"error": "No classification found for that content_id."}), 404

            timestamp = utc_now()
            db.execute(
                """
                UPDATE decisions
                SET status = ?, appeal_reasoning = ?, updated_at = ?
                WHERE content_id = ?
                """,
                ("under_review", creator_reasoning, timestamp, content_id),
            )

        classification = {
            "attribution": row["attribution"],
            "ai_likelihood": row["ai_likelihood"],
            "confidence": row["confidence"],
            "label_variant": row["label_variant"],
            "signals": json.loads(row["signals_json"]),
        }
        log_audit_event(
            app,
            event_type="appeal",
            content_id=content_id,
            creator_id=row["creator_id"],
            status="under_review",
            classification=classification,
            appeal_reasoning=creator_reasoning,
        )

        return jsonify(
            {
                "content_id": content_id,
                "status": "under_review",
                "message": "Appeal received. A human reviewer should evaluate this classification.",
            }
        )

    @app.get("/log")
    def log():
        limit = clamp(int(request.args.get("limit", 20)), 1, 100)
        with db_connection(app) as db:
            rows = db.execute(
                "SELECT * FROM audit_entries ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return jsonify({"entries": [row_to_audit_entry(row) for row in rows]})

    @app.get("/appeals")
    def appeals():
        with db_connection(app) as db:
            rows = db.execute(
                """
                SELECT content_id, creator_id, text_excerpt, attribution,
                       ai_likelihood, confidence, label_variant, status,
                       appeal_reasoning, updated_at
                FROM decisions
                WHERE status = 'under_review'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return jsonify({"appeals": [dict(row) for row in rows]})

    @app.get("/analytics")
    def analytics():
        with db_connection(app) as db:
            total = db.execute("SELECT COUNT(*) AS value FROM decisions").fetchone()["value"]
            appeals_count = db.execute(
                "SELECT COUNT(*) AS value FROM decisions WHERE status = 'under_review'"
            ).fetchone()["value"]
            by_attribution = db.execute(
                "SELECT attribution, COUNT(*) AS count FROM decisions GROUP BY attribution"
            ).fetchall()
            avg_confidence = db.execute("SELECT AVG(confidence) AS value FROM decisions").fetchone()["value"]
        return jsonify(
            {
                "total_classifications": total,
                "appeals": appeals_count,
                "appeal_rate": round(appeals_count / total, 3) if total else 0,
                "average_confidence": round(avg_confidence or 0, 3),
                "by_attribution": [dict(row) for row in by_attribution],
            }
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
