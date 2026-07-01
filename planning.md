# Provenance Guard Planning

## Product Goal

Provenance Guard is a backend service for creative writing platforms. A platform sends a text submission and creator ID to the API, and the service returns an attribution result, a calibrated confidence score, and reader-facing transparency label text. The system is intentionally cautious because a false positive against a human writer is more harmful than failing to catch every AI-assisted submission.

## Architecture

```text
Submission flow
--------------
POST /submit
  | raw text + creator_id
  v
Request validator
  | normalized text
  v
Detection pipeline
  |--> Signal 1: Groq LLM assessment, or local LLM proxy if Groq is unavailable
  |--> Signal 2: stylometric heuristics
  |--> Signal 3: AI phrase / generic-transition signal
  v
Weighted confidence scorer
  | attribution + ai_likelihood + classification confidence
  v
Transparency label generator
  | exact label text
  v
SQLite decision store + structured audit log
  | persisted decision
  v
JSON response to platform

Appeal flow
-----------
POST /appeal
  | content_id + creator_reasoning
  v
Lookup original decision
  | original classification + signal scores
  v
Status update
  | status = under_review
  v
Structured audit log
  | appeal entry linked to original decision
  v
JSON confirmation
```

The submission flow runs validation, detection, scoring, label selection, persistence, and audit logging before returning the result. The appeal flow never overwrites the original classification; it updates the content status to `under_review` and appends a new audit event so a human reviewer can see both the original signal evidence and the creator's reasoning.

## API Surface

`POST /submit`

Request:

```json
{
  "creator_id": "writer-123",
  "text": "The submitted poem, excerpt, or post..."
}
```

Response:

```json
{
  "content_id": "uuid",
  "creator_id": "writer-123",
  "status": "classified",
  "attribution": "likely_ai | likely_human | uncertain",
  "ai_likelihood": 0.728,
  "confidence": 0.728,
  "label_variant": "high_confidence_ai | high_confidence_human | uncertain",
  "label": "reader-facing label text",
  "signals": []
}
```

`POST /appeal`

Request:

```json
{
  "content_id": "uuid",
  "creator_reasoning": "I wrote this myself from personal experience..."
}
```

Response:

```json
{
  "content_id": "uuid",
  "status": "under_review",
  "message": "Appeal received. A human reviewer should evaluate this classification."
}
```

`GET /log` returns recent structured audit entries. `GET /appeals` returns the human review queue. `GET /analytics` returns simple aggregate detection and appeal metrics.

## Detection Signals

### Signal 1: Groq LLM Assessment

What it measures: A Groq-hosted `llama-3.3-70b-versatile` prompt assesses the whole text for semantic coherence, generic AI phrasing, human specificity, and overall authorship cues.

Output: `ai_score` from 0 to 1, where 1 means the signal sees strong AI-generation evidence; `confidence` from 0 to 1; and a short rationale.

Why this differs between human and AI writing: The LLM can consider holistic qualities that simple statistics miss, such as whether details feel grounded in lived experience or whether the prose follows a generic template.

Blind spots: LLM judgments are probabilistic and can be biased by genre. Formal human writing may look AI-like, and edited AI output may look human. If Groq is unavailable, the app uses a deterministic local proxy so local development and tests still work.

### Signal 2: Stylometric Heuristics

What it measures: Sentence length variance, type-token ratio, average word length, punctuation density, and casual markers.

Output: `ai_score` from 0 to 1 with details for each metric.

Why this differs between human and AI writing: AI-generated text often has more even sentence structure, polished word choice, and fewer abrupt personal or casual markers. Human writing, especially informal writing, tends to vary more.

Blind spots: Poetry, academic prose, non-native English, and intentionally polished human writing can all score as more AI-like than they really are.

### Signal 3: AI Phrase / Generic-Transition Signal

What it measures: Phrases and transitions commonly found in generic AI output, such as "it is important to note," "transformative paradigm," "furthermore," and similar broad framing.

Output: `ai_score` from 0 to 1 with matched phrases and transition counts.

Why this differs between human and AI writing: Generic AI drafts often lean on broad signposting and safe high-level claims instead of concrete context.

Blind spots: A human can naturally write with these phrases, especially in school or business writing. This signal is weighted lower than the LLM-style assessment because phrase matching is brittle.

## Confidence Scoring

Each signal returns an AI-likelihood score from 0 to 1. The scorer combines them with weighted voting:

