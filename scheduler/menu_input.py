"""Small prompt helpers shared by the CLI menus (commutes, settings)."""


def ask_until(ask, show, label: str, parse, default=None):
    """Ask until parse() accepts. Enter returns `default`, which is None (= cancel) when there is none."""
    while True:
        raw = ask(f"  {label}: ").strip()
        if not raw:
            return default
        try:
            return parse(raw)
        except ValueError as e:
            show(f"  Invalid: {e}")


def pick(ask, show, items, prompt: str):
    """Let the student choose one of items by its 1-based number. Enter (or a bad number) returns None."""
    raw = ask(prompt).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1]
    if raw:
        show(f"Enter a number between 1 and {len(items)}.")
    return None
