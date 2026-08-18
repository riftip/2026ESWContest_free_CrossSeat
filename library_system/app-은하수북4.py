from flask import Flask, render_template, jsonify, request
import re  # 정규표현식 라이브러리 추가

# ===== [추가] 좌석 감지/제어를 위한 라이브러리 =====
import os
import time
import threading
from datetime import datetime

app = Flask(__name__)

# 관리자 비밀번호
ADMIN_PIN = "1234"

# 010-1234-5678 형식 검증용 정규표현식
PHONE_REGEX = re.compile(r'^010-\d{4}-\d{4}$')

# 6개 좌석 데이터
seats = [
    {"id": i, "status": "EMPTY", "phone": None} for i in range(1, 7)
]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

@app.route('/api/seats', methods=['GET'])
def get_seats():
    return jsonify({"seats": seats})

@app.route('/api/issue/<int:seat_id>', methods=['POST'])
def issue_seat(seat_id):
    data = request.get_json()
    phone = data.get('phone')

    # [수정됨] 전화번호 형식 검증 로직 추가
    if not phone or not PHONE_REGEX.match(phone):
        return jsonify({"success": False, "message": "전화번호는 반드시 010-1234-5678 형식(하이픈 포함)으로 입력해야 합니다."}), 400

    for seat in seats:
        if seat["id"] == seat_id:
            if seat["status"] == "EMPTY":
                seat["status"] = "OCCUPIED"
                seat["phone"] = phone
                return jsonify({"success": True, "message": f"{seat_id}번 좌석 발권이 완료되었습니다."})
            else:
                return jsonify({"success": False, "message": "이미 사용중인 좌석입니다."}), 400
    return jsonify({"success": False, "message": "존재하지 않는 좌석입니다."}), 404

@app.route('/api/return/<int:seat_id>', methods=['POST'])
def return_seat(seat_id):
    data = request.get_json()
    phone = data.get('phone')

    for seat in seats:
        if seat["id"] == seat_id:
            if seat["status"] == "OCCUPIED":
                if seat["phone"] == phone:
                    seat["status"] = "EMPTY"
                    seat["phone"] = None
                    return jsonify({"success": True, "message": f"{seat_id}번 좌석 반납이 완료되었습니다."})
                else:
                    return jsonify({"success": False, "message": "번호를 확인해주세요."}), 400
            else:
                return jsonify({"success": False, "message": "빈 좌석입니다."}), 400
    return jsonify({"success": False, "message": "존재하지 않는 좌석입니다."}), 404

@app.route('/api/admin/force_return/<int:seat_id>', methods=['POST'])
def force_return(seat_id):
    data = request.get_json()
    pin = data.get('pin')

    if pin != ADMIN_PIN:
        return jsonify({"success": False, "message": "관리자 비밀번호가 틀렸습니다."}), 403

    for seat in seats:
        if seat["id"] == seat_id:
            seat["status"] = "EMPTY"
            seat["phone"] = None
            return jsonify({"success": True, "message": f"{seat_id}번 좌석이 강제 반납 되었습니다."})

    return jsonify({"success": False, "message": "존재하지 않는 좌석입니다."}), 404


# #############################################################################
# #############################################################################
##
##   여기부터가 추가된 부분입니다. 위쪽 원본 코드는 하나도 바뀌지 않았습니다.
##
##   [하는 일]
##     1) ssh2팀이 보낸 HTTP POST 를 받는다            (/detect 와 /control 둘 다 받음)
##     2) 위쪽 seats 리스트의 발권 정보와 대조한다      ← 요청하신 "대조"
##     3) A / B / C / D / E 상태를 판정한다
##     4) 상태가 바뀐 순간에만 LED·전원 명령을 쏜다     (엣지 트리거)
##     5) /vision 페이지에서 눈으로 확인한다
##
# #############################################################################
# #############################################################################


# =============================================================================
#  [설정]
# =============================================================================

