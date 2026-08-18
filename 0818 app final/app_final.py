# -*- coding: utf-8 -*-
"""
=============================================================================
  app.py  --  스터디카페 좌석 관리 서버 (전부 이 파일 하나)
=============================================================================

  실행:  python app.py

  화면
      http://<라즈베리파이IP>:5000/          손님용 발권 키오스크
      http://<라즈베리파이IP>:5000/vision    감지 상태 + 관리자 (한 페이지)
      POST  /detect  또는  /control          YOLO(또는 fake.py)가 보내는 데이터

  파일
      app.py                  ← 이 파일
      fake.py                 ← YOLO 없이 손으로 상태를 만들어보는 테스트 도구
      templates/index.html    ← 손님용 키오스크
      templates/vision.html   ← 감지 + 관리자

  ---------------------------------------------------------------------------
  ★ 값을 고쳐야 할 때는 바로 아래 [1. 설정] 구역만 보면 됩니다 ★
    그 아래 코드에는 IP나 비밀번호를 직접 적어두지 않았습니다.
  ---------------------------------------------------------------------------
=============================================================================
"""

import asyncio
import os
import queue
import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, render_template, request


# #############################################################################
#
#   1. 설정  ★★★ 고칠 일이 있으면 여기만 보면 됩니다 ★★★
#
# #############################################################################

# ─────────────────────────────────────────────────────────────── ★ 수정 1
#  라즈베리파이 IP (터미널 안내 문구 출력용. 동작에는 영향 없음)
#  확인 방법:  hostname -I
PI_IP = "192.168.0.113"

#  관리자 비밀번호. /vision 페이지에서 강제반납할 때 입력합니다.
ADMIN_PIN = "1234"
# ────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────── ★ 수정 2
#  좌석별 Wemos D1 Mini IP (LED 경고등)
#
#  IP 확인 방법
#    (1) Wemos 를 USB 로 PC 에 꽂고 시리얼 모니터를 열면 부팅 시 IP 를 출력
#    (2) 공유기 관리자 페이지(192.168.0.1) 의 "연결된 기기 목록"
#    (3) 라즈베리파이에서:  sudo apt install -y arp-scan
#                          sudo arp-scan --localnet
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
#  좌석별 Tapo 콘센트.  좌석번호 : (멀티탭 IP, 구멍 번호)
#  구멍 번호(child-index)는 0부터 셉니다. 멀티탭 1개에 구멍 3개.
#
#  ★ 어느 구멍이 몇 번인지 확인하는 방법 (라즈베리파이 터미널)
#      source ~/cafe/venv/bin/activate
#      kasa --host 192.168.0.183 --username <메일> --password '<비번>' state
#      kasa --host 192.168.0.183 --username <메일> --password '<비번>' off --child-index 0
#    구멍을 하나씩 꺼보면서 실제로 어느 자리 전원이 끊기는지 눈으로 확인한 뒤
#    아래 표를 그 결과에 맞게 고치세요.
SEAT_POWER = {
    1: ("192.168.0.190", 3),
    2: ("192.168.0.190", 2),
    3: ("192.168.0.190", 1),
    4: ("192.168.0.192", 3),
    5: ("192.168.0.192", 2),
    6: ("192.168.0.192", 1),
}
# ────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────── ★ 수정 4
#  Tapo 앱 로그인 계정 (멀티탭 자체 비밀번호가 아니라 앱 계정입니다)
#
#  ⚠ 이 파일을 GitHub 에 올리면 계정이 그대로 공개됩니다.
#    포트폴리오로 공개할 계획이면 라즈베리파이에서 환경변수를 쓰세요.
#        export TAPO_USER="메일주소"
#        export TAPO_PASS="비밀번호"
#    환경변수가 없으면 뒤에 적힌 기본값을 사용합니다.
TAPO_USER = os.environ.get("TAPO_USER", "cycy0125@cau.ac.kr")
TAPO_PASS = os.environ.get("TAPO_PASS", "young040125")
# ────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────── ★ 수정 5
#  하드웨어 제어 스위치
#    False = 실제 명령을 안 보내고 터미널에 로그만 출력 (하드웨어 없이 테스트)
#    True  = Wemos LED / Tapo 콘센트에 실제로 명령 전송
HARDWARE_ENABLED = False

