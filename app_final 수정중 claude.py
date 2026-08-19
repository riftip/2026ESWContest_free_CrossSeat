# -*- coding: utf-8 -*-
import os
import queue
import re
import threading
import time
import subprocess
from datetime import datetime
import requests
from flask import Flask, jsonify, render_template, request, Response
import json

# #############################################################################
#
#   1. 설정   ★★★ 고칠 일이 있으면 여기만 보면 됩니다 ★★★
#
# #############################################################################

# ─────────────────────────────────────────────────────────────── ★ 수정 1
PI_IP = "192.168.0.113"
ADMIN_PIN = "1234"
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 2
SEAT_WEMOS_IPS = {
    1: "192.168.0.100",
    2: "192.168.0.229",
    3: "192.168.0.151",
    4: "192.168.0.112",
    5: "192.168.0.105",
    6: "192.168.0.106",
}
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 3
SEAT_POWER = {
    1: ("192.168.0.191", 3),
    2: ("192.168.0.191", 2),
    3: ("192.168.0.191", 1),
    4: ("192.168.0.193", 3),
    5: ("192.168.0.193", 2),
    6: ("192.168.0.193", 1),
}
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 4
TAPO_USER = os.environ.get("TAPO_USER", "cycy0125@cau.ac.kr")
TAPO_PASS = os.environ.get("TAPO_PASS", "young040125")
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 5
HARDWARE_ENABLED = True
LED_ENABLED = True
POWER_ENABLED = True
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 6
WARN_SECONDS = 180          # 3분 -> B (사석화 경고)
TERMINATE_SECONDS = 300     # 5분 -> C (이용 종료)
RETURN_GRACE_SECONDS = 60
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 7
ALERT_STATES = ["C", "D", "E"]
ALERT_MESSAGES = {
    "C": "{seat}번 좌석 · 사석화 확정 — 5분 이상 비어 이용을 종료했습니다. 확인 필요합니다.",
    "D": "{seat}번 좌석 · 무단 사용 — 발권하지 않은 이용자가 앉아 있습니다. 확인이 필요합니다.",
    "E": "{seat}번 좌석 · 이용 종료 — 좌석이 비었고 전원이 차단되었습니다.",
}
# ────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────── ★ 수정 8
# Wemos 펌웨어는 /on 과 /off 두 개의 주소만 가지고 있습니다.
# 따라서 논리적 상태 이름을 실제 명령으로 번역하는 표가 반드시 필요합니다.
# "blink" 는 Wemos 펌웨어 기능이 아니라, 아래 blink_worker 가
# /on 과 /off 를 번갈아 보내서 소프트웨어로 만들어내는 점멸입니다.
LED_CMD = {
    "occupied": "off",              # A 정상점유
    "abandoned_warning": "blink",   # B 사석화경고 (점멸)
    "abandoned_terminated": "on",   # C 사석화확정
    "illegal": "on",                # D 무단사용
    "vacant": "off",                # E 빈자리
}
BLINK_INTERVAL = 0.7  # 점멸 주기(초)
# ────────────────────────────────────────────────────────────────────────

# ── 그 밖의 설정 ──────────────────────────────────────
SEAT_IDS = [1, 2, 3, 4, 5, 6]
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000
DEBUG_MODE = False
MONITOR_INTERVAL = 2.0
WEMOS_TIMEOUT = 2
PHONE_REGEX = re.compile(r"^010-\d{4}-\d{4}$")

# =============================================================================
# 2. 좌석 상태 및 데이터 구조
# =============================================================================
seats = [{"id": i, "status": "EMPTY", "phone": None} for i in SEAT_IDS]
vision = {sid: {"person": False, "last_seen": time.time(), "confidence": 0.0, "last_packet": None} for sid in SEAT_IDS}
current_state = {sid: None for sid in SEAT_IDS}
grace_until = {sid: 0.0 for sid in SEAT_IDS}

