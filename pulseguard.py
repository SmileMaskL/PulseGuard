#!/usr/bin/env python3
"""
PulseGuard - 가벼운 PC 상태 감시 프로그램
무료: CPU/메모리/디스크 사용량을 주기적으로 로컬 로그 파일에 기록
Pro(월 구독, Gumroad): 임계치 초과 시 웹훅(디스코드/슬랙) 알림, 더 긴 로그 보관, CSV 내보내기

개인정보는 어디로도 전송되지 않습니다 — 유일한 외부 통신은
① Pro 라이선스 확인(Gumroad) ② 설정된 웹훅으로 보내는 경고 알림뿐이고,
둘 다 사용자가 직접 설정해야만 동작합니다.
"""

import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.error
import urllib.parse
from datetime import datetime

APP_NAME = "PulseGuard"
# PyInstaller로 exe로 묶으면 __file__이 실행 파일 위치가 아니라 임시 압축
# 해제 폴더를 가리킨다 (sys.frozen이 그 신호) — 그러면 sys.executable을 써야
# 실제 exe가 있는 폴더 기준으로 로그/설정을 저장할 수 있다.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "pulseguard_data")
LOG_FILE = os.path.join(DATA_DIR, "monitor.log")
CSV_FILE = os.path.join(DATA_DIR, "history.csv")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

GUMROAD_PRODUCT_PERMALINK = "pulseguard"
MAX_LICENSE_ACTIVATIONS = 3

DEFAULT_CONFIG = {
    "license_key": "",
    "is_pro": False,
    "check_interval_seconds": 60,
    "mem_warn_percent": 90,
    "disk_warn_percent": 90,
    "webhook_url": "",
    "free_log_max_lines": 2000,
    "pro_log_max_lines": 20000,
    "dashboard_port": 8765,
    "dashboard_enabled": True,
}

latest_stats = {"timestamp": "-", "cpu": "-", "mem": "-", "disk": "-", "is_pro": False}
_stats_lock = threading.Lock()


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **cfg}
        return merged
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def verify_gumroad_license(license_key: str):
    """PixelBatch와 동일한 방식 - 별도 서버 없이 Gumroad API로 직접 확인"""
    data = urllib.parse.urlencode({
        "product_permalink": GUMROAD_PRODUCT_PERMALINK,
        "license_key": license_key,
        "increment_uses_count": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.gumroad.com/v2/licenses/verify", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            result = json.loads(res.read().decode("utf-8"))
    except urllib.error.URLError:
        return {"ok": False, "reason": "network"}

    if not result.get("success"):
        return {"ok": False, "reason": "invalid"}
    uses = result.get("uses", 0)
    if isinstance(uses, int) and uses > MAX_LICENSE_ACTIVATIONS:
        return {"ok": False, "reason": "overused", "uses": uses}
    return {"ok": True}


def activate_license(cfg, license_key: str) -> str:
    result = verify_gumroad_license(license_key)
    if result["ok"]:
        cfg["license_key"] = license_key
        cfg["is_pro"] = True
        save_config(cfg)
        return "인증 완료! Pro가 활성화되었습니다."
    if result["reason"] == "overused":
        return f"이 키는 이미 {result['uses']}회 인증되어 더 이상 사용할 수 없습니다."
    if result["reason"] == "network":
        return "인터넷 연결을 확인해주세요."
    return "유효하지 않은 라이선스 키입니다."


def show_native_notification(title: str, message: str):
    """무료 기능 - 인터넷/계정 없이 이 PC 화면에 바로 뜨는 알림 (실패해도 무시)"""
    try:
        if sys.platform.startswith("win"):
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n = New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
                "$n.Visible = $true;"
                f"$n.ShowBalloonTip(8000, '{title}', '{message}', "
                "[System.Windows.Forms.ToolTipIcon]::Warning);"
                "Start-Sleep -Seconds 9;"
                "$n.Dispose();"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["notify-send", title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (FileNotFoundError, OSError):
        pass  # 알림 표시 기능이 없는 환경이면 조용히 건너뜀 (로그 기록은 그대로 유지됨)


def send_webhook_alert(webhook_url: str, message: str):
    if not webhook_url:
        return
    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.URLError:
        pass


def read_stats():
    """psutil이 있으면 사용(Windows/Mac/Linux 전부 지원), 없으면 Linux /proc로 대체"""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(os.path.abspath(os.sep)).percent
        return round(cpu, 1), round(mem, 1), round(disk, 1)
    except ImportError:
        return read_stats_linux_fallback()


def read_stats_linux_fallback():
    load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    mem_percent = None
    try:
        total = available = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1])
                if total is not None and available is not None:
                    break
        if total:
            mem_percent = round((total - available) / total * 100, 1)
    except FileNotFoundError:
        pass
    disk_percent = round(shutil.disk_usage("/").used / shutil.disk_usage("/").total * 100, 1)
    return load1, mem_percent, disk_percent


def rotate_log_if_needed(max_lines: int):
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    if len(lines) > max_lines:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])