LED_ENABLED = True      # LED 만 따로 끄고 싶을 때
POWER_ENABLED = True    # 콘센트만 따로 끄고 싶을 때
# ────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────── ★ 수정 6
#  사석화 판정 시간(초)
#  시연 때 3분·5분을 기다리기 싫으면 아래 값을 10, 20 으로 줄이거나
#  브라우저에서 /api/vision/config?warn=10&term=20 을 열면
#  서버를 껐다 켜지 않고도 바꿀 수 있습니다.
WARN_SECONDS = 180          # 이만큼 자리 비면 -> B (사석화 경고)   3분
TERMINATE_SECONDS = 300     # 이만큼 자리 비면 -> C (이용 종료)     5분

#  반납 직후 유예 시간(초)
#  반납 버튼을 눌러도 손님이 짐을 챙기는 동안 카메라엔 사람이 계속 보입니다.
#  유예가 없으면 반납하자마자 D(무단사용) 경고가 떠버립니다. 0 = 기능 끔.
RETURN_GRACE_SECONDS = 60
# ────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────── ★ 수정 7
#  /vision 화면 오른쪽 위에 팝업을 띄울 상태
#      A 정상점유 / B 사석화경고 / C 사석화확정 / D 무단사용 / E 빈자리
#
#  ★ E(빈자리)는 손님이 반납할 때마다 뜹니다. 번거로우면 "E" 만 지우세요.
ALERT_STATES = ["C", "D", "E"]

ALERT_MESSAGES = {
    "C": "{seat}번 좌석 · 사석화 확정 — 5분 이상 비어 이용을 종료했습니다. 확인 필요합니다.",
    "D": "{seat}번 좌석 · 무단 사용 — 발권하지 않은 이용자가 앉아 있습니다. 확인이 필요합니다.",
    "E": "{seat}번 좌석 · 이용 종료 — 좌석이 비었고 전원이 차단되었습니다.",
}
# ────────────────────────────────────────────────────────────────────────


# ── 그 밖의 설정 (거의 고칠 일 없음) ──────────────────────────────────────
SEAT_IDS = [1, 2, 3, 4, 5, 6]
SERVER_HOST = "0.0.0.0"     # 같은 공유기 안 모든 기기에서 접속 허용
SERVER_PORT = 5000
DEBUG_MODE = True           # 개발 중 True, 발표/시연 때는 False 권장
MONITOR_INTERVAL = 2.0      # 몇 초마다 좌석을 다시 판정할지
WEMOS_TIMEOUT = 2           # Wemos 가 꺼져 있으면 이 시간만 기다리고 포기

PHONE_REGEX = re.compile(r"^010-\d{4}-\d{4}$")


# #############################################################################
#
#   2. 하드웨어 제어  (Wemos LED / Tapo 콘센트)
#
# #############################################################################
#
#  ★ 왜 "명령 대기열 + 작업 스레드" 구조인가 ★
#
#  Wemos 가 꺼져 있으면 HTTP 응답을 2초 기다립니다. Tapo 는 첫 연결에 3~5초
#  걸립니다. 이 명령을 웹 요청 처리 중에 그냥 호출하면, 손님이 발권 버튼을
#  누른 순간 화면이 몇 초씩 멈춰버립니다. 좌석 6개가 동시에 바뀌면 30초까지
#  멈출 수도 있습니다.
#
#  그래서 명령을 "대기열에 넣기만" 하고 바로 돌아옵니다(0.0001초).
#  실제 전송은 뒤에서 도는 작업 스레드가 순서대로 처리합니다.
#
#     [Flask / 감시스레드] --넣기--> [대기열] --꺼내기--> [작업스레드] --> 하드웨어
#            (즉시 리턴)                                 (느려도 상관없음)
#
#  ★ Tapo 는 왜 이벤트 루프를 계속 붙잡고 있나 ★
#  python-kasa 는 async 라이브러리입니다. 명령마다 asyncio.run() 을 부르면
#  매번 새로 연결하느라 느리고, 멀티탭이 요청 폭주로 뻗습니다
#  (예전에 Input/output error 가 났던 것과 같은 계열의 문제).
#  작업 스레드가 루프 하나를 계속 붙잡고 있으면 연결을 재사용할 수 있어서
#  두 번째 명령부터는 0.3초 정도로 끝납니다.
# #############################################################################