event_log = []
alert_log = []
notice_log = []
lock = threading.RLock()
user_event_queues = {}

# 🌟 LED 와 전원을 서로 다른 대기열/스레드로 분리했습니다.
#    kasa 명령은 한 번에 몇 초씩 걸리기 때문에, 같은 줄에 세워두면
#    LED 명령이 전원 명령 뒤에서 수십 초씩 밀립니다.
led_queue = queue.Queue()
power_queue = queue.Queue()

# 점멸 중인 좌석 목록
blink_seats = set()
blink_lock = threading.Lock()


def get_or_create_queue(seat_id):
    if seat_id not in user_event_queues:
        user_event_queues[seat_id] = queue.Queue()
    return user_event_queues[seat_id]


def push_notification_to_user(seat_id, title, message, event_type="WARNING"):
    q = get_or_create_queue(seat_id)
    q.put({"type": event_type, "title": title, "message": message, "timestamp": datetime.now().strftime("%H:%M:%S")})


# =============================================================================
# 3. 하드웨어 제어
# =============================================================================
def wemos_cmd(seat_id, cmd):
    """Wemos 에 실제 HTTP 요청을 보내는 유일한 통로. cmd 는 'on' 또는 'off'."""
    if not (HARDWARE_ENABLED and LED_ENABLED):
        return False
    ip = SEAT_WEMOS_IPS.get(seat_id)
    if not ip:
        print(f"⚠️ [LED] {seat_id}번 좌석 IP 정보 없음")
        return False
    url = f"http://{ip}/{cmd}"
    try:
        r = requests.get(url, timeout=WEMOS_TIMEOUT)
        print(f"💡 [LED] {seat_id}번 → {url} (응답 {r.status_code})")
        return r.status_code == 200
    except requests.exceptions.Timeout:
        print(f"⚠️ [LED 실패] {seat_id}번 ({ip}) - 응답 시간 초과")
    except requests.exceptions.ConnectionError:
        print(f"⚠️ [LED 실패] {seat_id}번 ({ip}) - 연결 실패 (전원/IP 확인)")
    except Exception as e:
        print(f"⚠️ [LED 실패] {seat_id}번 ({ip}) - {e}")
    return False


def led_worker():
    """LED 명령 전용 스레드"""
    print("[LED 스레드] 대기열 처리 시작...")
    while True:
        try:
            seat_id, led_state = led_queue.get(timeout=2.0)
            cmd = LED_CMD.get(led_state, "off")

            if cmd == "blink":
                with blink_lock:
                    blink_seats.add(seat_id)
                print(f"💡 [LED] {seat_id}번 → 점멸 시작")
            else:
                with blink_lock:
                    blink_seats.discard(seat_id)
                wemos_cmd(seat_id, cmd)

            led_queue.task_done()
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[LED 스레드 오류] {e}")


def blink_worker():
    """점멸 상태(B) 좌석에 /on 과 /off 를 번갈아 보내는 스레드"""
    phase = False
    while True:
        phase = not phase
        with blink_lock:
            targets = list(blink_seats)
        for sid in targets:
            wemos_cmd(sid, "on" if phase else "off")
        time.sleep(BLINK_INTERVAL)


def power_worker():
    """Tapo 전원 명령 전용 스레드"""
    print("[전원 스레드] 대기열 처리 시작...")
    while True:
        try:
            seat_id, on = power_queue.get(timeout=2.0)

            if HARDWARE_ENABLED and POWER_ENABLED:
                ip, child_idx = SEAT_POWER.get(seat_id, (None, None))
                if ip and child_idx is not None:
                    action = "on" if on else "off"
                    cmd = [
                        "kasa", "--host", ip,
                        "--username", TAPO_USER, "--password", TAPO_PASS,
                        action, "--child-index", str(child_idx)
                    ]
                    try:
                        subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                        print(f"🔌 [전원] {seat_id}번 좌석 → {action.upper()}")
                    except Exception as e:
                        print(f"⚠️ [전원 실패] {seat_id}번 ({ip} 플러그 {child_idx}) - {e}")

            power_queue.task_done()
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[전원 스레드 오류] {e}")


