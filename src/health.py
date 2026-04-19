"""Health check della VM Oracle Free Tier.

Obiettivo: prevenire il rischio di addebiti inattesi superando i
limiti del tier gratuito. I due limiti concreti sono:
- Bandwidth outbound: 10 TB/mese. Oltre, ~$0.0085 per GB.
- Disk: 200 GB totali (ma la VM e-micro tipica ha ~46 GB).

Il modulo legge le metriche disponibili localmente (vnstat per la banda
se installato, /proc/meminfo e df per RAM/disco) e produce un messaggio
Telegram con eventuali warning.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


# Soglie: raggiunto il WARNING, messaggio con simbolo ⚠️; raggiunto
# CRITICAL il messaggio ha 🚨 e sconsiglia il proseguimento.
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 92
MEM_WARN_PCT = 85
MEM_CRIT_PCT = 95
BANDWIDTH_TB_WARN = 7.0    # 70% del limite mensile 10 TB
BANDWIDTH_TB_CRIT = 9.0    # 90%
LOGS_WARN_MB = 500


@dataclass
class HealthMetric:
    label: str
    value: str
    warn: bool = False
    crit: bool = False


def _run(cmd: list[str], timeout: int = 5) -> str:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.stdout.strip()
    except Exception as exc:
        log.warning("Comando %s fallito: %s", cmd, exc)
        return ""


def _disk_root() -> HealthMetric:
    usage = shutil.disk_usage("/")
    used_pct = usage.used / usage.total * 100
    used_gb = usage.used / 1024**3
    total_gb = usage.total / 1024**3
    return HealthMetric(
        label="Disco /",
        value=f"{used_gb:.1f} / {total_gb:.1f} GB ({used_pct:.0f}%)",
        warn=used_pct >= DISK_WARN_PCT,
        crit=used_pct >= DISK_CRIT_PCT,
    )


def _memory() -> HealthMetric:
    try:
        data: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                data[parts[0].rstrip(":")] = int(parts[1])  # kB
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", total)
        used = total - available
        used_pct = used / total * 100 if total else 0
        return HealthMetric(
            label="RAM",
            value=f"{used / 1024:.0f} / {total / 1024:.0f} MB ({used_pct:.0f}%)",
            warn=used_pct >= MEM_WARN_PCT,
            crit=used_pct >= MEM_CRIT_PCT,
        )
    except Exception as exc:
        return HealthMetric(label="RAM", value=f"errore: {exc}")


def _uptime_load() -> HealthMetric:
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        load = _run(["cat", "/proc/loadavg"]).split()[:3]
        load_str = " ".join(load) if load else "?"
        return HealthMetric(
            label="Uptime / load",
            value=f"{days}d {hours}h, load {load_str}",
        )
    except Exception as exc:
        return HealthMetric(label="Uptime / load", value=f"errore: {exc}")


def _bandwidth_month() -> HealthMetric:
    """Outbound del mese corrente via vnstat. Se non installato, segnala
    che la metrica non e' disponibile."""
    if not shutil.which("vnstat"):
        return HealthMetric(
            label="Banda outbound mese",
            value="vnstat non installato (sudo apt install vnstat)",
            warn=True,
        )
    # --oneline ;-separated: version;iface;today_rx;today_tx;...;month_rx;month_tx;total_rx;total_tx
    raw = _run(["vnstat", "--oneline", "b"])
    if not raw:
        # Prima esecuzione vnstat puo' richiedere qualche minuto per
        # avere dati. Tenta un "vnstat -m" come fallback.
        raw_m = _run(["vnstat", "-m"])
        return HealthMetric(
            label="Banda outbound mese",
            value=("dati non ancora pronti" if not raw_m else raw_m.splitlines()[-2][:80]),
        )
    parts = raw.split(";")
    if len(parts) < 11:
        return HealthMetric(
            label="Banda outbound mese",
            value=f"parse error: {raw[:80]}",
        )
    try:
        month_tx_bytes = int(parts[9])
    except ValueError:
        return HealthMetric(
            label="Banda outbound mese",
            value=f"parse error TX: {parts[9]}",
        )
    month_tx_tb = month_tx_bytes / 1024**4
    pct = month_tx_tb / 10.0 * 100
    return HealthMetric(
        label="Banda outbound mese",
        value=f"{month_tx_tb:.2f} TB / 10 TB ({pct:.0f}%)",
        warn=month_tx_tb >= BANDWIDTH_TB_WARN,
        crit=month_tx_tb >= BANDWIDTH_TB_CRIT,
    )


def _logs_size(project_dir: Path) -> HealthMetric:
    logs = project_dir / "logs"
    if not logs.exists():
        return HealthMetric(label="Dimensione logs/", value="cartella assente")
    total = sum(p.stat().st_size for p in logs.rglob("*") if p.is_file())
    mb = total / 1024**2
    return HealthMetric(
        label="Dimensione logs/",
        value=f"{mb:.0f} MB",
        warn=mb >= LOGS_WARN_MB,
    )


def _systemd_listener() -> HealthMetric:
    state = _run(["systemctl", "is-active", "tradealert-listener"]) or "unknown"
    return HealthMetric(
        label="Listener daemon",
        value=state,
        warn=state != "active",
        crit=state not in ("active", "activating"),
    )


def collect_metrics(project_dir: Path) -> list[HealthMetric]:
    return [
        _uptime_load(),
        _memory(),
        _disk_root(),
        _logs_size(project_dir),
        _bandwidth_month(),
        _systemd_listener(),
    ]


def build_health_report(project_dir: Path | None = None) -> tuple[str, bool]:
    """Ritorna (messaggio HTML per Telegram, has_warning_or_worse)."""
    project_dir = project_dir or Path(__file__).resolve().parent.parent
    metrics = collect_metrics(project_dir)
    has_crit = any(m.crit for m in metrics)
    has_warn = any(m.warn for m in metrics)
    header_icon = "🚨" if has_crit else ("⚠️" if has_warn else "✅")
    header_text = (
        "CRITICO — rischio addebiti" if has_crit
        else "Warning" if has_warn
        else "Tutto ok"
    )

    now = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"{header_icon} <b>Health VM Oracle</b> — {header_text}",
        f"<i>{now}</i>",
        "",
    ]
    for m in metrics:
        icon = "🚨" if m.crit else ("⚠️" if m.warn else "•")
        lines.append(f"{icon} <b>{m.label}</b>: {m.value}")

    if has_crit:
        lines.append("")
        lines.append(
            "<i>Azione consigliata: controlla la dashboard OCI e se necessario "
            "ferma temporaneamente i job cron per evitare overage.</i>"
        )
    return "\n".join(lines), (has_warn or has_crit)