DEBUG_MODE = True               # 원본과 동일하게 디버그 유지
HARDWARE_ENABLED = False        # Wemos LED / Tapo 콘센트 연결되면 True 로 바꿀 것

WARN_SECONDS = 180              # 이만큼 자리 비면 -> B (사석화 경고).  3분
TERMINATE_SECONDS = 300         # 이만큼 자리 비면 -> C (이용종료).     5분

MONITOR_INTERVAL = 2.0          # 몇 초마다 좌석 상태를 다시 판정할지

SEAT_IDS = [1, 2, 3, 4, 5, 6]


# =============================================================================
#  [상태 정의]  A / B / C / D / E
# =============================================================================
#
#  판정에 쓰는 재료는 3개다.
#
#    person          : 지금 사람이 앉아 있냐
#                      → 카메라(ssh2팀)만 알 수 있다. HTTP로 받는다.
#
#    ticketed        : 발권(결제)했냐
#                      → 위쪽 seats 리스트가 갖고 있다. 여기서 직접 조회한다.
#                         seats[i]["status"] == "OCCUPIED" 이면 발권된 것.
#                      ★ 이게 요청하신 "본래 좌석 발권과 대조" 부분이다 ★
#
#    vacant_seconds  : 몇 초 동안 비어 있었냐
#                      → 이 파일이 직접 계산한다. (아래 타이머 설명 참고)
#
#  ┌────┬─────────────┬──────────────────────────────┬──────────┬──────┐
#  │상태│ 이름         │ 조건                          │ LED      │ 전원 │
#  ├────┼─────────────┼──────────────────────────────┼──────────┼──────┤
#  │ A  │ 정상점유     │ 발권O + (사람O 또는 3분 미만) │ 꺼짐     │ ON   │
#  │ B  │ 사석화 경고  │ 발권O + 사람X + 3분 이상      │ 점멸     │ ON   │
#  │ C  │ 사석화 확정  │ 발권O + 사람X + 5분 이상      │ 계속켜짐 │ OFF  │
#  │ D  │ 무단사용     │ 발권X + 사람O                 │ 계속켜짐 │ ON   │
#  │ E  │ 빈자리       │ 발권X + 사람X                 │ 꺼짐     │ OFF  │
#  └────┴─────────────┴──────────────────────────────┴──────────┴──────┘
# =============================================================================

STATE_TABLE = {
    "A": {"name": "정상점유",   "led": "occupied",             "power": True,  "color": "#22c55e"},
    "B": {"name": "사석화경고", "led": "abandoned_warning",    "power": True,  "color": "#f59e0b"},
    "C": {"name": "사석화확정", "led": "abandoned_terminated", "power": False, "color": "#ef4444"},
    "D": {"name": "무단사용",   "led": "illegal",              "power": True,  "color": "#dc2626"},
    "E": {"name": "빈자리",     "led": "vacant",               "power": False, "color": "#94a3b8"},
}


def judge_state(person, ticketed, vacant_seconds):
    """
    사실 3개를 받아서 상태 문자 하나를 돌려준다.
    이 함수는 아무것도 바꾸지 않는다(부작용 없음). 그래서 테스트가 쉽다.

    ★ 검사 순서 주의 ★
    C(5분)를 B(3분)보다 먼저 검사해야 한다.
    반대로 쓰면 5분이 지나도 "3분 이상이네" 하고 B에서 걸려서 절대 C가 안 된다.
    """
    if ticketed:
        if person:
            return "A"                                  # 발권하고 앉아 있음
        if vacant_seconds >= TERMINATE_SECONDS:
            return "C"                                  # ← 5분을 먼저 검사!
        if vacant_seconds >= WARN_SECONDS:
            return "B"
        return "A"                                      # 발권했고 잠깐 비운 정도는 아직 정상
    else:
        return "D" if person else "E"


# =============================================================================
#  [발권 대조]  위쪽 seats 리스트에서 발권 여부를 읽어온다
# =============================================================================

