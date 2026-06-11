"""Verifica post-reboot della VM e notifica Telegram (one-shot).

Pianificato via cron per il 2026-06-12 08:00 Europe/Rome, dopo il reboot di
sicurezza delle 23:00 dell'11/06 (contesto: manutenzione kernel/security,
Oracle alert CVE-2026-35273 non applicabile). Controlla kernel, listener, cron
e il log dell'update; manda l'esito su Telegram; poi rimuove la propria riga
cron (self-clean). Sola lettura del sistema.

    python -m jobs.post_reboot_check            # check + Telegram + self-clean
    python -m jobs.post_reboot_check --dry-run  # stampa, niente Telegram/clean
"""

from __future__ import annotations

import os
import subprocess
import sys

from src.config import load_config
from src.telegram_client import TelegramClient

EXPECTED_KERNEL = "6.8.0-1054-oracle"
HOME = os.path.expanduser("~/tradealert")


def _run(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception as e:  # pragma: no cover
        return f"ERR: {e}"


def _self_clean():
    cur = _run("crontab -l")
    new = "\n".join(l for l in cur.splitlines() if "post_reboot_check" not in l)
    subprocess.run(["crontab", "-"], input=new + "\n", text=True)


def build() -> tuple[str, bool]:
    kernel = _run("uname -r")
    uptime = _run("uptime -p")
    listener = _run("systemctl is-active tradealert-listener.service")
    cron_svc = _run("systemctl is-active cron")
    trailing_log = _run(f"stat -c '%y' {HOME}/logs/trailing.log 2>/dev/null | cut -d'.' -f1")
    sec_tail = _run(f"tail -n 4 {HOME}/logs/sec-update.log 2>/dev/null")

    kernel_ok = kernel == EXPECTED_KERNEL
    listener_ok = listener == "active"
    cron_ok = cron_svc == "active"
    all_ok = kernel_ok and listener_ok and cron_ok

    head = "✅ Reboot VM OK" if all_ok else "⚠️ Reboot VM: da controllare"
    lines = [f"<b>{head}</b>", ""]
    lines.append(f"{'✅' if kernel_ok else '⚠️'} kernel: <code>{kernel}</code>"
                 + ("" if kernel_ok else f" (atteso {EXPECTED_KERNEL})"))
    lines.append(f"{'✅' if listener_ok else '⚠️'} listener: {listener}")
    lines.append(f"{'✅' if cron_ok else '⚠️'} cron: {cron_svc}")
    lines.append(f"⏱ uptime: {uptime}")
    lines.append(f"📄 ultimo trailing.log: {trailing_log or 'n/d'}")
    if sec_tail:
        lines.append("")
        lines.append("<b>sec-update.log:</b>")
        lines.append(f"<code>{sec_tail}</code>")
    return "\n".join(lines), all_ok


def main() -> int:
    dry = "--dry-run" in sys.argv
    text, _ = build()
    if dry:
        print(text)
        return 0
    cfg = load_config()
    TelegramClient(cfg).send_message(text)
    _self_clean()
    return 0


if __name__ == "__main__":
    sys.exit(main())