#  Wemos 펌웨어가 알아듣는 LED 상태 문자
#     "occupied"              정상 점유        LED 꺼짐
#     "vacant"                비어있음         LED 꺼짐
#     "illegal"               미발권 무단사용   LED 항상 켜짐
#     "abandoned_warning"     사석화 경고      LED 점멸
#     "abandoned_terminated"  사석화 확정      LED 항상 켜짐
#  어느 상태에 어느 값을 보낼지는 아래 STATE_TABLE 에서 정합니다.

_cmd_queue = queue.Queue()
_worker_started = False
_tapo_devices = {}          # {멀티탭 IP: 연결 객체} — 연결 재사용용


def _send_led(seat_id, led_state):
    ip = SEAT_WEMOS_IPS.get(seat_id)
    if not ip:
        print(f"        [LED] 좌석{seat_id} IP 정보 없음 (SEAT_WEMOS_IPS 확인)")
        return
    try:
        res = requests.get(f"http://{ip}/set_state",
                           params={"state": led_state}, timeout=WEMOS_TIMEOUT)
        if res.status_code == 200:
            print(f"        [LED] 좌석{seat_id} -> {led_state}")
        else:
            print(f"        [LED] 좌석{seat_id} 응답 오류 {res.status_code}")
    except requests.exceptions.Timeout:
        print(f"        [LED] 좌석{seat_id} 응답 시간 초과 ({ip})")
    except requests.exceptions.ConnectionError:
        print(f"        [LED] 좌석{seat_id} 연결 실패 — Wemos 전원/IP 확인 ({ip})")
    except Exception as e:
        print(f"        [LED] 좌석{seat_id} 오류: {e}")


async def _get_tapo(host):
    """멀티탭 연결을 가져온다. 이미 연결돼 있으면 그대로 재사용."""
    if host in _tapo_devices:
        return _tapo_devices[host]

    from kasa import Credentials, Discover

    dev = await Discover.discover_single(
        host, credentials=Credentials(TAPO_USER, TAPO_PASS))
    await dev.update()
    _tapo_devices[host] = dev
    print(f"        [전원] 멀티탭 {host} 연결됨 (구멍 {len(dev.children)}개)")
    return dev


async def _send_power_async(seat_id, on):
    mapping = SEAT_POWER.get(seat_id)
    if not mapping:
        print(f"        [전원] 좌석{seat_id} 매핑 없음 (SEAT_POWER 확인)")
        return

    host, index = mapping

    # 연결이 끊겼을 수 있으므로 실패하면 캐시를 버리고 한 번 더 시도한다.
    for attempt in (1, 2):
        try:
            dev = await _get_tapo(host)
            await dev.update()

            if index >= len(dev.children):
                print(f"        [전원] 좌석{seat_id} 구멍 번호 {index} 없음 "
                      f"(이 멀티탭은 구멍이 {len(dev.children)}개)")
                return

            child = dev.children[index]
            await (child.turn_on() if on else child.turn_off())
            print(f"        [전원] 좌석{seat_id} ({host} 구멍{index}) -> "
                  f"{'ON' if on else 'OFF'}")
            return

        except Exception as e:
            _tapo_devices.pop(host, None)
            if attempt == 2:
                print(f"        [전원] 좌석{seat_id} 실패: {e}")
            else:
                time.sleep(0.5)


def _hardware_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        kind, seat_id, arg = _cmd_queue.get()
        try:
            if kind == "led":
                _send_led(seat_id, arg)
            elif kind == "power":
                loop.run_until_complete(_send_power_async(seat_id, arg))
        except Exception as e:
            print(f"        [하드웨어] 처리 중 오류: {e}")
        finally:
            _cmd_queue.task_done()


