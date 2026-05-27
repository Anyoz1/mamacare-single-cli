#!/usr/bin/env python3
"""Attach to an existing Chromium CDP session and use the MamaCare chat tab."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

from playwright.sync_api import Browser, Error, Locator, Page, TimeoutError, sync_playwright


CDP_ENDPOINT = "http://127.0.0.1:9222"
MAMACARE_URL_FRAGMENT = "mamacare.kaznu.kz/chat"
DEBUG_TEXT_PATH = "data/debug/latest_page_text.txt"
DEBUG_HTML_PATH = "data/debug/mamacare_page.html"
DEBUG_BODY_PATH = "data/debug/mamacare_body.txt"
DEBUG_SCREENSHOT_PATH = "data/debug/mamacare_before.png"
NEW_CHAT_WARNING_PATH = "data/debug/new_chat_warning.txt"
ERROR_LOG_PATH = "data/debug/errors.log"
RAW_GENERATED_PATH = "data/raw/generated.txt"
QUEUE_PATH = "data/queue/single_queue.jsonl"
FINAL_PATH = "data/final/single_dataset.jsonl"
FINAL_JSON_PATH = "data/final/single_dataset.json"
FINAL_AUDIT_JSON_PATH = "data/final/single_dataset_audit.json"
FINAL_BACKUP_PATH = "data/final/single_dataset.jsonl.bak"
REJECTED_PATH = "data/rejected/single_rejected.jsonl"
VALID_LANGUAGES = {"RU", "KZ", "EN", "RU+KZ", "RU+EN"}
DEFAULT_MAX_WAIT_SECONDS = 120.0
DEFAULT_STABLE_POLLS = 4
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MIN_INITIAL_WAIT_SECONDS = 10.0
DEFAULT_INCOMPLETE_EXTRA_WAIT_SECONDS = 10.0
DEFAULT_MAX_INCOMPLETE_EXTRA_ROUNDS = 2
FAST_MAX_WAIT_SECONDS = 75.0
FAST_STABLE_POLLS = 3
FAST_POLL_INTERVAL_SECONDS = 1.0
FAST_MIN_INITIAL_WAIT_SECONDS = 6.0
FAST_INCOMPLETE_EXTRA_WAIT_SECONDS = 6.0
FAST_MAX_INCOMPLETE_EXTRA_ROUNDS = 1
INPUT_BAD_WORDS = (
    "search",
    "поиск",
    "іздеу",
    "history",
    "sidebar",
    "чаттарды",
    "chats",
)
WELCOME_ONLY_LINES = {
    "Добро пожаловать в MamaCare",
    "Ребёнок не берёт грудь",
    "Боль после родов",
    "Нормальны ли мои выделения",
    "Как я себя чувствую сегодня",
    "Пройти скрининг EPDS",
}
CTA_WORDS = ("Открыть", "Начать", "Подробнее", "Перейти")
WELCOME_SUGGESTION_MARKERS = (
    "Добро пожаловать в MamaCare",
    "Ребёнок не берёт грудь",
    "Боль после родов",
    "Нормальны ли мои выделения",
    "Как я себя чувствую сегодня",
    "Пройти скрининг EPDS",
)
CARD_BODY_MARKERS = (
    "Несколько вопросов",
    "Уточним кормление",
)
KNOWN_CARD_TITLES = (
    "Уточним несколько деталей",
    "Пройти скрининг EPDS",
    "Добро пожаловать в MamaCare",
    "Срочная помощь",
)
CHAT_INPUT_SELECTORS = (
    "textarea",
    '[contenteditable="true"]',
    'input[type="text"]',
    "input:not([type])",
)
SEND_BUTTON_TEXTS = ("Send", "Отправить", "Жіберу", "Жібер")
NEW_CHAT_TEXTS = ("Новый чат", "Жаңа чат", "New chat", "+", "＋")
NEW_CHAT_SELECTORS = (
    'button[aria-label*="new" i]',
    'button[aria-label*="chat" i]',
    'a[href*="/chat"]',
)
ASSISTANT_MESSAGE_SELECTORS = (
    '[data-testid*="assistant" i]',
    '[data-testid*="bot" i]',
    '[class*="assistant" i]',
    '[aria-label*="assistant" i]',
    '[aria-label*="bot" i]',
    ".message.assistant",
    ".assistant-message",
    ".bot-message",
)

_LAST_SENT_QUESTION: str | None = None
_LAST_ANSWER_WAIT_TIMED_OUT = False
_LAST_ANSWER_WAIT_SECONDS = 0.0
_LAST_ACTION_CARD_TEXT = ""
_LAST_STABLE_REAL_ANSWER_TEXT = ""


class ExtractionFailed(RuntimeError):
    """Raised when the page changed but the answer cannot be trusted."""


@dataclass(frozen=True)
class PageMatch:
    context_index: int
    page_index: int
    page: Page


@dataclass(frozen=True)
class InputCandidate:
    selector: str
    index: int
    locator: Locator
    tag: str
    placeholder: str
    aria_label: str
    title: str
    name: str
    element_id: str
    class_name: str
    bbox: dict[str, float] | None
    viewport_height: int
    visible: bool
    enabled: bool
    in_main: bool
    in_form: bool
    in_chat_container: bool
    rejected: bool
    rejection_reason: str
    score: tuple[int, int, int, float, float]


@dataclass(frozen=True)
class ExtractedAnswer:
    answer: str
    uncertain: bool
    action_card_detected: bool = False
    raw_text: str = ""
    truncated: bool = False
    card_removed_from_answer: bool = False


@dataclass(frozen=True)
class MessageBubble:
    index: int
    role: str
    text: str
    selector_hint: str
    class_name: str
    bbox: dict[str, float] | None
    is_action_card: bool = False


@dataclass(frozen=True)
class WaitConfig:
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS
    stable_polls: int = DEFAULT_STABLE_POLLS
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS
    min_initial_wait: float = DEFAULT_MIN_INITIAL_WAIT_SECONDS
    incomplete_extra_wait: float = DEFAULT_INCOMPLETE_EXTRA_WAIT_SECONDS
    max_incomplete_extra_rounds: int = DEFAULT_MAX_INCOMPLETE_EXTRA_ROUNDS
    fast_mode: bool = False


def iter_pages(browser: Browser) -> list[PageMatch]:
    matches: list[PageMatch] = []
    for context_index, context in enumerate(browser.contexts):
        for page_index, page in enumerate(context.pages):
            matches.append(PageMatch(context_index, page_index, page))
    return matches


def find_mamacare_page(browser: Browser) -> PageMatch:
    pages = iter_pages(browser)
    for match in pages:
        if MAMACARE_URL_FRAGMENT in match.page.url:
            return match

    visible_pages = "\n".join(
        f"  [{match.context_index}:{match.page_index}] {match.page.url}"
        for match in pages
    )
    if not visible_pages:
        visible_pages = "  No open pages were exposed by the CDP session."

    raise RuntimeError(
        "Could not find an existing Chromium tab whose URL contains "
        f"{MAMACARE_URL_FRAGMENT!r}.\n\nOpen pages:\n{visible_pages}"
    )


def find_chat_input(page: Page) -> Locator:
    try:
        return find_message_input(page)
    except RuntimeError:
        pass

    candidates = collect_input_candidates(page)
    accepted = [candidate for candidate in candidates if not candidate.rejected]
    if not accepted:
        reasons = "; ".join(
            f"{candidate.selector}[{candidate.index}]: {candidate.rejection_reason}"
            for candidate in candidates
        )
        raise RuntimeError(
            "Could not find a safe MamaCare message input. "
            f"Rejected candidates: {reasons or 'none found'}"
        )
    return max(accepted, key=lambda candidate: candidate.score).locator


def find_message_input(page: Page) -> Locator:
    selectors = (
        'textarea[placeholder="Введите сообщение..."]',
        'textarea[data-slot="textarea"]',
        'main textarea',
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible() or not candidate.is_enabled():
                    continue
                bbox = candidate.bounding_box()
                placeholder = candidate.get_attribute("placeholder") or ""
                class_name = candidate.get_attribute("class") or ""
            except Error:
                continue
            if not bbox or bbox["width"] < 160 or bbox["height"] < 20:
                continue
            if input_rejection_reason(
                visible=True,
                enabled=True,
                bbox=bbox,
                placeholder=placeholder,
                aria_label=candidate.get_attribute("aria-label") or "",
                title=candidate.get_attribute("title") or "",
                name=candidate.get_attribute("name") or "",
                element_id=candidate.get_attribute("id") or "",
                class_name=class_name,
            ):
                continue
            return candidate
    raise RuntimeError("Could not find the real MamaCare message textarea.")


def find_send_button(page: Page) -> Locator:
    try:
        chat_input = find_message_input(page)
        relative = chat_input.locator(
            "xpath=ancestor::div[contains(@class, 'items-end')][1]"
            "//button[.//*[contains(@class, 'lucide-send')]]"
        )
        if relative.count() > 0 and relative.first.is_visible():
            return relative.first
    except Error:
        pass

    selectors = (
        'main button:has(svg.lucide-send)',
        'main button:has(.lucide-send)',
        'button:has(svg.lucide-send)',
        'button:has(.lucide-send)',
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Error:
                continue
    raise RuntimeError("Could not find the real MamaCare send button.")


def find_new_chat_button(page: Page) -> Locator:
    selectors = (
        'main button:has-text("Новый чат")',
        'button:has-text("Новый чат")',
        'button:has-text("Жаңа чат")',
        'button:has-text("New chat")',
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                bbox = candidate.bounding_box()
                if candidate.is_visible() and candidate.is_enabled() and bbox:
                    return candidate
            except Error:
                continue
    raise RuntimeError("Could not find the real MamaCare new-chat button.")


def collect_input_candidates(page: Page) -> list[InputCandidate]:
    viewport_size = page.viewport_size or {"width": 0, "height": 0}
    viewport_height = int(viewport_size.get("height") or 0)
    raw_candidates: list[InputCandidate] = []

    for selector_priority, selector in enumerate(CHAT_INPUT_SELECTORS):
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            raw_candidates.append(
                inspect_input_candidate(
                    candidate,
                    selector=selector,
                    selector_priority=selector_priority,
                    index=index,
                    viewport_height=viewport_height,
                )
            )

    has_lower_safe_candidate = any(
        not candidate.rejected and is_lower_half(candidate)
        for candidate in raw_candidates
    )
    if not has_lower_safe_candidate:
        return raw_candidates

    candidates = []
    for candidate in raw_candidates:
        if candidate.rejected or is_lower_half(candidate):
            candidates.append(candidate)
        else:
            candidates.append(reject_candidate(candidate, "not in lower half of viewport"))
    return candidates


def inspect_input_candidate(
    locator: Locator,
    selector: str,
    selector_priority: int,
    index: int,
    viewport_height: int,
) -> InputCandidate:
    visible = False
    enabled = False
    bbox = None
    details: dict[str, Any] = {}
    rejection_reason = ""

    try:
        visible = locator.is_visible()
        enabled = locator.is_enabled()
        bbox = locator.bounding_box()
        details = locator.evaluate(
            """
            element => {
              const attr = name => element.getAttribute(name) || "";
              const ancestry = [];
              for (let node = element; node; node = node.parentElement) {
                const bits = [
                  node.tagName ? node.tagName.toLowerCase() : "",
                  node.id ? `#${node.id}` : "",
                  node.className && typeof node.className === "string"
                    ? `.${node.className}` : ""
                ];
                ancestry.push(bits.join(" "));
              }
              const ancestryText = ancestry.join(" ");
              return {
                tag: element.tagName ? element.tagName.toLowerCase() : "",
                placeholder: attr("placeholder"),
                ariaLabel: attr("aria-label"),
                title: attr("title"),
                name: attr("name"),
                id: element.id || "",
                className: typeof element.className === "string" ? element.className : "",
                inMain: Boolean(element.closest("main")),
                inForm: Boolean(element.closest("form")),
                inChatContainer: /chat|message|conversation|dialog|thread|чат|сообщ|хабар/i.test(ancestryText)
              };
            }
            """
        )
    except Error as exc:
        rejection_reason = f"inspection failed: {exc}"

    tag = str(details.get("tag", ""))
    placeholder = str(details.get("placeholder", ""))
    aria_label = str(details.get("ariaLabel", ""))
    title = str(details.get("title", ""))
    name = str(details.get("name", ""))
    element_id = str(details.get("id", ""))
    class_name = str(details.get("className", ""))
    in_main = bool(details.get("inMain", False))
    in_form = bool(details.get("inForm", False))
    in_chat_container = bool(details.get("inChatContainer", False))

    if not rejection_reason:
        rejection_reason = input_rejection_reason(
            visible=visible,
            enabled=enabled,
            bbox=bbox,
            placeholder=placeholder,
            aria_label=aria_label,
            title=title,
            name=name,
            element_id=element_id,
            class_name=class_name,
        )

    rejected = bool(rejection_reason)
    score = input_candidate_score(
        selector_priority=selector_priority,
        bbox=bbox,
        viewport_height=viewport_height,
        in_main=in_main,
        in_form=in_form,
        in_chat_container=in_chat_container,
    )
    return InputCandidate(
        selector=selector,
        index=index,
        locator=locator,
        tag=tag,
        placeholder=placeholder,
        aria_label=aria_label,
        title=title,
        name=name,
        element_id=element_id,
        class_name=class_name,
        bbox=bbox,
        viewport_height=viewport_height,
        visible=visible,
        enabled=enabled,
        in_main=in_main,
        in_form=in_form,
        in_chat_container=in_chat_container,
        rejected=rejected,
        rejection_reason=rejection_reason,
        score=score,
    )


def input_rejection_reason(
    visible: bool,
    enabled: bool,
    bbox: dict[str, float] | None,
    placeholder: str,
    aria_label: str,
    title: str,
    name: str,
    element_id: str,
    class_name: str,
) -> str:
    if not visible:
        return "not visible"
    if not enabled:
        return "not enabled"
    if bbox is None:
        return "no bounding box"
    if bbox["width"] < 160:
        return f"too narrow ({bbox['width']:.0f}px)"
    if bbox["height"] < 20:
        return f"too short ({bbox['height']:.0f}px)"

    searchable_text = " ".join(
        (placeholder, aria_label, title, name, element_id, class_name)
    ).lower()
    for bad_word in INPUT_BAD_WORDS:
        if bad_word in searchable_text:
            return f"search/history/sidebar marker: {bad_word}"
    return ""


def input_candidate_score(
    selector_priority: int,
    bbox: dict[str, float] | None,
    viewport_height: int,
    in_main: bool,
    in_form: bool,
    in_chat_container: bool,
) -> tuple[int, int, int, float, float]:
    y = bbox["y"] if bbox else -1
    width = bbox["width"] if bbox else 0
    lower_half = int(bool(bbox) and viewport_height > 0 and y + bbox["height"] / 2 >= viewport_height / 2)
    container_score = int(in_main) + int(in_form) + int(in_chat_container)
    return (lower_half, container_score, -selector_priority, y, width)


def is_lower_half(candidate: InputCandidate) -> bool:
    if not candidate.bbox or candidate.viewport_height <= 0:
        return False
    return candidate.bbox["y"] + candidate.bbox["height"] / 2 >= candidate.viewport_height / 2


def reject_candidate(candidate: InputCandidate, reason: str) -> InputCandidate:
    return InputCandidate(
        selector=candidate.selector,
        index=candidate.index,
        locator=candidate.locator,
        tag=candidate.tag,
        placeholder=candidate.placeholder,
        aria_label=candidate.aria_label,
        title=candidate.title,
        name=candidate.name,
        element_id=candidate.element_id,
        class_name=candidate.class_name,
        bbox=candidate.bbox,
        viewport_height=candidate.viewport_height,
        visible=candidate.visible,
        enabled=candidate.enabled,
        in_main=candidate.in_main,
        in_form=candidate.in_form,
        in_chat_container=candidate.in_chat_container,
        rejected=True,
        rejection_reason=reason,
        score=candidate.score,
    )


def wait_for_chat_input(page: Page, timeout_ms: int = 60_000) -> Locator:
    deadline = time.monotonic() + timeout_ms / 1000
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            return find_chat_input(page)
        except RuntimeError as exc:
            last_error = exc
            page.wait_for_timeout(500)
    raise last_error or RuntimeError("Could not find a visible and enabled MamaCare chat input.")


def ensure_data_dirs() -> None:
    for path in (QUEUE_PATH, FINAL_PATH, REJECTED_PATH, DEBUG_TEXT_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)


def read_jsonl(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []

    records = []
    with open(path, "r", encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if not isinstance(value, dict):
                raise RuntimeError(f"{path}:{line_number}: JSONL line is not an object")
            records.append(value)
    return records


def atomic_write_jsonl(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as output_file:
        for record in records:
            output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temp_path, path)


def append_jsonl(path: str, record: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def atomic_write_json(path: str, records: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as output_file:
        json.dump(records, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")
    os.replace(temp_path, path)


def append_error(message: str) -> None:
    os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as error_file:
        error_file.write(f"[{timestamp}] {message}\n")


def used_ids() -> set[str]:
    return {
        str(record.get("id"))
        for record in [*read_jsonl(QUEUE_PATH), *read_jsonl(FINAL_PATH)]
        if record.get("id")
    }


def record_dedupe_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("language", "RU")).strip(),
        str(record.get("topic", "general")).strip(),
        str(record.get("question", "")).strip(),
    )


def existing_prompt_keys() -> set[tuple[str, str, str]]:
    return {
        record_dedupe_key(record)
        for record in [*read_jsonl(QUEUE_PATH), *read_jsonl(FINAL_PATH)]
        if str(record.get("question", "")).strip()
    }


def next_single_id(existing_ids: set[str]) -> str:
    index = 1
    while True:
        candidate = f"single_{index:04d}"
        if candidate not in existing_ids:
            return candidate
        index += 1


def contains_private_data(text: str) -> str | None:
    checks = (
        (r"[\w.+-]+@[\w-]+\.[\w.-]+", "contains email"),
        (r"(?:\+?\d[\s().-]*){10,}", "contains phone number"),
        (r"https?://(?:www\.)?(?:instagram|facebook|vk|t\.me|telegram|wa\.me|x\.com|twitter)\S*", "contains social media link"),
        (r"(?:ул\.|улица|проспект|пр-т|дом|квартира|street|avenue|apt\.?)\s+\S+", "contains exact address"),
        (r"(?:меня зовут|мое имя|менің атым|my name is)\s+[A-ZА-ЯЁӘІҢҒҮҰҚӨҺ][\wА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ-]+", "contains personal name"),
        (r"\b[A-ZА-ЯЁӘІҢҒҮҰҚӨҺ][a-zа-яёәіңғүұқөһ-]+\s+[A-ZА-ЯЁӘІҢҒҮҰҚӨҺ][a-zа-яёәіңғүұқөһ-]+\b", "contains obvious personal name"),
    )
    for pattern, reason in checks:
        if re.search(pattern, text):
            return reason
    return None


def validate_import_record(
    raw_record: Any, existing_ids: set[str]
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw_record, dict):
        return None, "record is not an object"

    record = dict(raw_record)
    record_type = record.get("type", "single-turn")
    language = record.get("language", "RU")
    question = str(record.get("question", "")).strip()
    answer = str(record.get("answer", "") or "").strip()

    if record_type != "single-turn":
        return None, 'type must be "single-turn"'
    if language not in VALID_LANGUAGES:
        return None, f"language must be one of: {', '.join(sorted(VALID_LANGUAGES))}"
    if not question:
        return None, "question must not be empty"
    if answer:
        return None, "answer must be empty during import"

    private_reason = contains_private_data(question)
    if private_reason:
        return None, private_reason

    record_id = str(record.get("id", "")).strip()
    if record_id:
        if record_id in existing_ids:
            return None, f"duplicate id: {record_id}"
    else:
        record_id = next_single_id(existing_ids)

    existing_ids.add(record_id)
    return {
        "id": record_id,
        "language": language,
        "type": "single-turn",
        "source": record.get("source") or "synthetic_llm",
        "topic": record.get("topic") or "general",
        "question": question,
        "answer": "",
        "status": "queued",
    }, None


def page_text(page: Page) -> str:
    return page.locator("body").inner_text(timeout=5_000)


def dom_probe(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const box = element => {
            const rect = element.getBoundingClientRect();
            return {
              x: rect.x,
              y: rect.y,
              width: rect.width,
              height: rect.height
            };
          };
          const visible = element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none"
              && style.visibility !== "hidden"
              && rect.width > 0
              && rect.height > 0;
          };
          const textOf = element =>
            (element.innerText || element.textContent || "").trim();
          const attrs = element => ({
            tag: element.tagName ? element.tagName.toLowerCase() : "",
            text: textOf(element),
            aria_label: element.getAttribute("aria-label") || "",
            title: element.getAttribute("title") || "",
            placeholder: element.getAttribute("placeholder") || "",
            name: element.getAttribute("name") || "",
            role: element.getAttribute("role") || "",
            aria_live: element.getAttribute("aria-live") || "",
            class: typeof element.className === "string" ? element.className : "",
            id: element.id || "",
            disabled: Boolean(element.disabled) || element.getAttribute("aria-disabled") === "true",
            visible: visible(element),
            bbox: box(element),
          });
          const candidates = selector =>
            [...document.querySelectorAll(selector)].map(attrs);
          const containerSelector = [
            "main",
            "form",
            "[role='main']",
            "[role='log']",
            "[aria-live]",
            "[class*='chat' i]",
            "[class*='message' i]",
            "[class*='conversation' i]",
            "[class*='overflow-y-auto' i]"
          ].join(",");
          const chatContainers = [...document.querySelectorAll(containerSelector)]
            .filter(element => visible(element))
            .slice(0, 120)
            .map(attrs);
          return {
            buttons: candidates("button,a,[role='button']"),
            inputs: candidates("input,textarea,[contenteditable='true']"),
            chat_containers: chatContainers,
          };
        }
        """
    )