def is_ticketed(seat_id):
    """
    ★ 발권 대조 함수 ★
    원본 app.py 의 seats 리스트를 그대로 읽는다. 별도 DB 없음.

    /api/issue 로 발권하면 status 가 "OCCUPIED" 가 되고,
    /api/return 이나 /api/admin/force_return 하면 "EMPTY" 로 돌아간다.
    그 값을 그대로 신뢰한다.
    """
    for seat in seats:
        if seat["id"] == seat_id:
            return seat["status"] == "OCCUPIED"
    return False


def get_phone(seat_id):
    """알림 보낼 전화번호 조회 (나중에 SMS 붙일 때 사용)"""
    for seat in seats:
        if seat["id"] == seat_id:
            return seat["phone"]
    return None


# =============================================================================
#  [타이머]  "마지막으로 사람이 보인 시각" 방식
# =============================================================================
#
#  스레드로 카운트다운을 돌리거나 알람을 맞출 필요가 전혀 없다. 원리는 이게 전부다.
#
#     사람이 보이면   ->  last_seen[좌석] = 지금시각      (시계를 0으로 리셋)
#     사람이 안 보이면 ->  아무것도 안 함                 (시각이 과거에 멈춰 있음)
#     공석시간        =  지금시각 - last_seen[좌석]
#
#  이 방식이 좋은 이유
#     - 좌석 6개가 완전히 독립적으로 계산된다
#     - 프로그램이 잠깐 멈췄다 돌아와도 시간이 정확하다
#     - ssh2팀이 데이터를 불규칙하게 보내도 시간 계산이 안 틀어진다
#
#  ★ 왜 타이머를 Flask(여기)에 뒀는가 ★
#  가짜 데이터 파일(fake_data_simple.py)은 내가 명령을 칠 때만 1회 발신한다.
#  즉 "1 vacant" 를 한 번 치고 가만히 있으면 그 뒤로는 아무 데이터도 안 온다.
#  타이머가 발신 쪽에 있으면 3분·5분이 지나도 아무 일이 일어나지 않는다.
#  그래서 여기에 두고, 아래 감시 스레드가 2초마다 스스로 시간을 확인하게 했다.
# =============================================================================

# 카메라(또는 가짜 데이터)로부터 받은 정보를 담아두는 곳
vision = {
    sid: {
        "person": False,            # 마지막으로 받은 "사람 있음/없음"
        "last_seen": time.time(),   # 마지막으로 사람이 보인 시각  ← 타이머의 핵심
        "confidence": 0.0,
        "last_packet": None,        # 마지막으로 데이터를 받은 시각 (연결 확인용)
    }
    for sid in SEAT_IDS
}

# 좌석별 직전 상태.  ← 엣지 트리거의 전부
current_state = {sid: None for sid in SEAT_IDS}

# 화면에 보여줄 기록들
event_log = []      # 상태 변화 이력
alert_log = []      # 관리자 조치 필요 알림

# 여러 스레드가 동시에 위 딕셔너리를 건드리므로 잠금장치가 필요하다
lock = threading.Lock()


def vacant_seconds(seat_id):
    """이 좌석이 몇 초 동안 비어 있었는지"""
    return time.time() - vision[seat_id]["last_seen"]


# =============================================================================
#  [하드웨어 제어]  지금은 연결 안 됐으므로 로그만 출력
# =============================================================================