def wemos_selftest():
    """서버 시작 시 전 좌석 Wemos 에 /off 를 보내 연결 상태를 한 번에 확인"""
    print("=" * 46)
    print("🧪 Wemos 연결 점검 (/off 전송)")
    print("=" * 46)
    for sid in SEAT_IDS:
        ok = wemos_cmd(sid, "off")
        print(f"{'✅' if ok else '❌'} 좌석 {sid}: {SEAT_WEMOS_IPS.get(sid)}")
    print("=" * 46)


# =============================================================================
# 4. 상태 판정 및 하드웨어 명령 추가
# =============================================================================
STATE_TABLE = {
    "A": {"name": "정상점유", "color": "#22c55e", "led": "occupied", "power": True},
    "B": {"name": "사석화경고", "color": "#f59e0b", "led": "abandoned_warning", "power": True},
    "C": {"name": "사석화확정", "color": "#ef4444", "led": "abandoned_terminated", "power": False},
    "D": {"name": "무단사용", "color": "#dc2626", "led": "illegal", "power": False},
    "E": {"name": "빈자리", "color": "#94a3b8", "led": "vacant", "power": False},
}


def is_ticketed(seat_id):
    for seat in seats:
        if seat["id"] == seat_id:
            return seat["status"] == "OCCUPIED"
    return False


def get_phone(seat_id):
    for seat in seats:
        if seat["id"] == seat_id:
            return seat["phone"]
    return None


def vacant_seconds(seat_id):
    return time.time() - vision[seat_id]["last_seen"]


def judge_state(person, ticketed, vac_seconds, in_grace=False):
    if ticketed:
        if person:
            return "A"
        if vac_seconds >= TERMINATE_SECONDS:
            return "C"
        if vac_seconds >= WARN_SECONDS:
            return "B"
        return "A"
    else:
        if person and not in_grace:
            return "D"
        return "E"


def on_state_change(seat_id, old, new):
    rule = STATE_TABLE[new]

    # 🌟 1. 하드웨어 명령을 각자의 대기열에 넣기 (웹 딜레이 방지)
    led_queue.put((seat_id, rule["led"]))
    power_queue.put((seat_id, rule["power"]))

    # 🌟 2. 스마트폰 사용자 푸시 알림
    if new == "B":
        push_notification_to_user(seat_id, "⚠️ [알림] 자리 비움 경고", f"{seat_id}번 좌석 자리 비움이 감지되었습니다.", "AWAY_WARN")
    elif new == "C":
        push_notification_to_user(seat_id, "🚨 [알림] 이용 종료 안내", f"{seat_id}번 좌석이 장기 이석으로 조치되었습니다.", "AWAY_TERMINATED")

    # 🌟 3. 관리자 화면 로그 알림
    if new in ALERT_STATES:
        msg_template = ALERT_MESSAGES.get(new, f"{seat_id}번 좌석 상태 변경 ({rule['name']})")
        alert_log.insert(0, {"seat_id": seat_id, "message": msg_template.format(seat=seat_id), "at": datetime.now().strftime("%H:%M:%S"), "phone": get_phone(seat_id)})
        del alert_log[20:]

    event_log.insert(0, {"seat_id": seat_id, "from": old or "-", "to": new, "at": datetime.now().strftime("%H:%M:%S")})
    del event_log[30:]


def evaluate_seat(seat_id):
    person = vision[seat_id]["person"]
    ticketed = is_ticketed(seat_id)
    vac = vacant_seconds(seat_id)
    in_grace = time.time() < grace_until[seat_id]

    new = judge_state(person, ticketed, vac, in_grace)
    old = current_state[seat_id]
    if new != old:
        current_state[seat_id] = new
        on_state_change(seat_id, old, new)
        return {"seat_id": seat_id, "from": old, "to": new}
    return None