def dom_message_blocks(page: Page) -> list[dict[str, Any]]:
    blocks = page.evaluate(
        """
        () => {
          const visible = element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none"
              && style.visibility !== "hidden"
              && rect.width > 0
              && rect.height > 0;
          };
          const box = element => {
            const rect = element.getBoundingClientRect();
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
          };
          const textOf = element =>
            (element.innerText || element.textContent || "").trim();
          const textarea = document.querySelector("textarea[placeholder='Введите сообщение...']")
            || document.querySelector("textarea[data-slot='textarea']")
            || document.querySelector("main textarea");
          let root = textarea ? textarea.closest(".flex.flex-col.h-full") : null;
          const scroll = root
            ? root.querySelector(".overflow-y-auto")
            : document.querySelector("main .overflow-y-auto") || document.querySelector("main");
          if (!scroll) return [];
          const elements = [...scroll.querySelectorAll("div,p,section,article,li")];
          return elements
            .filter(element => visible(element))
            .map((element, index) => {
              const text = textOf(element);
              const childTextSame = [...element.children].some(child =>
                visible(child) && textOf(child) === text && text.length > 0
              );
              return {
                index,
                tag: element.tagName.toLowerCase(),
                text,
                class: typeof element.className === "string" ? element.className : "",
                id: element.id || "",
                role: element.getAttribute("role") || "",
                aria_live: element.getAttribute("aria-live") || "",
                has_button: Boolean(element.querySelector("button")),
                has_input: Boolean(element.querySelector("input,textarea,[contenteditable='true']")),
                child_text_same: childTextSame,
                bbox: box(element),
              };
            })
            .filter(item => item.text && item.text.length >= 2 && !item.has_input)
            .filter(item => !item.child_text_same || item.text.length < 80);
        }
        """
    )
    return blocks if isinstance(blocks, list) else []


