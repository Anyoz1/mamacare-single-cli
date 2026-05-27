# MamaCare CDP CLI

This script attaches to an already-running Chromium instance exposed at:

```text
http://127.0.0.1:9222
```

It does not launch a browser. It scans the existing CDP tabs and selects only
the tab whose URL contains:

```text
mamacare.kaznu.kz/chat
```

ChatGPT tabs in the same Chromium profile are ignored because their URLs do not
match that MamaCare fragment.

## Setup

```bash
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python mamacare_cdp.py cdp-check
```

Send one test question and print the extracted answer:

```bash
.venv/bin/python mamacare_cdp.py test-send "Можно ли пить парацетамол при беременности?"
```

## Dataset Workflow

Generated questions must be a JSON array in `data/raw/generated.txt`. Import them
into the queue:

```bash
.venv/bin/python mamacare_cdp.py import data/raw/generated.txt
```

The import command validates records, adds missing `single_0001` style IDs, writes
valid records to `data/queue/single_queue.jsonl`, and writes rejected records to
`data/rejected/single_rejected.jsonl`.

Process the first queued question:

```bash
.venv/bin/python mamacare_cdp.py ask-next
```

Process a batch:

```bash
.venv/bin/python mamacare_cdp.py batch --limit 10
```

Fast batch mode uses a shorter answer wait and saves truncated outputs as
`needs_review` instead of waiting for a perfect ending:

```bash
.venv/bin/python mamacare_cdp.py batch --limit 9 --fast
.venv/bin/python mamacare_cdp.py batch --limit 9 --max-wait 30 --stable-polls 2 --poll-interval 0.75
.venv/bin/python mamacare_cdp.py batch --limit 9 --retry-truncated 1
```

Export the JSONL final dataset to a usable-only pretty JSON array:

```bash
.venv/bin/python mamacare_cdp.py export-json
```

Export an audit JSON that includes needs-review/action-card metadata:

```bash
.venv/bin/python mamacare_cdp.py export-json --include-audit
```

Find and fix final records that accidentally captured MamaCare UI cards instead
of answers:

```bash
.venv/bin/python mamacare_cdp.py validate-final
.venv/bin/python mamacare_cdp.py validate-final --fix
```

Remove empty/action-card-only records from final JSONL after creating
`data/final/single_dataset.jsonl.bak`:

```bash
.venv/bin/python mamacare_cdp.py clean-final-jsonl --remove-action-cards
```

Requeue action-card or empty-answer records for another attempt:

```bash
.venv/bin/python mamacare_cdp.py requeue-needs-review
```

Inspect truncated answers:

```bash
.venv/bin/python mamacare_cdp.py validate-truncated
```

Each question starts a new MamaCare chat before sending. This keeps the answer
single-turn and prevents previous messages from affecting the model response.

Inspect the final dataset:

```bash
sed -n '1,20p' data/final/single_dataset.jsonl
sed -n '1,80p' data/final/single_dataset.json
```

Each line is one JSON object using UTF-8 text and `ensure_ascii=False`.

Check progress:

```bash
.venv/bin/python mamacare_cdp.py stats
```

Inspect input detection on the live MamaCare page:

```bash
.venv/bin/python mamacare_cdp.py debug-inputs
.venv/bin/python mamacare_cdp.py inspect-dom
.venv/bin/python mamacare_cdp.py inspect-chat-state
```

`debug-inputs` prints every textarea/contenteditable/text input candidate, its
geometry, attributes, and the reason it was accepted or rejected. Search and chat
history inputs are rejected before batch processing can use them.

`inspect-dom` writes:

```text
data/debug/mamacare_page.html
data/debug/mamacare_body.txt
data/debug/mamacare_before.png
```

and prints visible buttons, inputs, textareas, contenteditable elements, and chat
container candidates. `inspect-chat-state` prints the currently selected DOM
state: message input, send button, generation state, latest user message, latest
assistant message, action-card texts, and message bubble count.

## Real MamaCare DOM Findings

The current MamaCare chat DOM uses these stable selectors:

```text
Message input: textarea[placeholder="Введите сообщение..."]
Send button: nearest input-row button containing svg.lucide-send
New chat: visible button text "Новый чат"
Message thread: textarea ancestor .flex.flex-col.h-full, then .overflow-y-auto
User bubble: .flex.gap-3.group.flex-row-reverse
Assistant bubble: .flex.gap-3.group.flex-row
```

Generation completion is detected from real UI state first: stop/loading buttons,
spinner/loading classes, `aria-busy`, and visible loading state. The disabled send
button alone is not treated as generation, because it is also disabled when the
textarea is empty after a completed answer.

The extractor ignores welcome suggestions and UI/action cards such as
`Уточним несколько деталей`, `Уточним кормление малыша`, `Несколько вопросов...`,
and standalone CTA buttons like `Открыть`. It does not remove ordinary medical
text only because it contains words such as `срочно`, `обратитесь`, or
`скорую помощь`.

For each processed dataset id, batch/test-send writes DOM debug artifacts:

```text
data/debug/<id>_new_chat.png
data/debug/<id>_after_send.png
data/debug/<id>_after_answer.png
data/debug/<id>_messages.json
data/debug/<id>_state.json
```

## Run 3 Prompts At Once

Example `data/raw/generated.txt`:

```json
[
  {
    "language": "RU",
    "type": "single-turn",
    "source": "synthetic_llm",
    "topic": "pregnancy",
    "question": "У меня 20 недель беременности и поднялась температура 38.2. Что делать?",
    "answer": ""
  },
  {
    "language": "KZ",
    "type": "single-turn",
    "source": "synthetic_llm",
    "topic": "breastfeeding",
    "question": "Емізу кезінде басым ауырып тұрса, қандай дәрі ішуге болады?",
    "answer": ""
  },
  {
    "language": "RU+KZ",
    "type": "single-turn",
    "source": "synthetic_llm",
    "topic": "baby_care",
    "question": "Балам 2 айлық, температурасы көтеріліп кетті, что делать?",
    "answer": ""
  }
]
```

Run import, batch, and JSON export in one command:

```bash
.venv/bin/python mamacare_cdp.py batch-from-raw data/raw/generated.txt --limit 3
```

Final files:

```text
data/final/single_dataset.jsonl
data/final/single_dataset.json
```

## Troubleshooting

If the new-chat button is not found, the script navigates directly to
`https://mamacare.kaznu.kz/chat` and waits for the chat input. If old messages
still appear after that, it writes `data/debug/new_chat_warning.txt`.

Extraction debug text is written to `data/debug/latest_page_text.txt` when the
answer is uncertain. Batch errors are appended to `data/debug/errors.log`.
Failed batch records remain queued; only records with a saved final JSONL row are
marked answered in the queue.
# mamacare-single-cli