def evaluate_all():
    changes = []
    for sid in SEAT_IDS:
        c = evaluate_seat(sid)
        if c:
            changes.append(c)
    return changes


def monitor_loop():
    while True:
        try:
            with lock:
                evaluate_all()
        except Exception as e:
            print(f"[감시 오류] {e}")
        time.sleep(MONITOR_INTERVAL)


def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=led_worker, daemon=True).start()
    threading.Thread(target=blink_worker, daemon=True).start()
    threading.Thread(target=power_worker, daemon=True).start()


def snapshot():
    with lock:
        return [
            {
                "seat_id": sid,
                "state": current_state[sid] or "E",
                "state_name": STATE_TABLE[current_state[sid]]["name"] if current_state[sid] else "빈자리",
                "color": STATE_TABLE[current_state[sid]]["color"] if current_state[sid] else "#94a3b8",
                "person": vision[sid]["person"],
                "ticketed": is_ticketed(sid),
                "phone": get_phone(sid),
                "vacant_seconds": round(vacant_seconds(sid), 1) if not vision[sid]["person"] else 0,
            }
            for sid in SEAT_IDS
        ]


# =============================================================================
# 5. Flask API
# =============================================================================
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/vision")
@app.route("/admin")
def vision_page():
    return render_template("vision.html")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    if data.get("pin", "") != ADMIN_PIN:
        return jsonify({"success": False, "message": "비밀번호 오류."}), 403
    return jsonify({"success": True, "message": "관리자 모드 전환 성공"})


# 🌟 LED 단독 테스트용 (브라우저에서 바로 호출 가능)
#    예) http://192.168.0.113:5000/api/led_test/1/on
@app.route("/api/led_test/<int:seat_id>/<cmd>", methods=["GET"])
def led_test(seat_id, cmd):
    if cmd not in ("on", "off"):
        return jsonify({"success": False, "message": "cmd 는 on 또는 off"}), 400
    with blink_lock:
        blink_seats.discard(seat_id)
    ok = wemos_cmd(seat_id, cmd)
    return jsonify({"success": ok, "seat_id": seat_id, "cmd": cmd, "ip": SEAT_WEMOS_IPS.get(seat_id)})


@app.route("/api/vision", methods=["GET"])
def api_vision():
    with lock:
        conf_data = {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS, "warn_seconds": WARN_SECONDS, "terminate_seconds": TERMINATE_SECONDS}
        return jsonify({
            "seats": snapshot(),
            "events": event_log[:15], "alerts": alert_log[:10], "notices": notice_log[:10],
            "config": conf_data, "thresholds": conf_data,
            "hardware_enabled": HARDWARE_ENABLED,
            "now": datetime.now().strftime("%H:%M:%S")
        })


@app.route("/api/seats", methods=["GET"])
def get_seats():
    with lock:
        conf_data = {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS}
        return jsonify({
            "seats": [{"id": s["id"], "seat_id": s["id"], "ticket_status": "issued" if s["status"] == "OCCUPIED" else "none", "issued": (s["status"] == "OCCUPIED"), "status": s["status"], "phone": s["phone"]} for s in seats],
            "config": conf_data, "thresholds": conf_data, "hardware_enabled": HARDWARE_ENABLED
        })


