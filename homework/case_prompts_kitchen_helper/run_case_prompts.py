"""
Учебный скрипт: прогон трёх case prompts через OpenAI API
и сохранение практических артефактов для проекта Kitchen Helper.

Запуск из корня проекта:
  python homework/case_prompts_kitchen_helper/run_case_prompts.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from openai import OpenAI

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
OUTPUTS_DIR = SCRIPT_DIR / "outputs"

MODEL = "gpt-4o-mini"
TEMPERATURE = 0.2

PROMPTS = [
    ("01_summary_prompt.json", "01_product_brief.md"),
    ("02_code_structure_prompt.json", "02_architecture_proposal.md"),
    ("03_task_planning_prompt.json", "03_beta_roadmap.md"),
]

SEP_MAJOR = "=" * 80
SEP_MINOR = "-" * 80
REQUIRED_FIELDS = {
    "role",
    "context",
    "format",
    "test_input",
    "expected_test_output_description",
}


def load_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")

    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        print(
            "Ошибка: OPENAI_API_KEY не найден.\n"
            f"Создайте файл {PROJECT_ROOT / '.env'} и укажите OPENAI_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def read_prompt(path: Path) -> dict[str, Any]:
    if not path.is_file():
        print(f"Ошибка: файл промпта не найден: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        prompt_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Ошибка: невалидный JSON в {path.name}: {exc}", file=sys.stderr)
        sys.exit(1)

    missing = sorted(REQUIRED_FIELDS - set(prompt_data))
    if missing:
        print(
            f"Ошибка: в {path.name} отсутствуют обязательные поля: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    return prompt_data


def build_user_message(prompt_data: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            "Контекст:",
            str(prompt_data["context"]),
            "Формат ответа:",
            str(prompt_data["format"]),
            "Тестовый ввод:",
            str(prompt_data["test_input"]),
            "Ожидаемое описание результата:",
            str(prompt_data["expected_test_output_description"]),
        ]
    )


def format_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def call_openai(client: OpenAI, prompt_data: dict[str, Any]) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        messages=[
            {
                "role": "system",
                "content": str(prompt_data["role"]),
            },
            {
                "role": "user",
                "content": build_user_message(prompt_data),
            },
        ],
    )
    return (response.choices[0].message.content or "").strip()


def print_prompt_result(prompt_file: str, output_file: str, response_text: str) -> None:
    print(SEP_MAJOR)
    print(f"PROMPT FILE: {prompt_file}")
    print(SEP_MAJOR)
    print()
    print(f"REAL PROJECT OUTPUT: outputs/{output_file}")
    print(SEP_MINOR)
    print(response_text)
    print()


def write_run_transcript(blocks: list[dict[str, str]]) -> None:
    lines = [
        "# Run transcript — Kitchen Helper case prompts",
        "",
        "## Параметры запуска",
        "",
        "| Параметр | Значение |",
        "|----------|----------|",
        "| Инструмент | Python script from Cursor terminal |",
        f"| Model | {MODEL} |",
        f"| Temperature | {TEMPERATURE} |",
        "| Response format | Markdown |",
        "| Output files | `outputs/01_product_brief.md`, `outputs/02_architecture_proposal.md`, `outputs/03_beta_roadmap.md` |",
        "",
    ]

    for block in blocks:
        lines.extend(
            [
                SEP_MAJOR,
                f"PROMPT FILE: {block['prompt_file']}",
                f"REAL PROJECT OUTPUT: outputs/{block['output_file']}",
                SEP_MAJOR,
                "",
                "## FULL PROMPT JSON",
                "",
                "```json",
                block["prompt_json"],
                "```",
                "",
                "## MODEL USER MESSAGE",
                "",
                block["user_message"],
                "",
                "## TEST INPUT",
                "",
                block["test_input"],
                "",
                "## REAL PROJECT OUTPUT",
                "",
                block["model_response"],
                "",
            ]
        )

    (SCRIPT_DIR / "run_transcript.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    api_key = load_api_key()
    client = OpenAI(api_key=api_key)
    OUTPUTS_DIR.mkdir(exist_ok=True)

    transcript_blocks: list[dict[str, str]] = []

    for prompt_file, response_file in PROMPTS:
        prompt_data = read_prompt(SCRIPT_DIR / prompt_file)
        user_message = build_user_message(prompt_data)
        response_text = call_openai(client, prompt_data)

        output_path = OUTPUTS_DIR / response_file
        output_path.write_text(response_text + "\n", encoding="utf-8")

        test_input = str(prompt_data["test_input"])
        print_prompt_result(prompt_file, response_file, response_text)

        transcript_blocks.append(
            {
                "prompt_file": prompt_file,
                "output_file": response_file,
                "prompt_json": format_json(prompt_data),
                "user_message": user_message,
                "test_input": test_input,
                "model_response": response_text,
            }
        )

    write_run_transcript(transcript_blocks)
    print(f"Готово. Ответы сохранены в: {OUTPUTS_DIR}")
    print(f"Транскрипт: {SCRIPT_DIR / 'run_transcript.md'}")


if __name__ == "__main__":
    main()
