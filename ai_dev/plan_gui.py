"""Public desktop entry point. Importing CLI helpers does not import Qt."""

from .gui_text import (
    TEXT,
    LANGUAGE_LABELS,
    chatgpt_plan_prompt,
    preferred_language,
    _doctor_ready,
)


def main():
    from .gui_window import main as launch

    return launch()


if __name__ == "__main__":
    main()