def control_led(seat_id, led_state):
    """
    Wemos D1 Mini 에 HTTP GET 을 보내 LED 상태를 바꾼다.
    led_state: occupied / vacant / illegal / abandoned_warning / abandoned_terminated
    """
    if not HARDWARE_ENABLED:
        print(f"        [모의] 좌석{seat_id} LED -> {led_state}")
        return

    # ---- Wemos 연결되면 아래 주석을 풀고, WEMOS_IP 를 실제 IP로 채울 것 ----
    # import requests
    # WEMOS_IP = {1: "192.168.0.xxx", 2: "192.168.0.xxx", 3: "192.168.0.xxx",
    #             4: "192.168.0.xxx", 5: "192.168.0.xxx", 6: "192.168.0.xxx"}
    # try:
    #     requests.get(f"http://{WEMOS_IP[seat_id]}/set_state",
    #                  params={"state": led_state}, timeout=2)
    #     print(f"        좌석{seat_id} LED -> {led_state}")
    # except Exception as e:
    #     print(f"        좌석{seat_id} LED 실패: {e}")


def control_power(seat_id, on):
    """Tapo 스마트 멀티탭의 해당 구멍을 켜거나 끈다."""
    if not HARDWARE_ENABLED:
        print(f"        [모의] 좌석{seat_id} 전원 -> {'ON' if on else 'OFF'}")
        return

    # ---- Tapo 연결되면 아래 주석을 풀 것 ----
    # 좌석 -> (멀티탭 IP, 몇 번째 구멍). child_index 는 0부터 시작.
    # 구멍 번호 확인법:
    #   kasa --host 192.168.0.183 --username <메일> --password <비번> state
    #   -> 자식 기기 목록이 순서대로 나온다. 하나씩 켜보며 눈으로 확인할 것.
    #
    # TAPO_USER = "..."
    # TAPO_PASS = "..."
    # SEAT_POWER = {1: ("192.168.0.183", 0), 2: ("192.168.0.183", 1),
    #               3: ("192.168.0.183", 2), 4: ("192.168.0.185", 0),
    #               5: ("192.168.0.185", 1), 6: ("192.168.0.185", 2)}
    #
    # import asyncio
    # from kasa import Discover
    # async def _go(host, idx, turn_on):
    #     dev = await Discover.discover_single(host, username=TAPO_USER, password=TAPO_PASS)
    #     await dev.update()
    #     child = dev.children[idx]
    #     await (child.turn_on() if turn_on else child.turn_off())
    #     await dev.disconnect()
    # host, idx = SEAT_POWER[seat_id]
    # try:
    #     asyncio.run(_go(host, idx, on))
    # except Exception as e:
    #     print(f"        좌석{seat_id} 전원 실패: {e}")


def notify_admin(seat_id, message):
    """관리자 알림. 지금은 콘솔 + /vision 화면. 나중에 CoolSMS 붙이면 됨."""
    print(f"        [알림] {message}")
    alert_log.insert(0, {
        "seat_id": seat_id,
        "message": message,
        "at": datetime.now().strftime("%H:%M:%S"),
        "phone": get_phone(seat_id),
    })
    del alert_log[20:]      # 최근 20개만 유지

    # TODO: SMS 발송
    # phone = get_phone(seat_id)
    # if phone: requests.post("https://api.coolsms.co.kr/...", json={...})


# =============================================================================
#  [엣지 트리거]  상태가 "바뀐 순간"에만 명령을 쏜다
# =============================================================================
#
#  아래 감시 스레드는 2초마다 좌석을 판정한다.
#  좌석이 C 상태로 5분간 머물면 "전원 꺼" 명령이 150번 나가게 된다. 이건 재앙이다.
#     - Tapo 가 요청 폭주로 뻗는다 (전에 Input/output error 났던 것과 같은 계열)
#     - 관리자에게 알림이 150번 간다
#     - 로그가 쓰레기로 뒤덮인다
#
#  그래서 직전 상태를 기억해두고, 달라진 순간에만 명령을 보낸다.
#
#     판정 결과 :  A A A A B B B B B C C C C
#     명령 발사 :          ↑         ↑
#                       여기만     여기만
#
#  (전자공학에서 신호가 0->1 로 바뀌는 순간에만 반응하는 걸 엣지 트리거라 한다.
#   그 아이디어를 소프트웨어로 그대로 옮긴 것)
# =============================================================================

