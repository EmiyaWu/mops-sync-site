from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mos_s import Config, SpreadsheetNotFound, SyncService, configure_console_encoding, prepare_credentials_from_json_secret


LOGGER = logging.getLogger("mops_sync")


def write_github_output(name: str, value: Any) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def main() -> int:
    configure_console_encoding()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    prepare_credentials_from_json_secret()
    try:
        new_rows = SyncService(Config.from_env()).sync_once()
    except (SpreadsheetNotFound, FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("Execution failed: %s", exc)
        return 1

    has_new_rows = str(new_rows > 0).lower()
    write_github_output("new_rows", new_rows)
    write_github_output("has_new_rows", has_new_rows)
    LOGGER.info("GitHub Actions output: new_rows=%s has_new_rows=%s", new_rows, has_new_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
