#!/usr/bin/env python3
import base64
import json
import os
import re
import signal
import sqlite3
import time
import urllib.error
import urllib.request

DOCS_URLS = [
    "https://docs.evcc.io/en/sponsorship/",
    "https://docs.evcc.io/docs/sponsorship",
    "https://docs.evcc.io/de/sponsorship/",
]
DB_PATH = "/root/.evcc/evcc.db"
CHECK_INTERVAL = 12 * 60 * 60  # 12 hours on success
RETRY_INTERVAL = 5 * 60         # 5 minutes on error
SETTINGS_KEY = "sponsorToken"
MIN_VALIDITY_SECONDS = 3600    # Require at least 1h remaining validity


def log(msg: str) -> None:
    print(f"[trial-token-updater] {msg}", flush=True)


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format (expected 3 parts)")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def fetch_published_token() -> tuple[str, dict]:
    jwt_pattern = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
    last_error = None

    for url in DOCS_URLS:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "evcc-trial-token-updater/1.0",
                    "Accept": "text/html",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            for candidate in jwt_pattern.findall(html):
                try:
                    payload = decode_jwt_payload(candidate)
                except Exception:
                    continue

                if payload.get("sub") != "trial":
                    continue

                exp = payload.get("exp")
                if not isinstance(exp, int):
                    continue

                if exp <= int(time.time()) + MIN_VALIDITY_SECONDS:
                    continue

                return candidate, payload

        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"No valid unexpired trial JWT found on docs pages. Last error: {last_error}"
    )


def get_current_token() -> str | None:
    if not os.path.exists(DB_PATH):
        return None

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        )
        if not cur.fetchone():
            return None

        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (SETTINGS_KEY,),
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def update_db_token(token: str) -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key text PRIMARY KEY,
                value text
            )
            """
        )
        conn.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (SETTINGS_KEY, token),
        )
        conn.commit()
    finally:
        conn.close()


def find_evcc_pid() -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode("utf-8", errors="replace")
            cmd = cmdline.strip().split(" ", 1)[0]
            if cmd == "evcc" or cmd.endswith("/evcc"):
                return pid
        except (FileNotFoundError, PermissionError):
            continue
    return None


def restart_evcc() -> None:
    pid = find_evcc_pid()
    if pid is None:
        log("evcc process not found; token will be loaded when evcc starts")
        return
    log(f"Sending SIGTERM to evcc (PID {pid}) to trigger restart...")
    os.kill(pid, signal.SIGTERM)


def check() -> bool:
    token, payload = fetch_published_token()
    exp = int(payload["exp"])
    remaining = exp - int(time.time())
    log(
        f"Fetched valid trial token (expires in "
        f"{remaining // 86400}d {(remaining % 86400) // 3600}h)"
    )

    current = get_current_token()
    if current == token:
        log("Database token matches published token. No update required.")
        return True

    if current:
        try:
            curr_payload = decode_jwt_payload(current)
            if curr_payload.get("sub") != "trial":
                log(
                    f"Existing sponsorToken is non-trial (sub='{curr_payload.get('sub')}'). "
                    "Skipping update to preserve lifetime/custom token."
                )
                return True
        except Exception:
            pass  # Malformed or expired token in DB; safe to replace

    log("Writing new trial token to SQLite database...")
    update_db_token(token)
    log("Database updated successfully.")
    restart_evcc()
    return True


def main() -> None:
    log(f"Started. Monitoring {DOCS_URLS[0]} -> {DB_PATH}")
    while True:
        try:
            success = check()
            sleep_time = CHECK_INTERVAL if success else RETRY_INTERVAL
        except Exception as err:
            log(f"Check failed: {err}")
            sleep_time = RETRY_INTERVAL

        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