def on_state_change(seat_id, old, new):
    """★ 상태가 실제로 바뀐 순간에만 호출된다. 2초마다가 아니다. ★"""
    old_label = f"{old}({STATE_TABLE[old]['name']})" if old else "시작"
    new_label = f"{new}({STATE_TABLE[new]['name']})"
    print(f"  [변화] 좌석{seat_id}: {old_label} -> {new_label}")

    rule = STATE_TABLE[new]

    control_led(seat_id, rule["led"])        # 1) 조명 제어
    control_power(seat_id, rule["power"])    # 2) 전원 제어

    # 3) 관리자 알림 (C: 사석화 확정, D: 무단사용 일 때만)
    if new == "C":
        notify_admin(seat_id, f"{seat_id}번 자리 조치 필요 — 사석화로 이용종료 처리됨")
    elif new == "D":
        notify_admin(seat_id, f"{seat_id}번 자리 조치 필요 — 미발권 무단사용 감지")

    # 4) 이력 기록
    event_log.insert(0, {
        "seat_id": seat_id,
        "from": old or "-",
        "to": new,
        "at": datetime.now().strftime("%H:%M:%S"),
    })
    del event_log[30:]      # 최근 30개만 유지


def evaluate_seat(seat_id):
    """좌석 하나를 판정하고, 상태가 바뀌었으면 제어까지 실행한다."""
    person = vision[seat_id]["person"]
    ticketed = is_ticketed(seat_id)              # ← 발권 대조
    vac = vacant_seconds(seat_id)

    new = judge_state(person, ticketed, vac)     # ← 상태 판정

    old = current_state[seat_id]
    if new != old:                               # ★ 엣지 트리거 ★
        current_state[seat_id] = new
        on_state_change(seat_id, old, new)
        return {"seat_id": seat_id, "from": old, "to": new}
    return None                                  # 안 바뀌면 아무것도 안 한다


def evaluate_all():
    """좌석 6개 전부 판정"""
    changes = []
    for sid in SEAT_IDS:
        c = evaluate_seat(sid)
        if c:
            changes.append(c)
    return changes


# =============================================================================
#  [감시 스레드]  2초마다 스스로 시간을 확인한다
# =============================================================================
#
#  이게 있어야 "데이터를 안 보내도 3분 지나면 B로 넘어가는" 동작이 가능하다.
#  daemon=True 로 만들었으므로 Ctrl+C 로 서버를 끄면 이 스레드도 같이 죽는다.
# =============================================================================

def monitor_loop():
    print(f"[감시] {MONITOR_INTERVAL}초 주기 감시 스레드 시작 "
          f"(경고 {WARN_SECONDS}초 / 종료 {TERMINATE_SECONDS}초)")
    while True:
        try:
            with lock:
                evaluate_all()
        except Exception as e:
            print(f"[감시] 오류: {e}")
        time.sleep(MONITOR_INTERVAL)


def start_monitor():
    threading.Thread(target=monitor_loop, daemon=True).start()


# =============================================================================
#  [수신]  ssh2팀이 보낸 데이터를 받는 엔드포인트
# =============================================================================
#
#  ★ /detect 와 /control 둘 다 받는다 ★
#  fake_data_simple.py 와 test_http_sender.py 는 /detect 로 보내고,
#  ssh2_sender.py 는 /control 로 보낸다. 어느 쪽이든 그대로 동작한다.
#
#  methods=['POST'] 를 적어야 한다. 안 적으면 GET만 받아서 405 에러가 난다.
# =============================================================================