def start_hardware_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_hardware_worker, daemon=True).start()
    print("[하드웨어] 명령 처리 스레드 시작")


def set_led(seat_id, led_state):
    if not HARDWARE_ENABLED or not LED_ENABLED:
        print(f"        [모의] 좌석{seat_id} LED -> {led_state}")
        return
    start_hardware_worker()
    _cmd_queue.put(("led", seat_id, led_state))


def set_power(seat_id, on):
    if not HARDWARE_ENABLED or not POWER_ENABLED:
        print(f"        [모의] 좌석{seat_id} 전원 -> {'ON' if on else 'OFF'}")
        return
    start_hardware_worker()
    _cmd_queue.put(("power", seat_id, bool(on)))


def check_hardware():
    """
    하드웨어 연결 점검.
        python app.py check
    라고 실행하면 서버를 띄우지 않고 이것만 돌립니다.
    """
    print("=" * 58)
    print("  하드웨어 연결 점검")
    print("=" * 58)

    print("\n[1] Wemos D1 Mini")
    for sid in SEAT_IDS:
        ip = SEAT_WEMOS_IPS.get(sid)
        try:
            info = requests.get(f"http://{ip}/info", timeout=WEMOS_TIMEOUT).json()
        except Exception:
            info = None
        print(f"  {'OK  ' if info else '실패'} 좌석{sid}  {ip}  {info or ''}")

    print("\n[2] Tapo 멀티탭")
    for host in sorted({h for h, _ in SEAT_POWER.values()}):
        try:
            dev = asyncio.run(_get_tapo(host))
            print(f"  OK   {host}  ({dev.alias})")
            for i, child in enumerate(dev.children):
                print(f"         구멍{i}: {child.alias}  현재 "
                      f"{'ON' if child.is_on else 'OFF'}")
        except Exception as e:
            print(f"  실패 {host}  {e}")
        finally:
            _tapo_devices.pop(host, None)

    print("\n점검 끝. 구멍 번호가 실제 좌석과 다르면 위 SEAT_POWER 를 고치세요.")


# #############################################################################
#
#   3. 좌석 데이터
#
# #############################################################################

# 발권 정보
seats = [{"id": i, "status": "EMPTY", "phone": None} for i in SEAT_IDS]

# 카메라(YOLO 또는 fake.py)가 보내준 정보
vision = {
    sid: {
        "person": False,            # 마지막으로 받은 "사람 있음/없음"
        "last_seen": time.time(),   # 마지막으로 사람이 보인 시각 ← 타이머의 핵심
        "confidence": 0.0,
        "last_packet": None,        # 마지막으로 데이터를 받은 시각 (연결 확인용)
    }
    for sid in SEAT_IDS
}

current_state = {sid: None for sid in SEAT_IDS}   # 직전 상태 = 엣지 트리거의 전부
grace_until = {sid: 0.0 for sid in SEAT_IDS}      # 반납 직후 유예 종료 시각

event_log = []      # 상태 변화 이력
alert_log = []      # 관리자 조치 필요 목록
notice_log = []     # /vision 화면에 띄울 팝업
_notice_seq = 0     # 팝업 번호 (브라우저가 "이미 본 알림"을 구분하는 데 씀)

lock = threading.RLock()
#  ↑ RLock 인 이유: 이미 lock 을 잡은 상태에서 다시 lock 을 잡는 함수를
#    호출하는 경우가 있어서. 보통 Lock 이면 거기서 영원히 멈춥니다(데드락).


