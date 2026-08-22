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
    1: ("192.168.0.193", 1),
    2: ("192.168.0.193", 2),
    3: ("192.168.0.193", 3),
    4: ("192.168.0.191", 1),
    5: ("192.168.0.191", 2),
    6: ("192.168.0.191", 3),
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
hw_queue = queue.Queue() # 하드웨어 명령 대기열

def get_or_create_queue(seat_id):
    if seat_id not in user_event_queues:
        user_event_queues[seat_id] = queue.Queue()
    return user_event_queues[seat_id]

def push_notification_to_user(seat_id, title, message, event_type="WARNING"):
    q = get_or_create_queue(seat_id)
    q.put({"type": event_type, "title": title, "message": message, "timestamp": datetime.now().strftime("%H:%M:%S")})

# =============================================================================
# 3. 하드웨어 제어 작업 스레드 (대기열 처리)
# =============================================================================
def hw_worker():
    """명령 대기열에서 작업을 꺼내 Wemos와 Tapo를 제어하는 백그라운드 스레드"""
    print("[HW 스레드] 대기열 처리 시작...")
    while True:
        try:
            task = hw_queue.get(timeout=2.0)
            target, seat_id, arg = task
            
            if not HARDWARE_ENABLED:
                continue

            # 1. Wemos LED 제어
            if target == "led" and LED_ENABLED:
                ip = SEAT_WEMOS_IPS.get(seat_id)
                if ip:
                    url = f"http://{ip}/led?state={arg}"
                    try:
                        requests.get(url, timeout=WEMOS_TIMEOUT)
                        print(f"💡 [LED] {seat_id}번 좌석 -> {arg} 전송 성공")
                    except Exception as e:
                        print(f"⚠️ [LED 실패] {seat_id}번 ({ip}) - {e}")
            
            # 2. Tapo 스마트 플러그 전원 제어
            elif target == "power" and POWER_ENABLED:
                ip, child_idx = SEAT_POWER.get(seat_id, (None, None))
                if ip and child_idx is not None:
                    action = "on" if arg else "off"
                    cmd = [
                        "kasa", "--host", ip,
                        "--username", TAPO_USER, "--password", TAPO_PASS,
                        action, "--child-index", str(child_idx)
                    ]
                    try:
                        # subprocess로 터미널 명령어(kasa) 직접 실행 (웹 프리징 방지)
                        subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                        print(f"🔌 [전원] {seat_id}번 좌석 -> {action.upper()} 전송 성공")
                    except Exception as e:
                        print(f"⚠️ [전원 실패] {seat_id}번 ({ip} 플러그 {child_idx}) - {e}")
                        
            hw_queue.task_done()
        except queue.Empty:
            pass
        except Exception as e:
            print(f"[HW 작업 스레드 오류] {e}")

# =============================================================================
# 4. 상태 판정 및 하드웨어 명령 추가
# =============================================================================
# 상태별 LED 및 전원 매핑 테이블
STATE_TABLE = {
    "A": {"name": "정상점유", "color": "#22c55e", "led": "occupied", "power": True},
    "B": {"name": "사석화경고", "color": "#f59e0b", "led": "abandoned_warning", "power": True},
    "C": {"name": "사석화확정", "color": "#ef4444", "led": "abandoned_terminated", "power": False},
    "D": {"name": "무단사용", "color": "#dc2626", "led": "illegal", "power": False},
    "E": {"name": "빈자리", "color": "#94a3b8", "led": "vacant", "power": False},
}

def is_ticketed(seat_id):
    for seat in seats:
        if seat["id"] == seat_id: return seat["status"] == "OCCUPIED"
    return False

def get_phone(seat_id):
    for seat in seats:
        if seat["id"] == seat_id: return seat["phone"]
    return None

def vacant_seconds(seat_id):
    return time.time() - vision[seat_id]["last_seen"]

def judge_state(person, ticketed, vac_seconds, in_grace=False):
    if ticketed:
        if person: return "A"
        if vac_seconds >= TERMINATE_SECONDS: return "C"
        if vac_seconds >= WARN_SECONDS: return "B"
        return "A"
    else:
        if person and not in_grace: return "D"
        return "E"

def on_state_change(seat_id, old, new):
    rule = STATE_TABLE[new]
    
    # 🌟 1. 하드웨어 명령 대기열(Queue)에 즉시 넣기 (웹 딜레이 방지)
    hw_queue.put(("led", seat_id, rule["led"]))
    hw_queue.put(("power", seat_id, rule["power"]))
    
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
        if c: changes.append(c)
    return changes

def monitor_loop():
    while True:
        try:
            with lock: evaluate_all()
        except Exception as e:
            print(f"[감시 오류] {e}")
        time.sleep(MONITOR_INTERVAL)

def start_threads():
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=hw_worker, daemon=True).start() # 하드웨어 작업 스레드 시작

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
def index(): return render_template("index.html")

@app.route("/vision")
@app.route("/admin")
def vision_page(): return render_template("vision.html")

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    if data.get("pin", "") != ADMIN_PIN: return jsonify({"success": False, "message": "비밀번호 오류."}), 403
    return jsonify({"success": True, "message": "관리자 모드 전환 성공"})

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
    if warn: WARN_SECONDS = int(warn)
    if term: TERMINATE_SECONDS = int(term)
    conf_data = {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS}
    return jsonify({"success": True, "warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS, "config": conf_data, "thresholds": conf_data})

@app.route("/api/issue", methods=["POST"])
@app.route("/api/issue/<int:seat_id_param>", methods=["POST"])
def issue_seat(seat_id_param=None):
    data = request.get_json(silent=True) or {}
    sid = int(seat_id_param or data.get("seat_id", 0))
    phone = data.get("phone", "").strip()
    if not PHONE_REGEX.match(phone): return jsonify({"success": False, "message": "010-XXXX-XXXX 형식으로 입력해주세요."}), 400
    with lock:
        for seat in seats:
            if seat["id"] == sid:
                if seat["status"] == "OCCUPIED": return jsonify({"success": False, "message": "이미 사용중인 좌석입니다."}), 400
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
                if seat["phone"] != phone: return jsonify({"success": False, "message": "번호 불일치."}), 400
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
    if data.get("pin", "") != ADMIN_PIN: return jsonify({"success": False, "message": "비밀번호 오류."}), 403
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
            if sid is None or int(sid) not in SEAT_IDS: continue
            sid = int(sid)
            person = item.get("person")
            if person is None: person = item.get("presence")
            if person is None:
                st = str(item.get("status", "")).lower()
                person = st in ("occupied", "illegal", "person", "1", "true")
            vision[sid]["person"] = bool(person)
            vision[sid]["last_packet"] = time.time()
            if person: vision[sid]["last_seen"] = time.time()
        evaluate_all()
    return jsonify({"success": True}), 200

@app.route("/api/events/<int:seat_id>", methods=["GET"])
def sse_events(seat_id):
    def event_stream():
        q = get_or_create_queue(seat_id)
        while True:
            try: msg = q.get(timeout=20.0); yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty: yield ": ping\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    start_threads()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE, threaded=True)