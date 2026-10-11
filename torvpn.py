#!/usr/bin/env python3
"""
torvpn - ابزار اتصال به Tor با Bridge و تنظیم خودکار SOCKS Proxy سیستم در macOS

دستورات:
  python3 torvpn.py setup                  نصب Tor و پلاگین‌ها با Homebrew
  python3 torvpn.py connect                اتصال با Snowflake (پیش‌فرض)
  python3 torvpn.py connect -m obfs4       اتصال با obfs4 (از bridges.txt)
  python3 torvpn.py connect -m direct      اتصال بدون Bridge
  python3 torvpn.py disconnect             قطع اتصال و برگرداندن تنظیمات شبکه
  python3 torvpn.py status                 وضعیت و IP خروجی
  python3 torvpn.py test-bridges FILE      تست و رتبه‌بندی Bridgeهای obfs4

توجه: این ابزار پروکسی SOCKS سیستم را تنظیم می‌کند، نه تونل کامل (TUN).
برنامه‌هایی که پروکسی سیستم را نادیده می‌گیرند از Tor رد نمی‌شوند.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home() / ".torvpn"
TORRC = HOME / "torrc"
PIDFILE = HOME / "tor.pid"
LOG = HOME / "tor.log"
DATA = HOME / "data"
STATE = HOME / "state.json"
BRIDGES = HOME / "bridges.txt"

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 9050

EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin"]

# Bridge پیش‌فرض Snowflake (ممکن است قدیمی شود؛ در آن صورت خط جدید را در bridges.txt بگذار)
DEFAULT_SNOWFLAKE = (
    "snowflake 192.0.2.3:80 2B280B23E1107BB62ABFC40DDCC8824814F80A72 "
    "fingerprint=2B280B23E1107BB62ABFC40DDCC8824814F80A72 "
    "url=https://1098762253.rsc.cdn77.org/ "
    "fronts=app.datapacket.com,www.datapacket.com "
    "ice=stun:stun.l.google.com:19302,stun:stun.antisip.com:3478,"
    "stun:stun.bluesip.net:3478,stun:stun.dus.net:3478,stun:stun.epygi.com:3478,"
    "stun:stun.sonetel.com:3478,stun:stun.uls.co.za:3478,"
    "stun:stun.voipgate.com:3478,stun:stun.voys.nl:3478 "
    "utls-imitate=hellorandomizedalpn"
)


# ---------------------------------------------------------------- helpers
def which(*names):
    path = os.pathsep.join(EXTRA_PATHS + [os.environ.get("PATH", "")])
    for n in names:
        p = shutil.which(n, path=path)
        if p:
            return p
    return None


def die(msg):
    print(f"✗ {msg}")
    sys.exit(1)


def sudo_networksetup(*args):
    return subprocess.run(["sudo", "networksetup", *args],
                          capture_output=True, text=True)


def network_services():
    out = subprocess.run(["networksetup", "-listallnetworkservices"],
                         capture_output=True, text=True).stdout.splitlines()
    # خط اول توضیح است و سرویس‌های غیرفعال با * شروع می‌شوند
    return [s for s in out[1:] if s and not s.startswith("*")]


def get_socks(svc):
    out = subprocess.run(["networksetup", "-getsocksfirewallproxy", svc],
                         capture_output=True, text=True).stdout
    d = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(":")
        d[k.strip()] = v.strip()
    return {"enabled": d.get("Enabled", "No").lower() == "yes",
            "server": d.get("Server", ""), "port": d.get("Port", "0")}


def restore_socks(svc, prev):
    if prev.get("enabled") and prev.get("server") and prev.get("port") not in ("", "0"):
        sudo_networksetup("-setsocksfirewallproxy", svc,
                          prev["server"], str(prev["port"]))
        sudo_networksetup("-setsocksfirewallproxystate", svc, "on")
    else:
        sudo_networksetup("-setsocksfirewallproxystate", svc, "off")


def read_pid():
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


# ---------------------------------------------------------------- setup
def cmd_setup(_):
    brew = which("brew")
    if not brew:
        die("Homebrew نصب نیست. از https://brew.sh نصب کن.")
    for formula in ("tor", "snowflake", "lyrebird"):
        print(f"→ brew install {formula}")
        r = subprocess.run([brew, "install", formula])
        if r.returncode != 0 and formula == "lyrebird":
            print("  lyrebird پیدا نشد، obfs4proxy را امتحان می‌کنم")
            subprocess.run([brew, "install", "obfs4proxy"])
    print("✓ نصب تمام شد")


# ---------------------------------------------------------------- torrc
def bridge_lines(mode):
    lines = []
    if BRIDGES.exists():
        for ln in BRIDGES.read_text().splitlines():
            ln = ln.strip()
            if ln.lower().startswith("bridge "):
                ln = ln[7:].strip()
            if ln and not ln.startswith("#") and ln.split()[0] == mode:
                lines.append(ln)
    if mode == "snowflake" and not lines:
        lines.append(DEFAULT_SNOWFLAKE)
    if mode == "obfs4":
        lines = lines[:5]
    return lines


def build_torrc(mode):
    cfg = [
        f"SocksPort {SOCKS_HOST}:{SOCKS_PORT}",
        f"DataDirectory {DATA}",
        f"Log notice file {LOG}",
        f"PidFile {PIDFILE}",
        "RunAsDaemon 1",
        "AvoidDiskWrites 1",
        "ClientOnly 1",
    ]
    if mode == "direct":
        return "\n".join(cfg) + "\n"

    lines = bridge_lines(mode)
    if not lines:
        die(f"هیچ Bridge از نوع {mode} در {BRIDGES} نیست.")
    if mode == "snowflake":
        exe = which("snowflake-client")
        if not exe:
            die("snowflake-client نصب نیست. اول: python3 torvpn.py setup")
    else:
        exe = which("lyrebird", "obfs4proxy")
        if not exe:
            die("lyrebird/obfs4proxy نصب نیست. اول: python3 torvpn.py setup")

    cfg += ["UseBridges 1", f"ClientTransportPlugin {mode} exec {exe}"]
    cfg += [f"Bridge {ln}" for ln in lines]
    return "\n".join(cfg) + "\n"


# ---------------------------------------------------------------- connect
def cmd_connect(args):
    tor = which("tor")
    if not tor:
        die("Tor نصب نیست. اول: python3 torvpn.py setup")
    if read_pid():
        die("Tor از قبل در حال اجراست. اول disconnect بزن.")

    HOME.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    LOG.write_text("")
    TORRC.write_text(build_torrc(args.mode))

    print(f"→ اجرای Tor (حالت: {args.mode})")
    r = subprocess.run([tor, "-f", str(TORRC)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"Tor اجرا نشد:\n{r.stderr or r.stdout}")

    # صبر تا Bootstrap کامل شود
    deadline = time.time() + args.timeout
    last = -1
    last_have = 0
    ok = False
    while time.time() < deadline:
        txt = LOG.read_text(errors="ignore") if LOG.exists() else ""
        m = re.findall(r"Bootstrapped (\d+)%", txt)
        pct = int(m[-1]) if m else 0
        if pct != last:
            print(f"   Bootstrap {pct}%")
            last = pct
            deadline = time.time() + args.timeout  # تا وقتی پیشرفت هست قطع نکن
        d = re.findall(r"we have (\d+)/(\d+)", txt)
        if d and int(d[-1][0]) > last_have:
            last_have = int(d[-1][0])
            print(f"   دانلود اطلاعات رله‌ها: {last_have}/{d[-1][1]}")
            deadline = time.time() + args.timeout  # دانلود در حال پیشرفت است
        if pct >= 100:
            ok = True
            break
        time.sleep(1)

    if not ok:
        tail = "\n".join(LOG.read_text(errors="ignore").splitlines()[-8:])
        stop_tor()
        die(f"{args.timeout} ثانیه هیچ پیشرفتی نبود.\n{tail}\n"
            "راهنما: Bridge دیگری امتحان کن (-m obfs4) یا خط جدید در bridges.txt بگذار.")

    if args.no_proxy:
        print(f"✓ Tor آماده است روی {SOCKS_HOST}:{SOCKS_PORT} (پروکسی سیستم تغییر نکرد)")
        return

    print("→ تنظیم پروکسی سیستم (رمز مدیر لازم است)")
    subprocess.run(["sudo", "-v"])
    # اگر state قبلی هست (مثلاً بعد از کرش)، تنظیمات اصلی همان است نه پروکسی Tor
    prev = load_state().get("prev") or {s: get_socks(s) for s in network_services()}
    changed = []
    for svc in network_services():
        r1 = sudo_networksetup("-setsocksfirewallproxy", svc,
                               SOCKS_HOST, str(SOCKS_PORT))
        r2 = sudo_networksetup("-setsocksfirewallproxystate", svc, "on")
        if r1.returncode == 0 and r2.returncode == 0:
            changed.append(svc)
    STATE.write_text(json.dumps({"prev": prev, "services": changed,
                                 "mode": args.mode}))
    print(f"✓ وصل شد. پروکسی روی: {', '.join(changed) or 'هیچ سرویسی'}")
    print("  بررسی IP: python3 torvpn.py status")


# ---------------------------------------------------------------- disconnect
def stop_tor():
    pid = read_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 15)
        for _ in range(20):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, 9)
    except ProcessLookupError:
        pass
    return True


def cmd_disconnect(_):
    state = load_state()
    services = state.get("services", [])
    prev = state.get("prev", {})
    if services:
        subprocess.run(["sudo", "-v"])
        for svc in services:
            restore_socks(svc, prev.get(svc, {}))
        print("✓ تنظیمات پروکسی قبلی برگردانده شد")
    else:
        print("تنظیمات شبکه توسط این ابزار تغییری نکرده بود؛ به آن دست نمی‌زنم")
    if stop_tor():
        print("✓ Tor متوقف شد")
    else:
        print("Tor در حال اجرا نبود")
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- status
def cmd_status(_):
    pid = read_pid()
    print(f"Tor: {'در حال اجرا (pid ' + str(pid) + ')' if pid else 'خاموش'}")
    if not pid:
        return
    r = subprocess.run(
        ["curl", "-s", "--max-time", "25", "--socks5-hostname",
         f"{SOCKS_HOST}:{SOCKS_PORT}", "https://check.torproject.org/api/ip"],
        capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        print(f"IP خروجی: {d.get('IP')}   |   از Tor رد می‌شود: {d.get('IsTor')}")
    except Exception:
        print("نتوانستم IP را بگیرم (شبکه کند است یا Tor هنوز آماده نیست).")


# ---------------------------------------------------------------- bridge tester
def parse_bridge(line):
    line = line.strip()
    if line.lower().startswith("bridge "):
        line = line[7:].strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split()
    if len(parts) < 3 or parts[0] != "obfs4":
        return None
    host, _, port = parts[1].rpartition(":")
    host = host.strip("[]")
    try:
        return line, host, int(port)
    except ValueError:
        return None


def probe(item):
    line, host, port = item
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=5):
            return line, host, port, (time.time() - t0) * 1000
    except OSError:
        return line, host, port, None


def cmd_test_bridges(args):
    src = Path(args.file)
    if not src.exists():
        die(f"فایل {src} پیدا نشد.")
    items = [p for p in map(parse_bridge, src.read_text().splitlines()) if p]
    if not items:
        die("هیچ خط obfs4 معتبری در فایل نیست.")
    print(f"→ تست {len(items)} Bridge ...")
    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(probe, items))

    alive = sorted([r for r in results if r[3] is not None], key=lambda r: r[3])
    for _, host, port, ms in alive:
        print(f"  ✓ {host}:{port}  {ms:.0f} ms")
    print(f"زنده: {len(alive)} از {len(results)}")
    if alive:
        HOME.mkdir(parents=True, exist_ok=True)
        keep = [l for l in (BRIDGES.read_text().splitlines() if BRIDGES.exists() else [])
                if l.strip() and l.split()[0] != "obfs4"]
        keep += [r[0] for r in alive]
        BRIDGES.write_text("\n".join(keep) + "\n")
        print(f"✓ ذخیره شد در {BRIDGES}")
    print("نکته: این تست فقط باز بودن پورت را می‌سنجد، نه کارکرد کامل Bridge.")


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Tor VPN برای macOS")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup").set_defaults(fn=cmd_setup)

    c = sub.add_parser("connect")
    c.add_argument("-m", "--mode", choices=["snowflake", "obfs4", "direct"],
                   default="snowflake")
    c.add_argument("-t", "--timeout", type=int, default=300)
    c.add_argument("--no-proxy", action="store_true",
                   help="فقط Tor را اجرا کن، پروکسی سیستم را تغییر نده")
    c.set_defaults(fn=cmd_connect)

    sub.add_parser("disconnect").set_defaults(fn=cmd_disconnect)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    t = sub.add_parser("test-bridges")
    t.add_argument("file")
    t.set_defaults(fn=cmd_test_bridges)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("این ابزار فقط برای macOS نوشته شده است.")
    main()