```text
Groq LLM signal: 50% when available
Local LLM proxy: 42% when Groq is unavailable
Stylometric signal: 33%
AI phrase signal: 25%
```

The final `ai_likelihood` is the weighted average after lightly adjusting each weight by the signal's own confidence. The public `confidence` score is `max(ai_likelihood, 1 - ai_likelihood)`, meaning values near 0.5 are low-confidence/borderline and values near 0 or 1 are stronger classifications.

Thresholds:

```text
ai_likelihood >= 0.70 -> likely_ai
ai_likelihood <= 0.30 -> likely_human
0.31 through 0.69 -> uncertain
```

A score around 0.60 means the system sees some AI evidence but not enough agreement to make a strong claim, so it should display the uncertain label. A score around 0.95 means the system has strong directional evidence and can show a high-confidence variant. I chose a wider uncertain band because false positives against human creators are worse than missed AI detections.

## Transparency Label Design

| Variant | Exact label text |
| --- | --- |
| High-confidence AI | "Likely AI-generated (high confidence): Multiple detection signals suggest this text was probably generated or heavily assisted by AI. Creators can appeal this label." |
| High-confidence human | "Likely human-written (high confidence): The available signals suggest this text was probably written by a person. This is not a guarantee; it is a transparency signal." |
| Uncertain | "Attribution uncertain: Our signals do not agree strongly enough to label this as AI-generated or human-written. We are showing uncertainty rather than forcing a guess." |

The API also returns numeric `ai_likelihood` and `confidence`, so the platform can display the banded plain-language label while retaining the score for moderation and audit views.

## Appeals Workflow

Who can appeal: A creator whose content received any classification can submit an appeal with the `content_id` and a free-text explanation.

What they provide: `content_id` and `creator_reasoning`.

System behavior: The service looks up the original decision, updates its status from `classified` to `under_review`, stores the creator's reasoning, and writes an `appeal` event to the audit log. The appeal does not automatically reclassify the work.

Reviewer view: `GET /appeals` returns content ID, creator ID, excerpt, attribution, scores, status, appeal reasoning, and update time. A reviewer would use that queue to inspect contested decisions.

## Anticipated Edge Cases

Poetry with repetition and simple vocabulary: Stylometric heuristics may interpret repeated words and consistent short lines as AI-like uniformity, even when the piece is human-written.

Formal writing by a human expert or non-native English writer: The text may use polished transitions, fewer contractions, and careful structure, causing the phrase and stylometric signals to overestimate AI likelihood.

Lightly edited AI output with personal details added: The phrase signal may drop and casual markers may reduce the stylometric score, leaving the system uncertain rather than detecting AI confidently.

Very short submissions: A short excerpt does not provide enough sentence variance or vocabulary diversity for stable heuristics, so the API rejects texts under 20 characters and lowers signal confidence for short samples.

## AI Tool Plan

### M3: Submission Endpoint + First Signal

Spec sections to provide: Architecture, API Surface, Signal 1.

Ask: Generate a Flask app skeleton with `POST /submit`, request validation, a unique `content_id`, Groq LLM signal function, and structured SQLite audit logging.

Verification: Call the signal directly with known AI-like and human-like examples, then submit through the endpoint and confirm the response includes `content_id`, attribution, placeholder score fields, and one audit entry.

### M4: Second Signal + Confidence Scoring

Spec sections to provide: Detection Signals, Confidence Scoring, Architecture.

Ask: Add stylometric and phrase-based signal functions, combine their outputs with weighted scoring, and expose individual signal details in the response and audit log.

Verification: Test four deliberate samples: clearly AI-like, clearly human-like, formal human writing, and lightly edited AI-like prose. Confirm scores vary meaningfully and all signals are stored.

### M5: Production Layer

Spec sections to provide: Transparency Label Design, Appeals Workflow, Architecture.

Ask: Add label generation, `POST /appeal`, `GET /log`, rate limiting, and complete audit entries for classifications and appeals.

Verification: Confirm each label variant is reachable, appeal updates status to `under_review`, `GET /log` shows both classification and appeal events, and rapid submit requests trigger HTTP 429.

## Stretch Feature Plan

I implemented two small stretch pieces after the required flow was working:

Ensemble detection: The pipeline uses three signals with documented weighting instead of only two.

Analytics dashboard endpoint: `GET /analytics` returns total classifications, counts by attribution, appeal count, appeal rate, and average confidence.