def normalize_payload(data):
    """
    발신 파일마다 JSON 모양이 조금씩 달라서, 여기서 하나로 통일한다.
    덕분에 발신 파일을 고치지 않고 그대로 쓸 수 있다.

    받아들이는 형식 3가지:

      (1) fake_data_simple.py / test_http_sender.py
          {"detections": [{"seat_id":1, "status":"vacant", "confidence":0.95}]}

      (2) test_http_sender.py 의 --seat 모드 (좌석 하나만)
          {"seat_id":1, "status":"vacant", "confidence":0.95}

      (3) ssh2_sender.py
          {"seats": [{"seat_id":1, "person":true, "ticketed":true, "vacant_seconds":0}]}

    돌려주는 모양:
          {1: {"person": False, "confidence": 0.95}, ...}
    """
    result = {}
    if not isinstance(data, dict):
        return result

    if "detections" in data:
        items = data["detections"]
    elif "seats" in data:
        items = data["seats"]
    elif "seat_id" in data:
        items = [data]                  # 좌석 하나만 온 경우
    else:
        return result

    if not isinstance(items, list):
        return result

    for item in items:
        if not isinstance(item, dict):
            continue
        sid = item.get("seat_id")
        if sid is None:
            continue
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        if sid not in SEAT_IDS:
            continue

        # person 을 알아낸다
        if "person" in item:
            person = bool(item["person"])
        else:
            # status 문자열을 person 으로 번역
            #   occupied / illegal -> 사람 있음
            #   vacant             -> 사람 없음
            status = str(item.get("status", "")).strip().lower()
            person = status in ("occupied", "illegal", "person", "true", "1")

        result[sid] = {
            "person": person,
            "confidence": float(item.get("confidence", 0.0) or 0.0),
        }

    return result


@app.route('/detect', methods=['POST'])
@app.route('/control', methods=['POST'])
def receive_detection():
    """
    ★ 여기가 HTTP 수신부 ★

    하는 일
      1) JSON 을 통일된 모양으로 정리한다
      2) 사람이 보이면 타이머를 리셋한다
      3) 발권 정보와 대조해서 A~E 를 판정한다
      4) 상태가 바뀐 좌석만 제어 명령을 쏜다
      5) 결과를 JSON 으로 돌려준다
    """
    data = request.get_json(silent=True)
    parsed = normalize_payload(data)

    if not parsed:
        return jsonify({
            "success": False,
            "message": "좌석 데이터를 못 읽었습니다. "
                       "{'detections':[{'seat_id':1,'status':'vacant'}]} 형태로 보내주세요."
        }), 400

    with lock:
        # --- 받은 정보로 타이머 갱신 ---
        for sid, info in parsed.items():
            vision[sid]["person"] = info["person"]
            vision[sid]["confidence"] = info["confidence"]
            vision[sid]["last_packet"] = time.time()

            if info["person"]:
                vision[sid]["last_seen"] = time.time()      # ★ 타이머 리셋 ★

        # --- 판정 + 엣지 트리거 ---
        changes = evaluate_all()

        # --- 지금 상태를 응답에 담아준다 (발신 쪽에서도 확인 가능) ---
        snapshot = [
            {
                "seat_id": sid,
                "state": current_state[sid],
                "state_name": STATE_TABLE[current_state[sid]]["name"] if current_state[sid] else None,
                "person": vision[sid]["person"],
                "ticketed": is_ticketed(sid),
                "vacant_seconds": round(vacant_seconds(sid), 1),
            }
            for sid in SEAT_IDS
        ]

    print(f"[수신] {len(parsed)}개 좌석 "
          + (f"| 변화 {len(changes)}건" if changes else "| 변화 없음"))

    return jsonify({
        "success": True,
        "received": len(parsed),
        "changes": changes,
        "seats": snapshot,
        "timestamp": datetime.now().isoformat(),
    }), 200


# =============================================================================
#  [조회]  상태를 JSON 으로 확인
# =============================================================================