# #############################################################################
#
#   4. 상태 판정  A / B / C / D / E
#
# #############################################################################
#
#  판정 재료 3개
#     person          : 지금 사람이 앉아 있냐        <- 카메라가 알려준다
#     ticketed        : 발권(결제)했냐              <- 위 seats 리스트를 조회
#     vacant_seconds  : 몇 초 동안 비어 있었냐       <- 이 파일이 계산
#
#  ┌────┬─────────────┬──────────────────────────────┬──────┬──────┐
#  │상태│ 이름         │ 조건                          │ LED  │ 전원 │
#  ├────┼─────────────┼──────────────────────────────┼──────┼──────┤
#  │ A  │ 정상점유     │ 발권O + (사람O 또는 3분 미만) │ 꺼짐 │ ON   │
#  │ B  │ 사석화 경고  │ 발권O + 사람X + 3분 이상      │ 꺼짐 │ ON   │
#  │ C  │ 사석화 확정  │ 발권O + 사람X + 5분 이상      │ 켜짐 │ OFF  │
#  │ D  │ 무단사용     │ 발권X + 사람O                 │ 켜짐 │ OFF  │
#  │ E  │ 빈자리       │ 발권X + 사람X                 │ 꺼짐 │ OFF  │
#  └────┴─────────────┴──────────────────────────────┴──────┴──────┘
#
#  → 발권하면 A 가 되면서 콘센트 ON, 반납하면 E 가 되면서 OFF.
#    전원/LED 규칙은 아래 STATE_TABLE 한 군데에만 적혀 있습니다.
#
#  ★ B 의 LED 를 다시 점멸시키고 싶으면 ★
#    B 줄의 led 값을 "abandoned_warning" 으로 바꾸면 됩니다.
# #############################################################################

STATE_TABLE = {
    "A": {"name": "정상점유",   "led": "occupied",             "power": True,  "color": "#22c55e"},
    "B": {"name": "사석화경고", "led": "occupied",             "power": True,  "color": "#f59e0b"},
    "C": {"name": "사석화확정", "led": "abandoned_terminated", "power": False, "color": "#ef4444"},
    "D": {"name": "무단사용",   "led": "illegal",              "power": False, "color": "#dc2626"},
    "E": {"name": "빈자리",     "led": "vacant",               "power": False, "color": "#94a3b8"},
}


def judge_state(person, ticketed, vac_seconds, in_grace=False):
    """
    사실 3개를 받아 상태 문자 하나를 돌려준다. 아무것도 바꾸지 않는다(부작용 없음).

    ★ 검사 순서 주의 ★
    C(5분)를 B(3분)보다 먼저 검사해야 한다. 반대로 쓰면 5분이 지나도
    "3분 이상이네" 하고 B 에서 걸려서 절대 C 가 되지 않는다.
    """
    if ticketed:
        if person:
            return "A"
        if vac_seconds >= TERMINATE_SECONDS:
            return "C"                       # ← 5분을 먼저 검사!
        if vac_seconds >= WARN_SECONDS:
            return "B"
        return "A"
    else:
        if person and not in_grace:
            return "D"
        return "E"                           # 반납 직후 유예 중이면 사람이 보여도 E


def is_ticketed(seat_id):
    """★ 발권 대조 ★  별도 DB 없이 위 seats 리스트를 그대로 읽는다."""
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


def reset_timer(seat_id):
    """
    ★ 발권할 때 반드시 불러야 한다 ★
    좌석이 10분 동안 비어 있다가 손님이 발권하면 vacant_seconds 가 600초다.
    타이머를 리셋하지 않으면 발권하는 순간 곧바로 C(사석화확정)로 판정되어
    콘센트가 꺼진다.
    """
    vision[seat_id]["last_seen"] = time.time()


# ── 알림 ─────────────────────────────────────────────────────────────────

def notify_admin(seat_id, message):
    print(f"        [알림] {message}")
    alert_log.insert(0, {
        "seat_id": seat_id,
        "message": message,
        "at": datetime.now().strftime("%H:%M:%S"),
        "phone": get_phone(seat_id),
    })
    del alert_log[20:]

    # TODO: Solapi SMS 발송을 붙일 자리
    # phone = get_phone(seat_id)
    # if phone: requests.post("https://api.solapi.com/...", json={...})


