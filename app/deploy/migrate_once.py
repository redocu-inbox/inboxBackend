"""One-shot migration command for a separate release step.

    python -m app.deploy.migrate_once
"""

from app.deploy.migrate import upgrade_head_with_lock


def main() -> None:
    upgrade_head_with_lock()


if __name__ == "__main__":
    main()