def get_message_bubbles(page: Page) -> list[MessageBubble]:
    raw_blocks = dom_message_blocks(page)
    bubbles: list[MessageBubble] = []
    seen: set[str] = set()
    question = _LAST_SENT_QUESTION
    for raw in raw_blocks:
        text = str(raw.get("text", "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        class_name = str(raw.get("class", ""))
        bbox = raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None
        is_card = is_dom_action_card(raw) or is_ui_card_text(text)
        role = classify_message_role(text, class_name, question, is_card)
        if role == "ignore":
            continue
        bubbles.append(
            MessageBubble(
                index=int(raw.get("index", len(bubbles))),
                role=role,
                text=text,
                selector_hint=message_selector_hint(raw),
                class_name=class_name,
                bbox=bbox,
                is_action_card=is_card,
            )
        )
    return prune_message_bubbles(bubbles)


def classify_message_role(
    text: str, class_name: str, question: str | None, is_card: bool
) -> str:
    lowered_class = class_name.lower()
    if "space-y-4" in lowered_class and "p-4" in lowered_class:
        return "ignore"
    if is_welcome_screen_answer(text) or is_welcome_or_disclaimer_text(text):
        return "ignore"
    if is_card:
        return "action_card"
    if question and normalize_text(text) == normalize_text(question):
        return "user"
    if "flex-row-reverse" in lowered_class:
        return "user"
    if "bg-primary" in lowered_class and "text-primary-foreground" in lowered_class:
        return "user"
    if is_plausible_answer(clean_extracted_answer(text), question):
        return "assistant"
    return "ignore"


def is_welcome_or_disclaimer_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    markers = (
        "Добро пожаловать в MamaCare",
        "Я ваш помощник по вопросам послеродового периода",
        "Вы общаетесь с AI-помощником",
    )
    return any(marker in stripped for marker in markers)


def prune_message_bubbles(bubbles: list[MessageBubble]) -> list[MessageBubble]:
    pruned: list[MessageBubble] = []
    for bubble in bubbles:
        if any(
            existing.text != bubble.text and bubble.text in existing.text
            for existing in bubbles
            if existing.role == bubble.role and len(existing.text) > len(bubble.text)
        ):
            continue
        pruned.append(bubble)
    return pruned


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def is_dom_action_card(raw: dict[str, Any]) -> bool:
    text = str(raw.get("text", "")).strip()
    class_name = str(raw.get("class", "")).lower()
    has_button = bool(raw.get("has_button"))
    if any(marker in text for marker in WELCOME_SUGGESTION_MARKERS):
        return True
    if has_button and word_count(text) < 80 and any(marker in text for marker in CARD_BODY_MARKERS):
        return True
    if has_button and word_count(text) < 60 and any(cta in text for cta in CTA_WORDS):
        return True
    if word_count(text) < 60 and any(marker in text for marker in CARD_BODY_MARKERS):
        return True
    return "rounded-full" in class_name and word_count(text) < 20


def message_selector_hint(raw: dict[str, Any]) -> str:
    tag = str(raw.get("tag", ""))
    role = str(raw.get("role", ""))
    class_name = str(raw.get("class", ""))
    parts = [tag]
    if role:
        parts.append(f'[role="{role}"]')
    if class_name:
        parts.append("." + ".".join(class_name.split()[:4]))
    return "".join(parts)


def get_latest_user_message(page: Page) -> str | None:
    for bubble in reversed(get_message_bubbles(page)):
        if bubble.role == "user":
            return bubble.text
    return None


def get_latest_assistant_message(page: Page) -> str | None:
    for bubble in reversed(get_message_bubbles(page)):
        if bubble.role == "assistant" and not bubble.is_action_card:
            return clean_extracted_answer(bubble.text)
    return None


def get_action_cards(page: Page) -> list[str]:
    return [
        bubble.text
        for bubble in get_message_bubbles(page)
        if bubble.role == "action_card" or bubble.is_action_card
    ]


def is_generating(page: Page) -> bool:
    return generation_indicator_visible(page)


def save_page_screenshot(page: Page, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    page.screenshot(path=path, full_page=True)


def start_new_chat(
    page: Page, timeout_ms: int = 60_000, debug_id: str | None = None
) -> str:
    page.bring_to_front()
    try:
        page.keyboard.press("Escape")
    except Error:
        pass
    previous_text = page_text(page)

    clicked_real_button = False
    try:
        find_new_chat_button(page).click()
        clicked_real_button = True
    except RuntimeError:
        clicked_real_button = False

    if clicked_real_button or click_new_chat_by_text(page) or click_new_chat_by_selector(page):
        try:
            wait_for_chat_input(page, timeout_ms)
        except RuntimeError:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            wait_for_chat_input(page, timeout_ms)
    else:
        page.goto("https://mamacare.kaznu.kz/chat", wait_until="domcontentloaded")
        wait_for_chat_input(page, timeout_ms)

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except TimeoutError:
        pass

    try:
        page.keyboard.press("Escape")
    except Error:
        pass

    wait_for_chat_input(page, timeout_ms)
    if is_search_or_sidebar_focused(page):
        try:
            page.keyboard.press("Escape")
        except Error:
            pass
        wait_for_chat_input(page, timeout_ms)

    new_text = page_text(page)
    if old_messages_still_visible(previous_text, new_text):
        os.makedirs(os.path.dirname(NEW_CHAT_WARNING_PATH), exist_ok=True)
        with open(NEW_CHAT_WARNING_PATH, "w", encoding="utf-8") as warning_file:
            warning_file.write(
                "Old chat text may still be visible after starting a new chat.\n\n"
                "Before:\n"
                f"{previous_text}\n\nAfter:\n{new_text}"
            )
    if debug_id:
        save_page_screenshot(page, f"data/debug/{debug_id}_new_chat.png")
    return new_text


def is_search_or_sidebar_focused(page: Page) -> bool:
    try:
        active_text = page.evaluate(
            """
            () => {
              const element = document.activeElement;
              if (!element) return "";
              return [
                element.getAttribute("placeholder") || "",
                element.getAttribute("aria-label") || "",
                element.getAttribute("title") || "",
                element.getAttribute("name") || "",
                element.id || "",
                typeof element.className === "string" ? element.className : ""
              ].join(" ").toLowerCase();
            }
            """
        )
    except Error:
        return False
    return any(bad_word in str(active_text) for bad_word in INPUT_BAD_WORDS)


def click_new_chat_by_text(page: Page) -> bool:
    for text in NEW_CHAT_TEXTS:
        selectors = (
            f'button:has-text("{text}")',
            f'a:has-text("{text}")',
            f'[role="button"]:has-text("{text}")',
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    return True
    return False


def click_new_chat_by_selector(page: Page) -> bool:
    for selector in NEW_CHAT_SELECTORS:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click()
                return True
    return False


def old_messages_still_visible(previous_text: str, new_text: str) -> bool:
    previous_lines = [
        line.strip()
        for line in previous_text.splitlines()
        if len(line.strip()) >= 20 and line.strip() not in {"MamaCare"}
    ]
    if not previous_lines:
        return False
    return any(line in new_text for line in previous_lines[-5:])


def fill_chat_input(chat_input: Locator, question: str) -> None:
    try:
        chat_input.fill(question)
    except Error:
        chat_input.click()
        chat_input.press("Control+A")
        chat_input.type(question)


def submit_with_button(page: Page) -> bool:
    try:
        button = find_send_button(page)
        if button.is_visible() and button.is_enabled():
            button.click()
            return True
    except RuntimeError:
        pass

    for text in SEND_BUTTON_TEXTS:
        selectors = (
            f'button:has-text("{text}")',
            f'[role="button"]:has-text("{text}")',
            f'input[type="submit"][value="{text}"]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(locator.count()):
                button = locator.nth(index)
                if button.is_visible() and button.is_enabled():
                    button.click()
                    return True
    return False


def wait_until_text_changes(page: Page, before_text: str, timeout_ms: int) -> None:
    page.wait_for_function(
        """before => document.body && document.body.innerText !== before""",
        arg=before_text,
        timeout=timeout_ms,
    )


def send_question(
    page: Page,
    question: str,
    timeout_ms: int = 60_000,
    wait_config: WaitConfig | None = None,
    verbose: bool = False,
    debug_id: str | None = None,
) -> str:
    global _LAST_ACTION_CARD_TEXT, _LAST_ANSWER_WAIT_SECONDS, _LAST_ANSWER_WAIT_TIMED_OUT, _LAST_SENT_QUESTION, _LAST_STABLE_REAL_ANSWER_TEXT

    page.bring_to_front()
    _LAST_ANSWER_WAIT_TIMED_OUT = False
    _LAST_ANSWER_WAIT_SECONDS = 0.0
    _LAST_ACTION_CARD_TEXT = ""
    _LAST_STABLE_REAL_ANSWER_TEXT = ""
    wait_config = wait_config or WaitConfig()
    before_text = page_text(page)
    _LAST_SENT_QUESTION = question
    chat_input = find_message_input(page)
    chat_input.click()
    fill_chat_input(chat_input, question)
    verify_message_input_value(chat_input, question)
    before_submit_text = page_text(page)

    button_submitted = False
    try:
        button = find_send_button(page)
        if button.is_visible() and button.is_enabled():
            button.click()
            wait_until_text_changes(page, before_submit_text, 5_000)
            button_submitted = True
    except (Error, TimeoutError, RuntimeError):
        button_submitted = False

    enter_submitted = False
    if not button_submitted:
        try:
            chat_input.press("Enter")
            wait_until_text_changes(page, before_submit_text, 5_000)
            enter_submitted = True
        except TimeoutError:
            enter_submitted = False

    if not button_submitted and not enter_submitted:
        if not submit_with_button(page):
            raise RuntimeError(
                "Could not submit the MamaCare message using the real send button "
                "or Enter."
            )
        wait_until_text_changes(page, before_text, timeout_ms)

    wait_for_user_message(page, question, timeout_ms=timeout_ms)
    if debug_id:
        save_page_screenshot(page, f"data/debug/{debug_id}_after_send.png")

    if verbose:
        print("Question sent.")
        print("Waiting for answer...")

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except TimeoutError:
        pass

    stopped, elapsed = wait_until_answer_settles(page, wait_config)
    _LAST_ANSWER_WAIT_TIMED_OUT = not stopped
    _LAST_ANSWER_WAIT_SECONDS = elapsed
    if debug_id:
        save_page_screenshot(page, f"data/debug/{debug_id}_after_answer.png")
    return before_text


def verify_message_input_value(chat_input: Locator, question: str) -> None:
    try:
        value = chat_input.input_value(timeout=2_000)
    except Error:
        value = chat_input.evaluate(
            "element => element.value || element.innerText || element.textContent || ''"
        )
    if str(value) != question:
        raise RuntimeError("Message input value did not match the requested question.")


def wait_for_user_message(page: Page, question: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    normalized_question = normalize_text(question)
    while time.monotonic() < deadline:
        latest_user = get_latest_user_message(page)
        if latest_user and normalize_text(latest_user) == normalized_question:
            return
        if question in page_text(page):
            return
        page.wait_for_timeout(300)
    raise RuntimeError("Sent question did not appear as a user message in the chat.")


def wait_until_answer_starts(
    page: Page, before_text: str, question: str, timeout_ms: int
) -> None:
    page.wait_for_function(
        """
        ({ before, question }) => {
          const bodyText = document.body ? document.body.innerText : "";
          const afterQuestion = bodyText.split(question).pop() || "";
          const withoutChrome = afterQuestion
            .split("\\n")
            .map(line => line.trim())
            .filter(line => line && line !== "AI" && !/^\\d+\\s*[мm]$/i.test(line))
            .join("\\n")
            .trim();
          if (withoutChrome.length >= 12) {
            return true;
          }
          if (bodyText.startsWith(before)) {
            return bodyText.slice(before.length).trim().length >= question.length + 12;
          }
          return false;
        }
        """,
        arg={"before": before_text, "question": question},
        timeout=timeout_ms,
    )


def wait_until_answer_settles(page: Page, wait_config: WaitConfig) -> tuple[bool, float]:
    return wait_for_stable_answer(page, wait_config)


def wait_for_stable_answer(page: Page, wait_config: WaitConfig) -> tuple[bool, float]:
    global _LAST_ACTION_CARD_TEXT, _LAST_STABLE_REAL_ANSWER_TEXT

    started_at = time.monotonic()
    deadline = started_at + wait_config.max_wait
    last_text = ""
    stable_polls = 0
    incomplete_rounds_used = 0
    last_change_at = started_at
    while time.monotonic() < deadline:
        time.sleep(wait_config.poll_interval)

        current_text = get_latest_real_answer_text(page)
        card_text = get_latest_action_card_text(page)
        if card_text:
            _LAST_ACTION_CARD_TEXT = card_text

        elapsed = time.monotonic() - started_at
        if elapsed < wait_config.min_initial_wait:
            if current_text:
                last_text = current_text
                last_change_at = time.monotonic()
            continue

        generation_visible = generation_indicator_visible(page)
        if generation_visible:
            stable_polls = 0
            continue

        if current_text is None:
            stable_polls = 0
            continue

        if current_text == last_text:
            stable_polls += 1
            if stable_polls >= wait_config.stable_polls:
                if answer_still_looks_incomplete(
                    current_text,
                    recently_changed=(time.monotonic() - last_change_at)
                    <= max(wait_config.poll_interval * 2.0, 2.0),
                    generation_visible=generation_visible,
                    fast_mode=wait_config.fast_mode,
                ):
                    if incomplete_rounds_used < wait_config.max_incomplete_extra_rounds:
                        incomplete_rounds_used += 1
                        print("Answer looks incomplete, waiting extra...")
                        time.sleep(wait_config.incomplete_extra_wait)
                        stable_polls = 0
                        continue
                    _LAST_STABLE_REAL_ANSWER_TEXT = current_text
                    return False, time.monotonic() - started_at
                if current_text:
                    _LAST_STABLE_REAL_ANSWER_TEXT = current_text
                    return True, time.monotonic() - started_at
        else:
            stable_polls = 0
            last_text = current_text
            last_change_at = time.monotonic()

    final_text = get_latest_real_answer_text(page)
    if final_text:
        _LAST_STABLE_REAL_ANSWER_TEXT = final_text
    return False, time.monotonic() - started_at


def latest_answer_poll_text(page: Page) -> str:
    return get_latest_real_answer_text(page) or ""


def generation_indicator_visible(page: Page) -> bool:
    try:
        dom_generating = page.evaluate(
            """
            () => {
              const visible = element => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && rect.width > 0
                  && rect.height > 0;
              };
              const buttons = [...document.querySelectorAll("button")].filter(visible);
              if (buttons.some(button =>
                /stop|останов|стоп/i.test(
                  `${button.innerText || ""} ${button.getAttribute("aria-label") || ""} ${button.getAttribute("title") || ""}`
                )
              )) return true;
              if (buttons.some(button =>
                button.querySelector(".lucide-square, .lucide-circle-stop, .lucide-loader, .animate-spin")
              )) return true;
              return Boolean(document.querySelector(
                "[data-state='loading'], [aria-busy='true'], .animate-spin, [class*='spinner' i], [class*='loading' i]"
              ));
            }
            """
        )
        if bool(dom_generating):
            return True
    except Error:
        pass

    selectors = (
        'button:has-text("Stop")',
        'button:has-text("Остановить")',
        'button:has-text("Стоп")',
        '[aria-label*="stop" i]',
        '[aria-label*="loading" i]',
        '[data-state="loading"]',
        '[class*="spinner" i]',
        '[class*="loading" i]',
        '[class*="generat" i]',
        '[data-testid*="loading" i]',
    )
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            try:
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return True
            except Error:
                continue

    body_text = page_text(page).lower()
    if any(
        token in body_text
        for token in ("generating", "thinking", "loading", "печатает", "генерац", "загрузка")
    ):
        return True
    return False


def ends_like_incomplete_stream(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True

    if stripped.endswith(("...", "…")):
        return True

    words = re.findall(r"[\wА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ]+", stripped)
    if not words:
        return False

    last_lower = words[-1].lower()
    fragments = (
        "кеңес",
        "тексері",
        "консульт",
        "специал",
        "дәрігер",
        "маман",
        "provider",
        "doctor",
    )
    if any(last_lower.endswith(fragment) for fragment in fragments):
        return True

    dangling_words = {
        "с",
        "к",
        "по",
        "для",
        "и",
        "или",
        "что",
        "чтобы",
        "және",
        "мен",
        "үшін",
    }
    if last_lower in dangling_words:
        return True

    if len(words) >= 20 and len(last_lower) <= 3:
        return True

    return False


def answer_still_looks_incomplete(
    text: str,
    recently_changed: bool,
    generation_visible: bool,
    fast_mode: bool,
) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if generation_visible:
        return True
    if ends_like_incomplete_stream(stripped):
        return True
    if fast_mode:
        if recently_changed and not last_line_has_terminal_punctuation(stripped):
            return True
    elif not last_line_has_terminal_punctuation(stripped):
        return True
    return False


def last_line_has_terminal_punctuation(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last_line = lines[-1] if lines else text.strip()
    return bool(re.search(r"[.!?…)\]»]$", last_line))


def message_candidate_texts(page: Page) -> list[str]:
    dom_texts = [
        bubble.text
        for bubble in get_message_bubbles(page)
        if bubble.role in {"assistant", "action_card"}
    ]
    if dom_texts:
        return dom_texts

    selectors = (
        *ASSISTANT_MESSAGE_SELECTORS,
        '[class*="message" i]',
        '[class*="chat" i]',
        '[role="article"]',
    )
    texts: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                text = candidate.inner_text(timeout=1_000).strip()
            except Error:
                try:
                    text = (candidate.text_content(timeout=1_000) or "").strip()
                except Error:
                    continue
            if text and text not in seen:
                texts.append(text)
                seen.add(text)
    return texts


def get_latest_real_answer_text(page: Page) -> str | None:
    dom_answer = get_latest_assistant_message(page)
    if dom_answer and not is_ui_card_text(dom_answer):
        return dom_answer

    question = _LAST_SENT_QUESTION
    for text in reversed(message_candidate_texts(page)):
        if is_ui_card_text(text):
            continue
        answer = clean_answer_text(text, question)
        if is_plausible_answer(answer, question) and not is_ui_card_text(answer):
            return answer

    raw_answer = answer_after_latest_question(page_text(page), question)
    if is_ui_card_text(raw_answer):
        return None
    answer = clean_answer_text(raw_answer, question)
    if is_plausible_answer(answer, question) and not is_ui_card_text(answer):
        return answer
    return None


def get_latest_action_card_text(page: Page) -> str:
    cards = get_action_cards(page)
    if cards:
        return cards[-1]

    question = _LAST_SENT_QUESTION
    for text in reversed(message_candidate_texts(page)):
        if is_ui_card_text(text):
            return text
        answer = clean_answer_text(text, question)
        if answer and is_ui_card_text(answer):
            return answer

    raw_answer = answer_after_latest_question(page_text(page), question)
    if raw_answer and is_ui_card_text(raw_answer):
        return raw_answer
    answer = clean_answer_text(raw_answer, question)
    if answer and is_ui_card_text(answer):
        return answer
    return ""


def save_debug_text(text: str) -> None:
    os.makedirs(os.path.dirname(DEBUG_TEXT_PATH), exist_ok=True)
    with open(DEBUG_TEXT_PATH, "w", encoding="utf-8") as debug_file:
        debug_file.write(text)


def save_action_card_debug(record_id: str, text: str) -> None:
    path = f"data/debug/{record_id}_action_card.txt"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as debug_file:
        debug_file.write(text)


def write_json_debug(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as debug_file:
        json.dump(value, debug_file, ensure_ascii=False, indent=2)
        debug_file.write("\n")


def message_bubbles_debug(page: Page, selected_answer: str = "") -> dict[str, Any]:
    bubbles = get_message_bubbles(page)
    user_messages = [bubble.text for bubble in bubbles if bubble.role == "user"]
    assistant_messages = [
        clean_extracted_answer(bubble.text)
        for bubble in bubbles
        if bubble.role == "assistant" and not bubble.is_action_card
    ]
    action_cards = [
        bubble.text
        for bubble in bubbles
        if bubble.role == "action_card" or bubble.is_action_card
    ]
    return {
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "action_card_texts": action_cards,
        "selected_final_answer": selected_answer,
        "rejected_card_texts": action_cards,
        "bubbles": [
            {
                "index": bubble.index,
                "role": bubble.role,
                "text": bubble.text,
                "selector_hint": bubble.selector_hint,
                "class": bubble.class_name,
                "bbox": bubble.bbox,
                "is_action_card": bubble.is_action_card,
            }
            for bubble in bubbles
        ],
    }


def chat_state_debug(page: Page, selected_answer: str = "") -> dict[str, Any]:
    send_button = None
    try:
        button = find_send_button(page)
        send_button = {
            "selector": "textarea ancestor button with .lucide-send",
            "enabled": button.is_enabled(),
            "visible": button.is_visible(),
            "text": button.inner_text(timeout=1_000).strip(),
            "title": button.get_attribute("title") or "",
            "class": button.get_attribute("class") or "",
            "bbox": button.bounding_box(),
        }
    except (Error, RuntimeError):
        send_button = None

    input_info = None
    try:
        chat_input = find_message_input(page)
        input_info = {
            "selector": 'textarea[placeholder="Введите сообщение..."]',
            "visible": chat_input.is_visible(),
            "enabled": chat_input.is_enabled(),
            "placeholder": chat_input.get_attribute("placeholder") or "",
            "class": chat_input.get_attribute("class") or "",
            "bbox": chat_input.bounding_box(),
        }
    except (Error, RuntimeError):
        input_info = None

    messages = message_bubbles_debug(page, selected_answer=selected_answer)
    return {
        "message_input": input_info,
        "send_button": send_button,
        "is_generating": is_generating(page),
        "latest_user_message": get_latest_user_message(page),
        "latest_assistant_message": get_latest_assistant_message(page),
        "action_card_texts": messages["action_card_texts"],
        "message_bubble_count": len(messages["bubbles"]),
        "messages": messages,
    }


def save_record_debug_artifacts(page: Page, record_id: str, selected_answer: str = "") -> None:
    write_json_debug(
        f"data/debug/{record_id}_messages.json",
        message_bubbles_debug(page, selected_answer=selected_answer),
    )
    write_json_debug(
        f"data/debug/{record_id}_state.json",
        chat_state_debug(page, selected_answer=selected_answer),
    )


def clean_answer_text(text: str, question: str | None) -> str:
    cleaned = text.strip()
    if question and question in cleaned:
        cleaned = cleaned.rsplit(question, 1)[-1].strip()
    cleaned = clean_extracted_answer(cleaned)
    return cleaned


def clean_extracted_answer(text: str) -> str:
    return clean_extracted_answer_result(text)[0]


def clean_extracted_answer_result(text: str) -> tuple[str, bool]:
    cleaned = text.strip()
    if not cleaned:
        return "", False
    removed_card = False
    if _LAST_SENT_QUESTION and _LAST_SENT_QUESTION in cleaned:
        cleaned = cleaned.rsplit(_LAST_SENT_QUESTION, 1)[-1].strip()

    cleaned = remove_leading_speaker_labels(cleaned)

    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()

    lines, line_card_removed = remove_trailing_card_lines(lines)
    removed_card = removed_card or line_card_removed

    while lines and lines[-1].strip() in CTA_WORDS:
        removed_card = True
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()

    blocks = split_text_blocks("\n".join(lines))
    while len(blocks) > 1 and is_ui_card_text(blocks[0]):
        removed_card = True
        blocks.pop(0)
    while len(blocks) > 1 and is_trailing_card_block(blocks[-1]):
        removed_card = True
        blocks.pop()
    return "\n\n".join(block.strip() for block in blocks if block.strip()).strip(), removed_card


def remove_leading_speaker_labels(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].strip() in {"AI", "Assistant", "MamaCare"}:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def remove_trailing_card_lines(lines: list[str]) -> tuple[list[str], bool]:
    stripped_lines = [line.strip() for line in lines]
    trailing_starts = (
        "Уточним несколько деталей",
        "Уточним кормление малыша",
        "Несколько вопросов",
        "Открыть",
    )
    for index in range(len(stripped_lines)):
        line = stripped_lines[index]
        if any(line.startswith(prefix) for prefix in trailing_starts):
            trailing = "\n".join(stripped_lines[index:]).strip()
            if trailing and is_trailing_card_block(trailing):
                return lines[:index], True
    return lines, False


def is_trailing_card_block(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if is_ui_card_text(stripped):
        return True
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return False
    trailing_starts = (
        "Уточним несколько деталей",
        "Уточним кормление малыша",
        "Несколько вопросов",
        "Открыть",
    )
    if any(lines[0].startswith(prefix) for prefix in trailing_starts):
        return True
    return all(line in CTA_WORDS for line in lines)


def split_text_blocks(text: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]


def is_ui_card_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    words = word_count(stripped)
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    has_cta = any(cta in stripped for cta in CTA_WORDS)
    has_standalone_cta = any(line in CTA_WORDS for line in lines)
    has_known_card_title = any(title in stripped for title in KNOWN_CARD_TITLES)
    has_welcome_suggestion = any(
        marker in stripped for marker in WELCOME_SUGGESTION_MARKERS
    )
    has_card_body = any(marker in stripped for marker in CARD_BODY_MARKERS)

    if has_welcome_suggestion and words < 80:
        return True
    if "Уточним несколько деталей" in stripped and words < 60:
        return True
    if has_card_body and words < 50:
        return True
    if has_known_card_title and has_standalone_cta and words < 80:
        return True
    if words < 40 and has_cta:
        return True
    return False


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def raw_contains_ui_card(text: str) -> bool:
    if is_ui_card_text(text):
        return True
    return any(is_ui_card_text(block) for block in split_text_blocks(text))


def answer_after_latest_question(text: str, question: str | None) -> str:
    if not question or question not in text:
        return ""

    tail = text.rsplit(question, 1)[-1]
    lines = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "AI":
            continue
        if re.fullmatch(r"\d+\s*[мm]", stripped, flags=re.IGNORECASE):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def latest_assistant_text(page: Page) -> str | None:
    question = _LAST_SENT_QUESTION
    for selector in ASSISTANT_MESSAGE_SELECTORS:
        locator = page.locator(selector)
        texts: list[str] = []
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                try:
                    text = candidate.inner_text(timeout=2_000).strip()
                except Error:
                    text = (candidate.text_content(timeout=2_000) or "").strip()
                if text:
                    texts.append(text)
        if texts:
            answer = clean_answer_text(texts[-1], question)
            if is_plausible_answer(answer, question):
                return answer
    return None


def is_plausible_answer(text: str, question: str | None) -> bool:
    stripped = text.strip()
    if not stripped or stripped == question:
        return False
    if len(stripped) < 12:
        return False
    if re.fullmatch(r"\d+\s*[мm]", stripped, flags=re.IGNORECASE):
        return False
    return True


def text_added_after_before(before_text: str, after_text: str) -> str:
    if after_text.startswith(before_text):
        return after_text[len(before_text) :].strip()

    before_lines = [line.strip() for line in before_text.splitlines() if line.strip()]
    after_lines = [line.strip() for line in after_text.splitlines() if line.strip()]
    before_line_set = set(before_lines)
    new_lines = [line for line in after_lines if line not in before_line_set]
    return "\n".join(new_lines).strip()


def extract_latest_answer(page: Page, before_text: str) -> str:
    result = extract_latest_answer_result(page, before_text)
    if result.uncertain:
        raise RuntimeError(
            "Could not confidently extract the latest answer. Saved page text to "
            f"{DEBUG_TEXT_PATH}."
        )
    return result.answer


def extract_latest_answer_result(page: Page, before_text: str) -> ExtractedAnswer:
    after_text = page_text(page)
    if _LAST_STABLE_REAL_ANSWER_TEXT:
        return evaluate_extracted_text(_LAST_STABLE_REAL_ANSWER_TEXT, after_text)

    selector_answer = latest_assistant_text(page)
    if selector_answer:
        return evaluate_extracted_text(selector_answer, after_text)

    question = _LAST_SENT_QUESTION
    latest_question_answer = answer_after_latest_question(after_text, question)
    if is_plausible_answer(latest_question_answer, question):
        return evaluate_extracted_text(latest_question_answer, after_text)

    added_text = text_added_after_before(before_text, after_text)
    answer = clean_answer_text(added_text, question)
    if is_plausible_answer(answer, question):
        save_debug_text(after_text)
        result = evaluate_extracted_text(added_text, after_text)
        return ExtractedAnswer(
            answer=result.answer,
            uncertain=True,
            action_card_detected=result.action_card_detected,
            raw_text=result.raw_text,
        )

    save_debug_text(after_text)
    if _LAST_ACTION_CARD_TEXT:
        return ExtractedAnswer(
            answer="",
            uncertain=True,
            action_card_detected=True,
            raw_text=_LAST_ACTION_CARD_TEXT,
            truncated=False,
        )
    uncertain_answer = answer or latest_question_answer
    return evaluate_extracted_text(uncertain_answer, after_text, force_uncertain=True)


def evaluate_extracted_text(
    raw_text: str, page_debug_text: str, force_uncertain: bool = False
) -> ExtractedAnswer:
    original = raw_text.strip()
    action_card_detected = raw_contains_ui_card(original)
    cleaned, card_removed = clean_extracted_answer_result(original)

    if action_card_detected and (not cleaned or is_ui_card_text(cleaned)):
        save_debug_text(page_debug_text)
        return ExtractedAnswer(
            answer="",
            uncertain=True,
            action_card_detected=True,
            raw_text=original,
            truncated=False,
            card_removed_from_answer=False,
        )

    truncated = is_likely_truncated_answer(cleaned) or ends_like_incomplete_stream(cleaned)
    uncertain = force_uncertain or _LAST_ANSWER_WAIT_TIMED_OUT or truncated
    if card_removed:
        action_card_detected = True
    if uncertain or action_card_detected:
        save_debug_text(page_debug_text)
    return ExtractedAnswer(
        answer=cleaned,
        uncertain=uncertain,
        action_card_detected=action_card_detected,
        raw_text=original,
        truncated=truncated,
        card_removed_from_answer=card_removed,
    )


def answer_looks_complete(answer: str) -> bool:
    return not is_likely_truncated_answer(answer)


def is_likely_truncated_answer(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("...", "…")):
        return True
    if stripped[-1] not in ".!?…»\")]":
        return True

    last_token_match = re.search(r"([\wА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ]+)[^\wА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ]*$", stripped)
    last_token = last_token_match.group(1).lower() if last_token_match else ""
    dangling_words = {
        "с",
        "к",
        "по",
        "для",
        "и",
        "или",
        "что",
        "чтобы",
        "және",
        "мен",
        "үшін",
    }
    incomplete_fragments = (
        "вр",
        "консульт",
        "специал",
        "кеңес",
        "дәрігер",
        "маман",
    )
    if last_token in dangling_words:
        return True
    if any(last_token.endswith(fragment) for fragment in incomplete_fragments):
        return True

    final_sentence = re.split(r"[.!?…]+", stripped)[-1].strip()
    if len(stripped) > 120 and final_sentence and word_count(final_sentence) >= 8:
        return True
    return False


def is_welcome_screen_answer(answer: str) -> bool:
    lines = {line.strip() for line in answer.splitlines() if line.strip()}
    if not lines:
        return False
    contains_welcome_line = any(line in WELCOME_ONLY_LINES for line in lines)
    return lines.issubset(WELCOME_ONLY_LINES) or (
        contains_welcome_line and len(answer.strip()) < 220
    )


def connect_to_mamacare_page(timeout_ms: int) -> tuple[object, Browser, Page]:
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        match = find_mamacare_page(browser)
        page = match.page
        page.bring_to_front()
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        return playwright, browser, page
    except Exception:
        playwright.stop()
        raise


def attach_to_mamacare_tab(timeout_ms: int) -> tuple[str, str]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        try:
            match = find_mamacare_page(browser)
            page = match.page
            page.bring_to_front()
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return page.url, page.title()
        finally:
            browser.close()


def run_cdp_check(timeout_ms: int) -> None:
    url, title = attach_to_mamacare_tab(timeout_ms)
    print(f"Attached to MamaCare tab: {url}")
    print(f"Title: {title}")


def run_test_send(question: str, timeout_ms: int) -> None:
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        debug_id = "test_send"
        start_new_chat(page, timeout_ms=timeout_ms, debug_id=debug_id)
        before_text = send_question(
            page,
            question,
            timeout_ms=timeout_ms,
            wait_config=WaitConfig(),
            verbose=True,
            debug_id=debug_id,
        )
        result = extract_latest_answer_result(page, before_text)
        save_record_debug_artifacts(page, debug_id, selected_answer=result.answer)
        print(result.answer)
        if result.uncertain:
            print(
                f"Warning: extraction is uncertain; debug text saved to {DEBUG_TEXT_PATH}.",
                file=sys.stderr,
            )
    finally:
        browser.close()
        playwright.stop()


def run_debug_inputs(timeout_ms: int) -> None:
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        candidates = collect_input_candidates(page)
        accepted = [candidate for candidate in candidates if not candidate.rejected]
        selected = max(accepted, key=lambda candidate: candidate.score) if accepted else None
        for candidate in candidates:
            bbox = candidate.bbox or {}
            output = {
                "selector": candidate.selector,
                "index": candidate.index,
                "tag": candidate.tag,
                "placeholder": candidate.placeholder,
                "aria_label": candidate.aria_label,
                "class": candidate.class_name,
                "id": candidate.element_id,
                "bbox": {
                    "x": bbox.get("x"),
                    "y": bbox.get("y"),
                    "width": bbox.get("width"),
                    "height": bbox.get("height"),
                },
                "accepted": not candidate.rejected,
                "selected": candidate is selected,
                "rejected": candidate.rejected,
                "rejection_reason": candidate.rejection_reason,
                "in_main": candidate.in_main,
                "in_form": candidate.in_form,
                "in_chat_container": candidate.in_chat_container,
            }
            print(json.dumps(output, ensure_ascii=False))
        if not candidates:
            print("No input candidates found.")
    finally:
        browser.close()
        playwright.stop()


def run_debug_messages(timeout_ms: int) -> None:
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        texts = message_candidate_texts(page)
        real_answer = get_latest_real_answer_text(page)
        for index, text in enumerate(texts):
            cleaned = clean_answer_text(text, _LAST_SENT_QUESTION)
            output = {
                "index": index,
                "text_preview": cleaned.replace("\n", " ")[:220],
                "is_ui_card_text": is_ui_card_text(cleaned),
                "selected_as_real_answer": bool(real_answer and cleaned == real_answer),
            }
            print(json.dumps(output, ensure_ascii=False))
        if not texts:
            print("No visible message candidates found.")
    finally:
        browser.close()
        playwright.stop()


def run_inspect_dom(timeout_ms: int) -> None:
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        ensure_data_dirs()
        os.makedirs("data/debug", exist_ok=True)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as html_file:
            html_file.write(page.content())
        with open(DEBUG_BODY_PATH, "w", encoding="utf-8") as body_file:
            body_file.write(page_text(page))
        save_page_screenshot(page, DEBUG_SCREENSHOT_PATH)

        probe = dom_probe(page)
        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")
        print(f"Saved HTML: {DEBUG_HTML_PATH}")
        print(f"Saved body text: {DEBUG_BODY_PATH}")
        print(f"Saved screenshot: {DEBUG_SCREENSHOT_PATH}")
        print("Buttons:")
        for index, button in enumerate(probe.get("buttons", [])):
            print(
                json.dumps(
                    {
                        "index": index,
                        "text": button.get("text"),
                        "aria_label": button.get("aria_label"),
                        "title": button.get("title"),
                        "class": button.get("class"),
                        "id": button.get("id"),
                        "bbox": button.get("bbox"),
                        "visible": button.get("visible"),
                        "disabled": button.get("disabled"),
                    },
                    ensure_ascii=False,
                )
            )
        print("Inputs:")
        for item in probe.get("inputs", []):
            print(
                json.dumps(
                    {
                        "tag": item.get("tag"),
                        "placeholder": item.get("placeholder"),
                        "aria_label": item.get("aria_label"),
                        "title": item.get("title"),
                        "name": item.get("name"),
                        "class": item.get("class"),
                        "id": item.get("id"),
                        "bbox": item.get("bbox"),
                        "visible": item.get("visible"),
                        "enabled": not item.get("disabled"),
                    },
                    ensure_ascii=False,
                )
            )
        print("Candidate chat containers:")
        for item in probe.get("chat_containers", []):
            print(
                json.dumps(
                    {
                        "tag": item.get("tag"),
                        "role": item.get("role"),
                        "aria_live": item.get("aria_live"),
                        "class": item.get("class"),
                        "id": item.get("id"),
                        "bbox": item.get("bbox"),
                        "text_preview": str(item.get("text", "")).replace("\n", " ")[:240],
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        browser.close()
        playwright.stop()


def run_inspect_chat_state(timeout_ms: int) -> None:
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        state = chat_state_debug(page)
        print(json.dumps(state, ensure_ascii=False, indent=2))
    finally:
        browser.close()
        playwright.stop()


def run_import(input_path: str) -> None:
    ensure_data_dirs()
    if not os.path.exists(input_path):
        raise RuntimeError(f"Input file does not exist: {input_path}")

    with open(input_path, "r", encoding="utf-8") as input_file:
        try:
            raw_records = json.load(input_file)
        except JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON array in {input_path}: {exc}") from exc

    if not isinstance(raw_records, list):
        raise RuntimeError(f"{input_path} must contain a JSON array")

    queue_records = read_jsonl(QUEUE_PATH)
    rejected_records = read_jsonl(REJECTED_PATH)
    existing_ids = used_ids()
    existing_keys = existing_prompt_keys()
    imported = 0
    rejected = 0
    skipped = 0

    for index, raw_record in enumerate(raw_records, start=1):
        if isinstance(raw_record, dict):
            raw_key = (
                str(raw_record.get("language", "RU")).strip(),
                str(raw_record.get("topic", "general")).strip(),
                str(raw_record.get("question", "")).strip(),
            )
            if raw_key in existing_keys:
                print(f"Skipped duplicate: {raw_key[2]}")
                skipped += 1
                continue

        record, reason = validate_import_record(raw_record, existing_ids)
        if record is None:
            rejected_record = raw_record if isinstance(raw_record, dict) else {"raw": raw_record}
            rejected_record = dict(rejected_record)
            rejected_record["status"] = "rejected"
            rejected_record["rejection_reason"] = reason or "unknown rejection"
            rejected_record["source_index"] = index
            rejected_records.append(rejected_record)
            rejected += 1
        else:
            key = record_dedupe_key(record)
            existing_keys.add(key)
            queue_records.append(record)
            imported += 1

    atomic_write_jsonl(QUEUE_PATH, queue_records)
    atomic_write_jsonl(REJECTED_PATH, rejected_records)
    print(f"Imported: {imported}")
    print(f"Skipped duplicates: {skipped}")
    print(f"Rejected: {rejected}")
    print(f"Queue: {QUEUE_PATH}")
    print(f"Rejected: {REJECTED_PATH}")


def first_queued_record() -> dict[str, Any] | None:
    for record in read_jsonl(QUEUE_PATH):
        if record.get("status", "queued") == "queued":
            return record
    return None


def queued_records_in_order() -> list[dict[str, Any]]:
    return [
        record
        for record in read_jsonl(QUEUE_PATH)
        if record.get("status", "queued") == "queued"
    ]


def run_next() -> None:
    record = first_queued_record()
    if not record:
        print("No queued records.")
        return
    print(json.dumps(record, ensure_ascii=False))


def mark_queue_record(record_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    queue_records = read_jsonl(QUEUE_PATH)
    matching_indexes = [
        index for index, record in enumerate(queue_records) if record.get("id") == record_id
    ]
    found = bool(matching_indexes)
    preferred_indexes = [
        index
        for index in matching_indexes
        if queue_records[index].get("status", "queued") == "queued"
    ]
    for index in preferred_indexes or matching_indexes[:1]:
        record = queue_records[index]
        if record.get("id") == record_id:
            record["status"] = status
            if extra:
                record.update(extra)
            break
    if not found:
        raise RuntimeError(f"Record not found in queue: {record_id}")
    atomic_write_jsonl(QUEUE_PATH, queue_records)


def find_queue_record(record_id: str) -> dict[str, Any]:
    fallback = None
    for record in read_jsonl(QUEUE_PATH):
        if record.get("id") == record_id:
            if fallback is None:
                fallback = record
            if record.get("status", "queued") == "queued":
                return record
    if fallback is not None:
        return fallback
    raise RuntimeError(f"Record not found in queue: {record_id}")


def final_ids() -> set[str]:
    return {str(record.get("id")) for record in read_jsonl(FINAL_PATH) if record.get("id")}


def append_final_once(record: dict[str, Any]) -> None:
    if record.get("id") in final_ids():
        raise RuntimeError(f"Final dataset already contains id: {record.get('id')}")
    append_jsonl(FINAL_PATH, record)


def append_final_if_new(record: dict[str, Any]) -> bool:
    if record.get("id") in final_ids():
        return False
    append_jsonl(FINAL_PATH, record)
    return True


def append_or_replace_retryable_final(record: dict[str, Any]) -> None:
    records = read_jsonl(FINAL_PATH)
    record_id = record.get("id")
    for index, existing in enumerate(records):
        if existing.get("id") != record_id:
            continue
        existing_answer = str(existing.get("answer", "") or "").strip()
        retryable_existing = (
            existing.get("superseded")
            or (existing.get("action_card_detected") and not existing_answer)
            or not existing_answer
        )
        if retryable_existing:
            replacement = dict(record)
            if existing.get("raw_extracted_text") and "previous_raw_extracted_text" not in replacement:
                replacement["previous_raw_extracted_text"] = existing["raw_extracted_text"]
            records[index] = replacement
            atomic_write_jsonl(FINAL_PATH, records)
            return
        raise RuntimeError(f"Final dataset already contains id: {record_id}")
    append_jsonl(FINAL_PATH, record)


def final_record_from_queue(
    record: dict[str, Any],
    answer: str,
    status: str,
    uncertain: bool,
    action_card_detected: bool = False,
    raw_extracted_text: str = "",
    truncated_answer: bool = False,
    card_removed_from_answer: bool = False,
) -> dict[str, Any]:
    final_record = {
        "id": record["id"],
        "language": record.get("language", "RU"),
        "type": "single-turn",
        "source": record.get("source") or "synthetic_llm",
        "topic": record.get("topic") or "general",
        "question": record["question"],
        "answer": answer,
        "status": status,
    }
    if uncertain:
        final_record["extraction_uncertain"] = True
    if action_card_detected:
        final_record["action_card_detected"] = True
    if raw_extracted_text:
        final_record["raw_extracted_text"] = raw_extracted_text
    if truncated_answer:
        final_record["truncated_answer"] = True
    if card_removed_from_answer:
        final_record["card_removed_from_answer"] = True
    return final_record


def ask_record_once(
    page: Page,
    record: dict[str, Any],
    timeout_ms: int,
    wait_config: WaitConfig,
    verbose: bool = False,
) -> dict[str, Any]:
    if verbose:
        print(f"Processing {record['id']} / {record['question']}")
    start_new_chat(page, timeout_ms=timeout_ms, debug_id=str(record["id"]))
    if verbose:
        print("New chat created.")
    before_text = send_question(
        page,
        record["question"],
        timeout_ms=timeout_ms,
        wait_config=wait_config,
        verbose=verbose,
        debug_id=str(record["id"]),
    )
    result = extract_latest_answer_result(page, before_text)
    save_record_debug_artifacts(page, str(record["id"]), selected_answer=result.answer)
    if verbose:
        print(f"Answer stopped after {_LAST_ANSWER_WAIT_SECONDS:.1f} seconds.")
        print("Answer extracted.")
    status = "needs_review" if result.uncertain else "answered"
    return final_record_from_queue(
        record,
        result.answer,
        status,
        result.uncertain,
        action_card_detected=result.action_card_detected,
        raw_extracted_text=result.raw_text if result.action_card_detected else "",
        truncated_answer=result.truncated,
        card_removed_from_answer=result.card_removed_from_answer,
    )


def ask_record(
    page: Page,
    record: dict[str, Any],
    timeout_ms: int,
    wait_config: WaitConfig,
    retry_truncated: int = 0,
    retry_action_card: int = 1,
    verbose: bool = False,
) -> dict[str, Any]:
    best_record = ask_record_once(page, record, timeout_ms, wait_config, verbose=verbose)
    action_card_retries_left = retry_action_card
    while (
        best_record.get("action_card_detected")
        and not best_record.get("answer")
        and action_card_retries_left > 0
    ):
        action_card_retries_left -= 1
        if verbose:
            print(f"WARNING: action card only, retrying {record['id']} in a new chat.")
        best_record = ask_record_once(page, record, timeout_ms, wait_config, verbose=verbose)

    retries_left = retry_truncated
    while best_record.get("truncated_answer") and retries_left > 0:
        retries_left -= 1
        if verbose:
            print(f"WARNING: truncated answer, retrying {record['id']} in a new chat.")
        retry_record = ask_record_once(page, record, timeout_ms, wait_config, verbose=verbose)
        if len(str(retry_record.get("answer", ""))) > len(str(best_record.get("answer", ""))):
            best_record = retry_record
        if not retry_record.get("truncated_answer"):
            best_record = retry_record
            break
    return best_record


def process_record_by_id(record_id: str, timeout_ms: int) -> dict[str, Any]:
    record = find_queue_record(record_id)
    if record.get("status", "queued") != "queued":
        raise RuntimeError(f"Record {record_id} is not queued; status={record.get('status')}")
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        final_record = ask_record(page, record, timeout_ms, WaitConfig())
        if final_record.get("action_card_detected") and not final_record.get("answer"):
            save_action_card_debug(record_id, str(final_record.get("raw_extracted_text", "")))
        else:
            append_or_replace_retryable_final(final_record)
            mark_queue_record(record_id, "answered")
        return final_record
    finally:
        browser.close()
        playwright.stop()


def run_ask(record_id: str, timeout_ms: int) -> None:
    final_record = process_record_by_id(record_id, timeout_ms)
    print(json.dumps(final_record, ensure_ascii=False))


def run_ask_next(timeout_ms: int) -> None:
    record = first_queued_record()
    if not record:
        print("No queued records.")
        return
    run_ask(str(record["id"]), timeout_ms)


def run_batch(
    limit: int,
    timeout_ms: int,
    wait_config: WaitConfig | None = None,
    retry_truncated: int = 0,
    retry_action_card: int = 1,
) -> None:
    if limit < 1:
        raise RuntimeError("--limit must be at least 1")

    processed = 0
    wait_config = wait_config or WaitConfig()
    records_to_process = queued_records_in_order()[:limit]
    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        for record in records_to_process:
            record_id = str(record["id"])
            try:
                current_record = find_queue_record(record_id)
                if current_record.get("status", "queued") != "queued":
                    continue
                processed += 1
                final_record = ask_record(
                    page,
                    current_record,
                    timeout_ms,
                    wait_config,
                    retry_truncated=retry_truncated,
                    retry_action_card=retry_action_card,
                    verbose=True,
                )
                if final_record.get("action_card_detected") and not final_record.get("answer"):
                    save_action_card_debug(record_id, str(final_record.get("raw_extracted_text", "")))
                    append_error(f"{record_id}: action card detected; record remains queued")
                    print(
                        f"WARNING: {record_id} action card detected; record remains queued.",
                        file=sys.stderr,
                    )
                    continue
                append_or_replace_retryable_final(final_record)
                mark_queue_record(record_id, "answered")
                print("Saved to final.")
                if final_record.get("truncated_answer"):
                    print("WARNING: truncated answer, needs review.")
                if final_record.get("extraction_uncertain"):
                    print(f"WARNING: {record_id} needs review.")
            except Exception as exc:
                append_error(f"{record_id}: {exc}")
                print(
                    f"Error processing {record_id}; record remains queued: {exc}",
                    file=sys.stderr,
                )
            if processed < len(records_to_process):
                time.sleep(random.uniform(2, 5))
    finally:
        browser.close()
        playwright.stop()

    print(f"Processed: {processed}")


def count_records(path: str, status: str | None = None) -> int:
    records = read_jsonl(path)
    if status is None:
        return len(records)
    return sum(1 for record in records if record.get("status") == status)


def record_id_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    record_id = str(record.get("id", ""))
    match = re.search(r"(\d+)$", record_id)
    if match:
        return int(match.group(1)), record_id
    return sys.maxsize, record_id


def should_export_usable_record(record: dict[str, Any]) -> tuple[bool, str]:
    status = record.get("status")
    answer = str(record.get("answer", "") or "").strip()
    if status == "error":
        return False, "error status"
    if status not in {"answered", "needs_review"}:
        return False, "unsupported status"
    if not answer:
        return False, "empty answer"
    if record.get("action_card_detected") and not answer:
        return False, "empty action-card record"
    if is_ui_card_text(answer):
        return False, "answer is UI/action card"
    return True, ""


def clean_dataset_export_record(record: dict[str, Any]) -> dict[str, Any]:
    exported = {
        "id": record.get("id"),
        "language": record.get("language"),
        "type": record.get("type"),
        "source": record.get("source"),
        "topic": record.get("topic"),
        "question": record.get("question"),
        "answer": record.get("answer"),
        "status": record.get("status"),
    }
    if record.get("extraction_uncertain"):
        exported["extraction_uncertain"] = True
    if record.get("truncated_answer"):
        exported["truncated_answer"] = True
    return exported


def run_export_json(include_audit: bool = False) -> None:
    all_records = read_jsonl(FINAL_PATH)
    if include_audit:
        records = [
            record
            for record in all_records
            if record.get("status") in {"answered", "needs_review"}
            or record.get("action_card_detected")
            or record.get("raw_extracted_text")
        ]
        records.sort(key=record_id_sort_key)
        atomic_write_json(FINAL_AUDIT_JSON_PATH, records)
        print(f"Exported: {FINAL_AUDIT_JSON_PATH}")
        print(f"Audit count: {len(records)}")
        return

    records = []
    skipped = 0
    for record in all_records:
        should_export, reason = should_export_usable_record(record)
        if should_export:
            records.append(clean_dataset_export_record(record))
        elif reason in {"empty answer", "empty action-card record", "answer is UI/action card"}:
            skipped += 1

    records.sort(key=record_id_sort_key)
    atomic_write_json(FINAL_JSON_PATH, records)
    print(f"Exported: {FINAL_JSON_PATH}")
    print(f"Exported usable count: {len(records)}")
    print(f"Skipped empty/action-card count: {skipped}")


def suspicious_final_record(record: dict[str, Any]) -> tuple[bool, str]:
    answer = str(record.get("answer", "") or "")
    cleaned, card_removed = clean_extracted_answer_result(answer)
    if card_removed and cleaned:
        return True, "trailing UI/action card can be removed"
    if record.get("action_card_detected") and answer.strip() and not record.get("card_removed_from_answer"):
        return True, "action_card_detected=true"
    if is_ui_card_text(answer):
        return True, "answer is UI/action card text"
    if answer.strip() and not cleaned:
        return True, "answer cleans to empty after UI card removal"
    return False, ""


def run_validate_final(fix: bool = False) -> None:
    records = read_jsonl(FINAL_PATH)
    suspicious_count = 0
    fixed_records: list[dict[str, Any]] = []

    for record in records:
        is_suspicious, reason = suspicious_final_record(record)
        if is_suspicious:
            suspicious_count += 1
            preview = str(record.get("answer", "")).replace("\n", " ")[:160]
            print(
                json.dumps(
                    {
                        "id": record.get("id"),
                        "question": record.get("question"),
                        "answer_preview": preview,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )
            )
            if fix:
                old_answer = str(record.get("answer", "") or "")
                cleaned_answer, card_removed = clean_extracted_answer_result(old_answer)
                record = dict(record)
                if card_removed and cleaned_answer and not is_ui_card_text(cleaned_answer):
                    record["answer"] = cleaned_answer
                    record["action_card_detected"] = True
                    record["card_removed_from_answer"] = True
                    if record.get("status") not in {"answered", "needs_review"}:
                        record["status"] = "answered"
                    elif record.get("status") == "needs_review" and not record.get("extraction_uncertain"):
                        record["status"] = "answered"
                    if old_answer != cleaned_answer:
                        record["raw_extracted_text"] = old_answer
                else:
                    record["answer"] = ""
                    record["status"] = "needs_review"
                    record["extraction_uncertain"] = True
                    record["action_card_detected"] = True
                if old_answer and "raw_extracted_text" not in record:
                    record["raw_extracted_text"] = old_answer
        fixed_records.append(record)

    if fix:
        atomic_write_jsonl(FINAL_PATH, fixed_records)
        print(f"Fixed: {suspicious_count}")
    else:
        print(f"Suspicious: {suspicious_count}")


def should_remove_action_card_record(record: dict[str, Any]) -> tuple[bool, str]:
    answer = str(record.get("answer", "") or "").strip()
    if record.get("action_card_detected") and not answer:
        return True, "empty action-card record"
    if is_ui_card_text(answer):
        return True, "answer is UI/action card"
    if not answer:
        return True, "empty answer"
    return False, ""


def run_clean_final_jsonl(remove_action_cards: bool = False) -> None:
    if not remove_action_cards:
        raise RuntimeError("clean-final-jsonl requires --remove-action-cards")

    records = read_jsonl(FINAL_PATH)
    os.makedirs(os.path.dirname(FINAL_BACKUP_PATH), exist_ok=True)
    if os.path.exists(FINAL_PATH):
        with open(FINAL_PATH, "r", encoding="utf-8") as source_file:
            original_text = source_file.read()
        with open(FINAL_BACKUP_PATH, "w", encoding="utf-8") as backup_file:
            backup_file.write(original_text)

    kept_records = []
    removed = 0
    for record in records:
        should_remove, _reason = should_remove_action_card_record(record)
        if should_remove:
            removed += 1
        else:
            kept_records.append(record)

    atomic_write_jsonl(FINAL_PATH, kept_records)
    print(f"Backup: {FINAL_BACKUP_PATH}")
    print(f"Removed: {removed}")
    print(f"Kept: {len(kept_records)}")


def run_requeue_needs_review() -> None:
    final_records = read_jsonl(FINAL_PATH)
    queue_records = read_jsonl(QUEUE_PATH)
    queued_ids = {
        str(record.get("id"))
        for record in queue_records
        if record.get("status", "queued") == "queued"
    }
    requeued = 0
    updated_final_records: list[dict[str, Any]] = []

    for record in final_records:
        is_suspicious, _reason = suspicious_final_record(record)
        should_requeue = (
            is_suspicious
            or not str(record.get("answer", "") or "").strip()
            or record.get("action_card_detected") is True
        )
        record_id = str(record.get("id", ""))
        if should_requeue and record_id and record_id not in queued_ids:
            queue_records.append(
                {
                    "id": record_id,
                    "language": record.get("language", "RU"),
                    "type": "single-turn",
                    "source": record.get("source") or "synthetic_llm",
                    "topic": record.get("topic") or "general",
                    "question": record.get("question", ""),
                    "answer": "",
                    "status": "queued",
                }
            )
            queued_ids.add(record_id)
            record = dict(record)
            record["superseded"] = True
            requeued += 1
        updated_final_records.append(record)

    atomic_write_jsonl(QUEUE_PATH, queue_records)
    atomic_write_jsonl(FINAL_PATH, updated_final_records)
    print(f"Requeued: {requeued}")


def find_record_by_id(record_id: str) -> dict[str, Any]:
    for record in read_jsonl(FINAL_PATH):
        if record.get("id") == record_id:
            return record
    for record in read_jsonl(QUEUE_PATH):
        if record.get("id") == record_id:
            return record
    raise RuntimeError(f"Record not found in final or queue: {record_id}")


def queue_has_record_id(record_id: str) -> bool:
    return any(record.get("id") == record_id for record in read_jsonl(QUEUE_PATH))


def queue_has_queued_record_id(record_id: str) -> bool:
    return any(
        record.get("id") == record_id and record.get("status", "queued") == "queued"
        for record in read_jsonl(QUEUE_PATH)
    )


def replace_final_record(record_id: str, replacement: dict[str, Any]) -> None:
    records = read_jsonl(FINAL_PATH)
    for index, record in enumerate(records):
        if record.get("id") == record_id:
            records[index] = replacement
            atomic_write_jsonl(FINAL_PATH, records)
            return
    append_jsonl(FINAL_PATH, replacement)


def build_queue_record_from_source(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "language": record.get("language", "RU"),
        "type": "single-turn",
        "source": record.get("source") or "synthetic_llm",
        "topic": record.get("topic") or "general",
        "question": record.get("question", ""),
        "answer": "",
        "status": "queued",
    }


def run_requeue_id(record_id: str) -> None:
    if queue_has_queued_record_id(record_id):
        print(f"Already queued: {record_id}")
        return
    source = find_record_by_id(record_id)
    queue_records = read_jsonl(QUEUE_PATH)
    queue_records.append(build_queue_record_from_source(source))
    atomic_write_jsonl(QUEUE_PATH, queue_records)
    print(f"Requeued: {record_id}")


def run_retry_id(
    record_id: str,
    timeout_ms: int,
    wait_config: WaitConfig,
    replace: bool = False,
    retry_truncated: int = 0,
    retry_action_card: int = 1,
) -> None:
    source = find_record_by_id(record_id)
    if not str(source.get("question", "")).strip():
        raise RuntimeError(f"Record has no question: {record_id}")

    playwright, browser, page = connect_to_mamacare_page(timeout_ms)
    try:
        result = ask_record(
            page,
            source,
            timeout_ms,
            wait_config,
            retry_truncated=retry_truncated,
            retry_action_card=retry_action_card,
            verbose=True,
        )
        result = dict(result)
        result["retry_of"] = record_id
        if result.get("action_card_detected") and not result.get("answer"):
            save_action_card_debug(record_id, str(result.get("raw_extracted_text", "")))
            print(f"WARNING: {record_id} action-card only; kept queued.")
            return

        if replace:
            replace_final_record(record_id, result)
            print(f"Replaced final record: {record_id}")
        else:
            append_jsonl(FINAL_PATH, result)
            print(f"Appended retry record: {record_id}")

        if queue_has_queued_record_id(record_id):
            mark_queue_record(record_id, "answered")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        browser.close()
        playwright.stop()


def run_batch_from_raw(
    input_path: str,
    limit: int,
    timeout_ms: int,
    wait_config: WaitConfig | None = None,
    retry_truncated: int = 0,
    retry_action_card: int = 1,
) -> None:
    run_import(input_path)
    run_batch(
        limit,
        timeout_ms,
        wait_config=wait_config,
        retry_truncated=retry_truncated,
        retry_action_card=retry_action_card,
    )
    run_export_json()


def run_stats() -> None:
    queue_records = read_jsonl(QUEUE_PATH)
    final_records = read_jsonl(FINAL_PATH)
    rejected_records = read_jsonl(REJECTED_PATH)

    queued_count = sum(1 for record in queue_records if record.get("status", "queued") == "queued")
    answered_count = sum(1 for record in final_records if record.get("status") == "answered")
    needs_review_count = sum(1 for record in final_records if record.get("status") == "needs_review")
    rejected_count = sum(1 for record in rejected_records if record.get("status") == "rejected")
    error_count = sum(1 for record in queue_records if record.get("status") == "error") + sum(
        1 for record in final_records if record.get("status") == "error"
    )
    if os.path.exists(ERROR_LOG_PATH):
        with open(ERROR_LOG_PATH, "r", encoding="utf-8") as error_file:
            error_count += sum(1 for line in error_file if line.strip())

    print(f"queued count: {queued_count}")
    print(f"answered count: {answered_count}")
    print(f"needs_review count: {needs_review_count}")
    print(f"rejected count: {rejected_count}")
    print(f"error count: {error_count}")


def run_validate_truncated() -> None:
    for record in read_jsonl(FINAL_PATH):
        answer = str(record.get("answer", "") or "")
        truncated = bool(record.get("truncated_answer")) or is_likely_truncated_answer(answer)
        if truncated:
            ending = answer[-80:].replace("\n", " ")
            print(
                json.dumps(
                    {
                        "id": record.get("id"),
                        "language": record.get("language"),
                        "question_preview": str(record.get("question", ""))[:120],
                        "answer_ending": ending,
                        "truncated_answer": truncated,
                        "stored_truncated_answer": record.get("truncated_answer", False),
                    },
                    ensure_ascii=False,
                )
            )


def add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-wait", type=float, default=None, help="Max answer wait in seconds.")
    parser.add_argument("--stable-polls", type=int, default=None, help="Stable polls before stopping.")
    parser.add_argument("--poll-interval", type=float, default=None, help="Answer poll interval in seconds.")
    parser.add_argument("--min-initial-wait", type=float, default=None, help="Minimum seconds to wait before accepting a stable answer.")
    parser.add_argument("--fast", action="store_true", help="Use fast answer-wait settings.")
    parser.add_argument("--retry-truncated", type=int, default=0, help="Retry truncated answers this many times.")
    parser.add_argument("--retry-action-card", type=int, default=1, help="Retry card-only answers this many times.")


def wait_config_from_args(args: argparse.Namespace) -> WaitConfig:
    if getattr(args, "fast", False):
        max_wait = FAST_MAX_WAIT_SECONDS
        stable_polls = FAST_STABLE_POLLS
        poll_interval = FAST_POLL_INTERVAL_SECONDS
        min_initial_wait = FAST_MIN_INITIAL_WAIT_SECONDS
        incomplete_extra_wait = FAST_INCOMPLETE_EXTRA_WAIT_SECONDS
        max_incomplete_extra_rounds = FAST_MAX_INCOMPLETE_EXTRA_ROUNDS
    else:
        max_wait = DEFAULT_MAX_WAIT_SECONDS
        stable_polls = DEFAULT_STABLE_POLLS
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
        min_initial_wait = DEFAULT_MIN_INITIAL_WAIT_SECONDS
        incomplete_extra_wait = DEFAULT_INCOMPLETE_EXTRA_WAIT_SECONDS
        max_incomplete_extra_rounds = DEFAULT_MAX_INCOMPLETE_EXTRA_ROUNDS

    if getattr(args, "max_wait", None) is not None:
        max_wait = args.max_wait
    if getattr(args, "stable_polls", None) is not None:
        stable_polls = args.stable_polls
    if getattr(args, "poll_interval", None) is not None:
        poll_interval = args.poll_interval
    if getattr(args, "min_initial_wait", None) is not None:
        min_initial_wait = args.min_initial_wait

    return WaitConfig(
        max_wait=max_wait,
        stable_polls=max(1, stable_polls),
        poll_interval=max(0.1, poll_interval),
        min_initial_wait=max(0.0, min_initial_wait),
        incomplete_extra_wait=max(0.0, incomplete_extra_wait),
        max_incomplete_extra_rounds=max(0, max_incomplete_extra_rounds),
        fast_mode=bool(getattr(args, "fast", False)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to an existing Chromium CDP session and use only the "
            "tab whose URL contains mamacare.kaznu.kz."
        )
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=90_000,
        help="Timeout for page and answer waits.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("cdp-check", help="Verify the existing MamaCare CDP tab.")
    subparsers.add_parser(
        "debug-inputs",
        help="Print visible/enabled input candidates and strict rejection reasons.",
    )
    subparsers.add_parser(
        "debug-messages",
        help="Print visible message candidates and UI-card classification.",
    )
    subparsers.add_parser(
        "inspect-dom",
        help="Save and print real MamaCare DOM controls and chat containers.",
    )
    subparsers.add_parser(
        "inspect-chat-state",
        help="Print DOM-based input, send, generation, message, and card state.",
    )
    import_parser = subparsers.add_parser(
        "import",
        help="Import a JSON array of generated single-turn questions into the queue.",
    )
    import_parser.add_argument(
        "input_path",
        nargs="?",
        default=RAW_GENERATED_PATH,
        help=f"JSON array input path. Default: {RAW_GENERATED_PATH}",
    )
    subparsers.add_parser("next", help="Print the next queued record.")
    ask_parser = subparsers.add_parser(
        "ask",
        help="Process one queued record by id.",
    )
    ask_parser.add_argument("record_id", help="Queue record id, for example single_0001.")
    subparsers.add_parser("ask-next", help="Process the first queued record.")
    batch_parser = subparsers.add_parser(
        "batch",
        help="Process up to N queued records.",
    )
    batch_parser.add_argument("--limit", type=int, default=10, help="Maximum records to process.")
    add_wait_options(batch_parser)
    batch_from_raw_parser = subparsers.add_parser(
        "batch-from-raw",
        help="Import raw generated questions, run batch, then export final JSON.",
    )
    batch_from_raw_parser.add_argument(
        "input_path",
        nargs="?",
        default=RAW_GENERATED_PATH,
        help=f"JSON array input path. Default: {RAW_GENERATED_PATH}",
    )
    batch_from_raw_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum queued records to process after import.",
    )
    add_wait_options(batch_from_raw_parser)
    export_json_parser = subparsers.add_parser(
        "export-json",
        help="Convert final JSONL records into ordered pretty JSON.",
    )
    export_json_parser.add_argument(
        "--include-audit",
        action="store_true",
        help=f"Export audit JSON to {FINAL_AUDIT_JSON_PATH} instead of usable-only JSON.",
    )
    validate_final_parser = subparsers.add_parser(
        "validate-final",
        help="Print final records whose answers look like UI/action cards.",
    )
    validate_final_parser.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite suspicious final records as needs_review action-card records.",
    )
    retry_id_parser = subparsers.add_parser(
        "retry-id",
        help="Retry a question by record id using the original question text.",
    )
    retry_id_parser.add_argument("record_id", help="Record id to retry.")
    retry_id_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace the existing final record instead of appending a retry row.",
    )
    add_wait_options(retry_id_parser)
    subparsers.add_parser(
        "requeue-id",
        help="Put a question back into the queue by record id.",
    ).add_argument("record_id", help="Record id to requeue.")
    subparsers.add_parser(
        "requeue-needs-review",
        help="Move action-card or empty-answer final records back into the queue.",
    )
    clean_final_parser = subparsers.add_parser(
        "clean-final-jsonl",
        help="Rewrite final JSONL after removing unusable action-card/empty records.",
    )
    clean_final_parser.add_argument(
        "--remove-action-cards",
        action="store_true",
        help="Remove empty/action-card-only final records after creating a backup.",
    )
    subparsers.add_parser("stats", help="Print queue/final/rejected counts.")
    subparsers.add_parser(
        "validate-truncated",
        help="Print final records whose answer appears truncated.",
    )
    test_send_parser = subparsers.add_parser(
        "test-send",
        help="Send one question to the MamaCare chat and print the extracted answer.",
    )
    test_send_parser.add_argument("question", help="Question text to send.")
    args = parser.parse_args()

    try:
        if args.command in (None, "cdp-check"):
            run_cdp_check(args.timeout_ms)
        elif args.command == "debug-inputs":
            run_debug_inputs(args.timeout_ms)
        elif args.command == "debug-messages":
            run_debug_messages(args.timeout_ms)
        elif args.command == "inspect-dom":
            run_inspect_dom(args.timeout_ms)
        elif args.command == "inspect-chat-state":
            run_inspect_chat_state(args.timeout_ms)
        elif args.command == "import":
            run_import(args.input_path)
        elif args.command == "next":
            run_next()
        elif args.command == "ask":
            run_ask(args.record_id, args.timeout_ms)
        elif args.command == "ask-next":
            run_ask_next(args.timeout_ms)
        elif args.command == "batch":
            run_batch(
                args.limit,
                args.timeout_ms,
                wait_config=wait_config_from_args(args),
                retry_truncated=args.retry_truncated,
                retry_action_card=args.retry_action_card,
            )
        elif args.command == "batch-from-raw":
            run_batch_from_raw(
                args.input_path,
                args.limit,
                args.timeout_ms,
                wait_config=wait_config_from_args(args),
                retry_truncated=args.retry_truncated,
                retry_action_card=args.retry_action_card,
            )
        elif args.command == "export-json":
            run_export_json(include_audit=args.include_audit)
        elif args.command == "validate-final":
            run_validate_final(fix=args.fix)
        elif args.command == "retry-id":
            run_retry_id(
                args.record_id,
                args.timeout_ms,
                wait_config_from_args(args),
                replace=args.replace,
                retry_truncated=args.retry_truncated,
                retry_action_card=args.retry_action_card,
            )
        elif args.command == "requeue-id":
            run_requeue_id(args.record_id)
        elif args.command == "requeue-needs-review":
            run_requeue_needs_review()
        elif args.command == "clean-final-jsonl":
            run_clean_final_jsonl(remove_action_cards=args.remove_action_cards)
        elif args.command == "stats":
            run_stats()
        elif args.command == "validate-truncated":
            run_validate_truncated()
        elif args.command == "test-send":
            run_test_send(args.question, args.timeout_ms)
        else:
            parser.error(f"Unknown command: {args.command}")
    except (Error, TimeoutError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
