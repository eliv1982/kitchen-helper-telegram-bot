"""
Учебный скрипт: прогон prompt_v1 и prompt_v2 через OpenAI API.
Запуск из корня проекта:
  python homework/prompt_json_kitchen_helper/run_prompts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]

MODEL = "gpt-4o-mini"
TEMPERATURE = 0.2
MAX_TOKENS = 600

PROMPTS = [
    ("prompt_v1.txt", "response_v1.json"),
    ("prompt_v2.txt", "response_v2.json"),
]

SEP_MAJOR = "=" * 80
SEP_MINOR = "-" * 80


def load_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    import os

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        print(
            "Ошибка: OPENAI_API_KEY не найден.\n"
            f"Создайте файл {PROJECT_ROOT / '.env'} "
            "(можно скопировать из .env.example) и укажите OPENAI_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def read_prompt(filename: str) -> str:
    path = SCRIPT_DIR / filename
    if not path.is_file():
        print(f"Ошибка: файл промпта не найден: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text(encoding="utf-8").strip()


def format_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def call_openai(client: OpenAI, prompt: str) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    return (response.choices[0].message.content or "").strip()


def parse_json_response(raw: str, label: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:500] + ("..." if len(raw) > 500 else "")
        print(
            f"Ошибка: ответ модели для {label} — невалидный JSON.\n"
            f"Причина: {exc}\n"
            f"Фрагмент ответа:\n{preview}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(data, dict):
        print(
            f"Ошибка: ответ для {label} должен быть JSON-объектом, "
            f"получен тип {type(data).__name__}.",
            file=sys.stderr,
        )
        sys.exit(1)

    for field in ("title", "steps", "notes"):
        if field not in data:
            print(
                f"Ошибка: в JSON для {label} отсутствует обязательное поле '{field}'.",
                file=sys.stderr,
            )
            sys.exit(1)

    return data


def save_json(path: Path, data: dict) -> None:
    path.write_text(format_json(data) + "\n", encoding="utf-8")


def print_transcript_block(prompt_file: str, prompt_text: str, response_file: str, data: dict) -> None:
    print(SEP_MAJOR)
    print(f"PROMPT FILE: {prompt_file}")
    print(SEP_MAJOR)
    print()
    print(prompt_text)
    print()
    print(SEP_MINOR)
    print(f"MODEL RESPONSE -> {response_file}")
    print(SEP_MINOR)
    print()
    print(format_json(data))
    print()


def write_run_log(results: list[dict]) -> None:
    lines = [
        "# Run log — prompt JSON Kitchen Helper",
        "",
        "## Параметры запуска",
        "",
        "| Параметр | Значение |",
        "|----------|----------|",
        "| Инструмент | Python script from Cursor terminal |",
        f"| Model | {MODEL} |",
        f"| Temperature | {TEMPERATURE} |",
        f"| Max tokens | {MAX_TOKENS} |",
        "| Response format | json_object |",
        "",
        "## Файлы",
        "",
        "| Промпт | Ответ |",
        "|--------|-------|",
    ]
    for row in results:
        lines.append(f"| {row['prompt']} | {row['response']} |")

    lines.extend(
        [
            "",
            "## Дополнительные артефакты",
            "",
            "- [run_transcript.md](./run_transcript.md) — полный транскрипт «промпт → ответ»",
            "",
            "## JSON validation",
            "",
        ]
    )
    for row in results:
        lines.append(f"- **{row['label']}**: {row['status']}")

    (SCRIPT_DIR / "run_log.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_transcript(blocks: list[dict]) -> None:
    lines = [
        "# Run transcript — prompt → response",
        "",
        "## Параметры запуска",
        "",
        "| Параметр | Значение |",
        "|----------|----------|",
        "| Инструмент | Python script from Cursor terminal |",
        f"| Model | {MODEL} |",
        f"| Temperature | {TEMPERATURE} |",
        f"| Max tokens | {MAX_TOKENS} |",
        "| Response format | json_object |",
        "",
    ]

    for block in blocks:
        lines.extend(
            [
                SEP_MAJOR,
                f"PROMPT FILE: {block['prompt_file']}",
                SEP_MAJOR,
                "",
                block["prompt_text"],
                "",
                SEP_MINOR,
                f"MODEL RESPONSE -> {block['response_file']}",
                SEP_MINOR,
                "",
                "```json",
                block["response_json"],
                "```",
                "",
            ]
        )

    (SCRIPT_DIR / "run_transcript.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    api_key = load_api_key()
    client = OpenAI(api_key=api_key)
    results: list[dict] = []
    transcript_blocks: list[dict] = []

    for prompt_file, response_file in PROMPTS:
        label = prompt_file.replace(".txt", "")
        prompt_text = read_prompt(prompt_file)
        raw = call_openai(client, prompt_text)
        data = parse_json_response(raw, label)
        out_path = SCRIPT_DIR / response_file
        save_json(out_path, data)
        print_transcript_block(prompt_file, prompt_text, response_file, data)

        results.append(
            {
                "prompt": prompt_file,
                "response": response_file,
                "label": label,
                "status": "valid JSON",
            }
        )
        transcript_blocks.append(
            {
                "prompt_file": prompt_file,
                "prompt_text": prompt_text,
                "response_file": response_file,
                "response_json": format_json(data),
            }
        )

    write_run_log(results)
    write_run_transcript(transcript_blocks)
    print(f"Готово. Лог: {SCRIPT_DIR / 'run_log.md'}")
    print(f"Транскрипт: {SCRIPT_DIR / 'run_transcript.md'}")


if __name__ == "__main__":
    main()