def push_notice(seat_id, state):
    """/vision 화면 오른쪽 위에 뜰 팝업을 만든다."""
    global _notice_seq

    template = ALERT_MESSAGES.get(state)
    if not template:
        return

    _notice_seq += 1
    notice_log.insert(0, {
        "id": _notice_seq,
        "seat_id": seat_id,
        "state": state,
        "state_name": STATE_TABLE[state]["name"],
        "color": STATE_TABLE[state]["color"],
        "message": template.format(seat=seat_id),
        "at": datetime.now().strftime("%H:%M:%S"),
    })
    del notice_log[30:]


# ── 엣지 트리거 ───────────────────────────────────────────────────────────
#
#  감시 스레드는 2초마다 좌석을 판정한다. 좌석이 C 로 5분간 머물면
#  "전원 꺼" 명령이 150번 나간다. 그래서 직전 상태를 기억해두고
#  달라진 순간에만 명령을 보낸다.
#
#     판정 결과 :  A A A A B B B B B C C C C
#     명령 발사 :          ↑         ↑
#                       여기만     여기만

def on_state_change(seat_id, old, new):
    """★ 상태가 실제로 바뀐 순간에만 호출된다. 2초마다가 아니다. ★"""
    old_label = f"{old}({STATE_TABLE[old]['name']})" if old else "시작"
    print(f"  [변화] 좌석{seat_id}: {old_label} -> {new}({STATE_TABLE[new]['name']})")

    rule = STATE_TABLE[new]

    set_led(seat_id, rule["led"])        # 1) LED
    set_power(seat_id, rule["power"])    # 2) 콘센트

    # 3) 관리자 조치 목록 (C, D 만)
    if new == "C":
        notify_admin(seat_id, f"{seat_id}번 자리 조치 필요 — 사석화로 이용종료 처리됨")
    elif new == "D":
        notify_admin(seat_id, f"{seat_id}번 자리 조치 필요 — 미발권 무단사용 감지")

    # 4) 화면 팝업
    #    old 가 None = 서버를 막 켜서 처음 판정한 순간.
    #    이때는 좌석 6개가 한꺼번에 E 가 되므로 팝업을 띄우지 않는다.
    if old is not None and new in ALERT_STATES:
        push_notice(seat_id, new)

    # 5) 이력
    event_log.insert(0, {
        "seat_id": seat_id,
        "from": old or "-",
        "to": new,
        "at": datetime.now().strftime("%H:%M:%S"),
    })
    del event_log[30:]


def evaluate_seat(seat_id):
    person = vision[seat_id]["person"]
    ticketed = is_ticketed(seat_id)                  # ← 발권 대조
    vac = vacant_seconds(seat_id)
    in_grace = time.time() < grace_until[seat_id]

    new = judge_state(person, ticketed, vac, in_grace)

    old = current_state[seat_id]
    if new != old:                                   # ★ 엣지 트리거 ★
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


# ── 발권 / 반납 ───────────────────────────────────────────────────────────
#  콘센트를 여기서 직접 켜고 끄지 않고 evaluate_all() 을 부르는 이유:
#  전원 규칙이 STATE_TABLE 한 군데에만 적혀 있어야 나중에 헷갈리지 않기 때문.

def issue(seat_id, phone):
    with lock:
        for seat in seats:
            if seat["id"] != seat_id:
                continue
            if seat["status"] != "EMPTY":
                return False, "이미 사용중인 좌석입니다."

            seat["status"] = "OCCUPIED"
            seat["phone"] = phone
            reset_timer(seat_id)          # ★ 안 하면 발권 즉시 C 로 감
            grace_until[seat_id] = 0.0
            evaluate_all()                # -> A 로 바뀌면서 콘센트 ON
            return True, f"{seat_id}번 좌석 발권이 완료되었습니다."
    return False, "존재하지 않는 좌석입니다."