@app.route('/api/vision', methods=['GET'])
def api_vision():
    with lock:
        data = [
            {
                "seat_id": sid,
                "state": current_state[sid],
                "state_name": STATE_TABLE[current_state[sid]]["name"] if current_state[sid] else None,
                "person": vision[sid]["person"],
                "ticketed": is_ticketed(sid),
                "phone": get_phone(sid),
                "vacant_seconds": round(vacant_seconds(sid), 1),
                "led": STATE_TABLE[current_state[sid]]["led"] if current_state[sid] else None,
                "power": STATE_TABLE[current_state[sid]]["power"] if current_state[sid] else None,
            }
            for sid in SEAT_IDS
        ]
    return jsonify({
        "seats": data,
        "thresholds": {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS},
        "hardware_enabled": HARDWARE_ENABLED,
        "events": event_log[:15],
        "alerts": alert_log[:10],
    })


@app.route('/api/vision/config', methods=['GET', 'POST'])
def api_vision_config():
    """
    테스트용 임계값 변경.
    3분·5분을 기다리기 싫을 때 브라우저에서 이 주소만 열면 된다.

      http://192.168.0.102:5000/api/vision/config?warn=10&term=20   ← 빠른 테스트
      http://192.168.0.102:5000/api/vision/config?warn=180&term=300 ← 원래대로
    """
    global WARN_SECONDS, TERMINATE_SECONDS

    warn = request.args.get('warn', type=int)
    term = request.args.get('term', type=int)

    if warn:
        WARN_SECONDS = warn
    if term:
        TERMINATE_SECONDS = term

    if warn or term:
        print(f"[설정] 임계값 변경 -> 경고 {WARN_SECONDS}초 / 종료 {TERMINATE_SECONDS}초")

    return jsonify({"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS})


# =============================================================================
#  [화면]  브라우저에서 눈으로 확인하는 페이지
# =============================================================================
#  기존 /admin 은 그대로 두고, 이건 /vision 이라는 새 주소로 만들었다.
#  접속:  http://192.168.0.102:5000/vision
# =============================================================================

@app.route('/vision')
def vision_page():
    with lock:
        cards = ""
        for sid in SEAT_IDS:
            st = current_state[sid] or "E"
            info = STATE_TABLE[st]
            ticketed = is_ticketed(sid)
            person = vision[sid]["person"]
            vac = vacant_seconds(sid)

            if person:
                time_txt = "사람 있음"
            elif ticketed:
                remain_warn = WARN_SECONDS - vac
                remain_term = TERMINATE_SECONDS - vac
                if remain_warn > 0:
                    time_txt = f"{vac:.0f}초 비움 · 경고까지 {remain_warn:.0f}초"
                elif remain_term > 0:
                    time_txt = f"{vac:.0f}초 비움 · 종료까지 {remain_term:.0f}초"
                else:
                    time_txt = f"{vac:.0f}초 비움 · 종료됨"
            else:
                time_txt = f"{vac:.0f}초 비움"

            phone = get_phone(sid) or "-"

            cards += f"""
            <div class="card" style="border-color:{info['color']}">
              <div class="row">
                <span class="seatno">좌석 {sid}</span>
                <span class="badge" style="background:{info['color']}">{st} · {info['name']}</span>
              </div>
              <div class="meta">발권 <b>{'O' if ticketed else 'X'}</b> · {phone}</div>
              <div class="meta">{time_txt}</div>
              <div class="hw">LED <code>{info['led']}</code> · 전원 <code>{'ON' if info['power'] else 'OFF'}</code></div>
            </div>"""

        alerts_html = "".join(
            f"<li><span class='t'>{a['at']}</span> {a['message']}</li>"
            for a in alert_log[:8]
        ) or "<li class='empty'>조치할 알림 없음</li>"

        events_html = "".join(
            f"<li><span class='t'>{e['at']}</span> 좌석{e['seat_id']} "
            f"{e['from']} → {e['to']}</li>"
            for e in event_log[:12]
        ) or "<li class='empty'>아직 상태 변화 없음</li>"

    hw_banner = ("" if HARDWARE_ENABLED else
                 "<div class='warn'>하드웨어 미연결 — LED·전원은 터미널에 로그만 출력됩니다</div>")

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>좌석 감지 상태</title>
<meta http-equiv="refresh" content="2">
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
       background:#0f172a; color:#e2e8f0; padding:28px; margin:0; }}
h1 {{ font-size:21px; margin:0 0 4px; }}
.sub {{ color:#64748b; font-size:13px; margin-bottom:18px; }}
.warn {{ background:#f59e0b; color:#1a1a1a; padding:8px 14px; border-radius:7px;
        display:inline-block; font-size:12.5px; font-weight:600; margin-bottom:18px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; max-width:940px; }}
.card {{ background:#1e293b; border:2px solid; border-radius:11px; padding:15px; }}
.row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }}
.seatno {{ font-size:15.5px; font-weight:700; }}
.badge {{ padding:3px 10px; border-radius:5px; font-size:12px; font-weight:700; color:#fff; }}
.meta {{ font-size:12.5px; color:#cbd5e1; margin-top:5px; }}
.hw {{ font-size:11.5px; color:#64748b; margin-top:9px;
      border-top:1px solid #334155; padding-top:8px; }}
code {{ background:#0f172a; padding:1px 5px; border-radius:3px; font-size:11px; }}
h2 {{ font-size:15px; margin:28px 0 9px; }}
ul {{ list-style:none; padding:0; margin:0; max-width:940px; font-size:13px; }}
li {{ background:#1e293b; padding:9px 13px; border-radius:7px; margin-bottom:4px; }}
.t {{ color:#64748b; margin-right:9px; font-variant-numeric:tabular-nums; }}
.empty {{ color:#64748b; }}
.tips {{ margin-top:26px; font-size:12px; color:#64748b; max-width:940px; line-height:1.7; }}
.tips code {{ color:#94a3b8; }}
</style></head><body>

<h1>좌석 감지 · 제어 상태</h1>
<div class="sub">{datetime.now().strftime('%H:%M:%S')} · 2초마다 자동 새로고침 ·
  임계값 경고 {WARN_SECONDS}초 / 종료 {TERMINATE_SECONDS}초</div>
{hw_banner}

<div class="grid">{cards}</div>

<h2>조치 필요</h2>
<ul>{alerts_html}</ul>

<h2>상태 변화 이력 <span style="color:#64748b;font-weight:400;font-size:12px">
  (엣지 트리거가 발동한 시점)</span></h2>
<ul>{events_html}</ul>

<div class="tips">
빠른 테스트: <code>/api/vision/config?warn=10&amp;term=20</code> 로 임계값을 10초·20초로 줄일 수 있습니다.<br>
발권은 <code>/</code> 페이지, 강제반납은 <code>/admin</code> 페이지에서 하면 이 화면에 바로 반영됩니다.
</div>

</body></html>"""


# =============================================================================
#  [실행]
# =============================================================================
#  원본의 app.run(...) 은 그대로 두고, 그 위에 감시 스레드 시작만 추가했다.
#
#  ★ WERKZEUG_RUN_MAIN 검사가 왜 필요한가 ★
#  debug=True 면 Flask 가 파일을 자동 재시작하려고 프로세스를 두 개 띄운다.
#  그냥 스레드를 시작하면 감시 스레드가 두 개 돌면서
#  current_state 가 두 벌 생기고 엣지 트리거가 엉뚱하게 동작한다.
#  그래서 실제 작업 프로세스에서만 스레드를 띄운다.
# =============================================================================

if __name__ == '__main__':
    if (not DEBUG_MODE) or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_monitor()

    print("=" * 62)
    print("  스터디카페 좌석 관리 서버")
    print("=" * 62)
    print("  발권 화면        http://<라파이IP>:5000/")
    print("  관리자 화면      http://<라파이IP>:5000/admin")
    print("  감지/제어 화면   http://<라파이IP>:5000/vision      <- 추가됨")
    print("  데이터 수신      POST /detect  또는  POST /control  <- 추가됨")
    print("=" * 62)

    app.run(host='0.0.0.0', port=5000, debug=DEBUG_MODE)