@app.route("/api/vision/config", methods=["GET"])
def api_vision_config():
    global WARN_SECONDS, TERMINATE_SECONDS
    warn, term = request.args.get("warn"), request.args.get("term")
    if warn:
        WARN_SECONDS = int(warn)
    if term:
        TERMINATE_SECONDS = int(term)
    conf_data = {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS}
    return jsonify({"success": True, "warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS, "config": conf_data, "thresholds": conf_data})


@app.route("/api/issue", methods=["POST"])
@app.route("/api/issue/<int:seat_id_param>", methods=["POST"])
def issue_seat(seat_id_param=None):
    data = request.get_json(silent=True) or {}
    sid = int(seat_id_param or data.get("seat_id", 0))
    phone = data.get("phone", "").strip()
    if not PHONE_REGEX.match(phone):
        return jsonify({"success": False, "message": "010-XXXX-XXXX 형식으로 입력해주세요."}), 400
    with lock:
        for seat in seats:
            if seat["id"] == sid:
                if seat["status"] == "OCCUPIED":
                    return jsonify({"success": False, "message": "이미 사용중인 좌석입니다."}), 400
                seat["status"] = "OCCUPIED"
                seat["phone"] = phone
                vision[sid]["last_seen"] = time.time()
                grace_until[sid] = 0.0
                evaluate_all()
                return jsonify({"success": True, "message": f"{sid}번 좌석 발권 완료."})
    return jsonify({"success": False}), 404


@app.route("/api/return", methods=["POST"])
@app.route("/api/return/<int:seat_id_param>", methods=["POST"])
def return_seat(seat_id_param=None):
    data = request.get_json(silent=True) or {}
    sid = int(seat_id_param or data.get("seat_id", 0))
    phone = data.get("phone", "").strip()
    with lock:
        for seat in seats:
            if seat["id"] == sid:
                if seat["phone"] != phone:
                    return jsonify({"success": False, "message": "번호 불일치."}), 400
                seat["status"] = "EMPTY"
                seat["phone"] = None
                grace_until[sid] = time.time() + RETURN_GRACE_SECONDS
                evaluate_all()
                return jsonify({"success": True, "message": f"{sid}번 좌석 반납 완료."})
    return jsonify({"success": False}), 404


@app.route("/api/admin/force_return", methods=["POST"])
@app.route("/api/admin/force_return/<int:seat_id_param>", methods=["POST"])
def force_return(seat_id_param=None):
    data = request.get_json(silent=True) or {}
    sid = int(seat_id_param or data.get("seat_id", 0))
    if data.get("pin", "") != ADMIN_PIN:
        return jsonify({"success": False, "message": "비밀번호 오류."}), 403
    with lock:
        for seat in seats:
            if seat["id"] == sid:
                seat["status"] = "EMPTY"
                seat["phone"] = None
                grace_until[sid] = time.time() + RETURN_GRACE_SECONDS
                evaluate_all()
                push_notification_to_user(sid, "🚨 [알림] 강제 반납", "퇴실 처리되었습니다.", "FORCE_RETURN")
                return jsonify({"success": True, "message": f"{sid}번 반납 완료."})
    return jsonify({"success": False}), 404


@app.route("/detect", methods=["POST"])
@app.route("/control", methods=["POST"])
def receive_detection():
    data = request.get_json(silent=True) or {}
    items = data.get("detections") or data.get("seats") or ([data] if "seat_id" in data else [])
    with lock:
        for item in items:
            sid = item.get("seat_id")
            if sid is None or int(sid) not in SEAT_IDS:
                continue
            sid = int(sid)
            person = item.get("person")
            if person is None:
                person = item.get("presence")
            if person is None:
                st = str(item.get("status", "")).lower()
                person = st in ("occupied", "illegal", "person", "1", "true")
            vision[sid]["person"] = bool(person)
            vision[sid]["last_packet"] = time.time()
            if person:
                vision[sid]["last_seen"] = time.time()
        evaluate_all()
    return jsonify({"success": True}), 200


@app.route("/api/events/<int:seat_id>", methods=["GET"])
def sse_events(seat_id):
    def event_stream():
        q = get_or_create_queue(seat_id)
        while True:
            try:
                msg = q.get(timeout=20.0)
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    start_threads()
    if HARDWARE_ENABLED and LED_ENABLED:
        wemos_selftest()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE, threaded=True)
