"""Where the app keeps its local files: the repo root, whatever folder you run a script from."""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_path(name: str) -> str:
    return os.path.join(REPO_ROOT, name)


DB_PATH = repo_path("scheduler.db")
CACHE_PATH = repo_path("last_extraction.json")  # last raw extraction, so the review can be replayed free