def do_return(seat_id, phone=None, force=False):
    """force=True 면 전화번호 확인 없이 강제 반납(관리자용)."""
    with lock:
        for seat in seats:
            if seat["id"] != seat_id:
                continue

            if not force:
                if seat["status"] != "OCCUPIED":
                    return False, "빈 좌석입니다."
                if seat["phone"] != phone:
                    return False, "번호를 확인해주세요."

            seat["status"] = "EMPTY"
            seat["phone"] = None
            grace_until[seat_id] = time.time() + RETURN_GRACE_SECONDS
            evaluate_all()                # -> E 로 바뀌면서 콘센트 OFF, LED OFF

            if force:
                return True, f"{seat_id}번 좌석이 강제 반납 되었습니다."
            return True, f"{seat_id}번 좌석 반납이 완료되었습니다."
    return False, "존재하지 않는 좌석입니다."


# ── 감시 스레드 ───────────────────────────────────────────────────────────
#  이게 있어야 "데이터를 안 보내도 3분 지나면 B 로 넘어가는" 동작이 가능하다.
#  fake.py 는 내가 명령을 칠 때만 1회 발신하므로, 발신 쪽에 타이머를 두면
#  아무 일도 일어나지 않는다.

def monitor_loop():
    print(f"[감시] {MONITOR_INTERVAL}초 주기 감시 시작 "
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


def snapshot():
    """화면과 응답에 쓰는 현재 상태 요약"""
    with lock:
        return [
            {
                "seat_id": sid,
                "state": current_state[sid],
                "state_name": STATE_TABLE[current_state[sid]]["name"] if current_state[sid] else None,
                "color": STATE_TABLE[current_state[sid]]["color"] if current_state[sid] else "#94a3b8",
                "person": vision[sid]["person"],
                "ticketed": is_ticketed(sid),
                "phone": get_phone(sid),
                "vacant_seconds": round(vacant_seconds(sid), 1),
                "led": STATE_TABLE[current_state[sid]]["led"] if current_state[sid] else None,
                "power": STATE_TABLE[current_state[sid]]["power"] if current_state[sid] else None,
            }
            for sid in SEAT_IDS
        ]


# #############################################################################
#
#   5. 손님용 발권 키오스크  ( / )
#
# #############################################################################

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/seats", methods=["GET"])
def get_seats():
    return jsonify({"seats": seats})


@app.route("/api/issue/<int:seat_id>", methods=["POST"])
def issue_seat(seat_id):
    data = request.get_json(silent=True) or {}
    phone = data.get("phone")

    if not phone or not PHONE_REGEX.match(phone):
        return jsonify({
            "success": False,
            "message": "전화번호는 반드시 010-1234-5678 형식(하이픈 포함)으로 입력해야 합니다."
        }), 400

    ok, message = issue(seat_id, phone)
    return jsonify({"success": ok, "message": message}), (200 if ok else 400)


@app.route("/api/return/<int:seat_id>", methods=["POST"])
def return_seat(seat_id):
    data = request.get_json(silent=True) or {}
    ok, message = do_return(seat_id, phone=data.get("phone"), force=False)
    return jsonify({"success": ok, "message": message}), (200 if ok else 400)


# #############################################################################
#
#   6. 감지 화면 + 관리자  ( /vision )
#
# #############################################################################
#
#  ★ 예전 /admin 페이지는 없어졌습니다 ★
#  /vision 한 페이지에서 비밀번호를 입력하면 관리자 모드가 켜지고,
#  그 상태로 좌석 카드를 누르면 강제 반납됩니다.
# #############################################################################

@app.route("/vision")
def vision_page():
    return render_template("vision.html")


# 예전에 /admin 을 즐겨찾기 해둔 경우를 위해 같은 화면을 보여준다.
@app.route("/admin")
def admin_page():
    return render_template("vision.html")


@app.route("/api/vision", methods=["GET"])
def api_vision():
    with lock:
        return jsonify({
            "seats": snapshot(),
            "thresholds": {"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS},
            "hardware_enabled": HARDWARE_ENABLED,
            "events": event_log[:15],
            "alerts": alert_log[:10],
            "notices": notice_log[:10],
            "now": datetime.now().strftime("%H:%M:%S"),
        })


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    if data.get("pin") != ADMIN_PIN:
        return jsonify({"success": False, "message": "관리자 비밀번호가 틀렸습니다."}), 403
    return jsonify({"success": True, "message": "관리자 모드로 전환했습니다."})


@app.route("/api/admin/force_return/<int:seat_id>", methods=["POST"])
def force_return(seat_id):
    data = request.get_json(silent=True) or {}
    if data.get("pin") != ADMIN_PIN:
        return jsonify({"success": False, "message": "관리자 비밀번호가 틀렸습니다."}), 403

    ok, message = do_return(seat_id, force=True)
    return jsonify({"success": ok, "message": message}), (200 if ok else 404)


# #############################################################################
#
#   7. 데이터 수신  ( POST /detect  또는  /control )
#
# #############################################################################
#
#  ★ /detect 와 /control 둘 다 받습니다 ★
#  fake.py 는 /detect 로, ssh2_sender.py 는 /control 로 보내기 때문에
#  둘 다 등록해두면 발신 파일을 고칠 필요가 없습니다.
# #############################################################################

def normalize_payload(data):
    """
    발신 파일마다 JSON 모양이 조금씩 달라서 여기서 하나로 통일한다.

    받아들이는 형식 3가지
      (1) {"detections": [{"seat_id":1, "status":"vacant", "confidence":0.95}]}
      (2) {"seat_id":1, "status":"vacant", "confidence":0.95}
      (3) {"seats": [{"seat_id":1, "person":true}]}

    돌려주는 모양
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
        items = [data]
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


@app.route("/detect", methods=["POST"])
@app.route("/control", methods=["POST"])
def receive_detection():
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
        snap = snapshot()

    print(f"[수신] {len(parsed)}개 좌석 "
          + (f"| 변화 {len(changes)}건" if changes else "| 변화 없음"))

    return jsonify({
        "success": True,
        "received": len(parsed),
        "changes": changes,
        "seats": snap,
        "timestamp": datetime.now().isoformat(),
    }), 200


# ── 시연용 임계값 변경 ─────────────────────────────────────────────────────
#      /api/vision/config?warn=10&term=20    ← 빠른 테스트
#      /api/vision/config?warn=180&term=300  ← 원래대로

@app.route("/api/vision/config", methods=["GET", "POST"])
def api_vision_config():
    global WARN_SECONDS, TERMINATE_SECONDS

    warn = request.args.get("warn", type=int)
    term = request.args.get("term", type=int)

    if warn:
        WARN_SECONDS = warn
    if term:
        TERMINATE_SECONDS = term

    if warn or term:
        print(f"[설정] 임계값 변경 -> 경고 {WARN_SECONDS}초 / 종료 {TERMINATE_SECONDS}초")

    return jsonify({"warn": WARN_SECONDS, "terminate": TERMINATE_SECONDS})


# #############################################################################
#
#   8. 실행
#
# #############################################################################

if __name__ == "__main__":
    import sys

    # python app.py check  -> 서버를 띄우지 않고 하드웨어 점검만
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check_hardware()
        sys.exit(0)

    # ★ WERKZEUG_RUN_MAIN 검사가 왜 필요한가 ★
    # debug=True 면 Flask 가 파일 자동 재시작을 위해 프로세스를 두 개 띄운다.
    # 그냥 스레드를 시작하면 감시 스레드가 두 개 돌면서 current_state 가
    # 두 벌 생기고 엣지 트리거가 엉뚱하게 동작한다.
    if (not DEBUG_MODE) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_monitor()

    base = f"http://{PI_IP}:{SERVER_PORT}"

    print("=" * 64)
    print("  스터디카페 좌석 관리 서버")
    print("=" * 64)
    print(f"  발권 키오스크      {base}/")
    print(f"  감지 + 관리자      {base}/vision")
    print(f"  데이터 수신        POST {base}/detect  (또는 /control)")
    print("-" * 64)
    print(f"  하드웨어 제어      {'ON' if HARDWARE_ENABLED else 'OFF (모의 로그만 출력)'}")
    print(f"  임계값             경고 {WARN_SECONDS}초 / 종료 {TERMINATE_SECONDS}초")
    print("  하드웨어 점검      python app.py check")
    print("=" * 64)

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE)