def write_log(line: str, max_lines: int):
    os.makedirs(DATA_DIR, exist_ok=True)
    rotate_log_if_needed(max_lines)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_csv_row(timestamp, cpu, mem, disk):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "cpu_or_load", "memory_percent", "disk_percent"])
        writer.writerow([timestamp, cpu, mem, disk])


class DashboardHandler(BaseHTTPRequestHandler):
    """무료 기능 - 이 컴퓨터 안에서만 보이는 실시간 상태 페이지 (외부 네트워크에 노출되지 않음)"""

    def do_GET(self):
        with _stats_lock:
            s = dict(latest_stats)
        html = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>PulseGuard 대시보드</title>
<style>body{{font-family:sans-serif;background:#0f172a;color:#eef2ff;padding:24px;}}
.box{{background:#16213e;border-radius:12px;padding:20px;max-width:400px}}
.row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #28345c}}
</style></head><body>
<div class="box">
<h2>🛡️ PulseGuard {"Pro" if s["is_pro"] else "무료"}</h2>
<div class="row"><span>마지막 확인</span><b>{s["timestamp"]}</b></div>
<div class="row"><span>CPU/부하</span><b>{s["cpu"]}</b></div>
<div class="row"><span>메모리</span><b>{s["mem"]}%</b></div>
<div class="row"><span>디스크</span><b>{s["disk"]}%</b></div>
</div>
<p style="color:#9aa5c7;font-size:.8rem">10초마다 자동 새로고침 · 이 페이지는 이 컴퓨터에서만 열립니다</p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # 콘솔에 매 요청마다 로그가 쌓이지 않도록 조용히 처리


def start_dashboard_server(port: int):
    try:
        server = HTTPServer(("127.0.0.1", port), DashboardHandler)  # 127.0.0.1만 - 외부 네트워크 접근 불가
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except OSError:
        pass  # 포트가 이미 사용 중이면 대시보드 없이 계속 진행 (감시 기능엔 영향 없음)


_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = load_config()
    is_pro = bool(cfg.get("is_pro"))
    max_lines = cfg["pro_log_max_lines"] if is_pro else cfg["free_log_max_lines"]

    write_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ▶ {APP_NAME} 감시 시작 ({'Pro' if is_pro else '무료'})", max_lines)

    if cfg.get("dashboard_enabled", True):
        start_dashboard_server(cfg["dashboard_port"])
        write_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 대시보드: http://localhost:{cfg['dashboard_port']}/", max_lines)

    while _running:
        try:
            cpu, mem, disk = read_stats()
            timestamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            write_log(f"[{timestamp}] CPU/부하={cpu} 메모리={mem}% 디스크={disk}%", max_lines)

            with _stats_lock:
                latest_stats.update(timestamp=timestamp, cpu=cpu, mem=mem, disk=disk, is_pro=is_pro)

            if is_pro:
                write_csv_row(timestamp, cpu, mem, disk)

            if mem is not None and mem >= cfg["mem_warn_percent"]:
                msg = f"[{timestamp}] 경고: 메모리 사용량 {mem}%"
                write_log(msg, max_lines)
                show_native_notification("PulseGuard 경고", f"메모리 사용량 {mem}%")
                if is_pro:
                    send_webhook_alert(cfg["webhook_url"], f"⚠️ PulseGuard 경고: 메모리 {mem}%")
            if disk is not None and disk >= cfg["disk_warn_percent"]:
                msg = f"[{timestamp}] 경고: 디스크 사용량 {disk}%"
                write_log(msg, max_lines)
                show_native_notification("PulseGuard 경고", f"디스크 사용량 {disk}%")
                if is_pro:
                    send_webhook_alert(cfg["webhook_url"], f"⚠️ PulseGuard 경고: 디스크 {disk}%")
        except Exception as e:
            write_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 오류: {e}", max_lines)

        for _ in range(cfg["check_interval_seconds"]):
            if not _running:
                break
            time.sleep(1)

    write_log(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ■ 감시 종료", max_lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "activate":
        cfg = load_config()
        if len(sys.argv) < 3:
            print("사용법: python pulseguard.py activate <라이선스키>")
            sys.exit(1)
        print(activate_license(cfg, sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "config":
        cfg = load_config()
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    else:
        main()
