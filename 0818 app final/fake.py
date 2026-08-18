# -*- coding: utf-8 -*-
"""
가짜 감지 데이터 발신기 (YOLO 대신 손으로 상태를 만들어보는 도구)

    python fake.py

  >> 2 occupied    2번 좌석에 사람이 앉았다고 알림
  >> 2 vacant      2번 좌석이 비었다고 알림
  >> quit          종료
"""

import json
import urllib.request

# ★★★ 수정 ─────────────────────────────────────────────────────────────────
#  서버(라즈베리파이) 주소.
#  라즈베리파이에서 직접 실행하면 127.0.0.1 로 두는 편이 안전합니다.
#  (라즈베리파이 IP 가 바뀌어도 고칠 필요가 없습니다)
#
#     라즈베리파이에서 실행할 때 :  http://127.0.0.1:5000/detect
#     노트북에서 실행할 때       :  http://192.168.0.102:5000/detect
URL = "http://127.0.0.1:5000/detect"
# ──────────────────────────────────────────────────────────────────────────


def send(seat, status):
    body = json.dumps({"detections": [
        {"seat_id": seat, "status": status, "confidence": 0.95}]}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read())
        print(f"\n[발신] 좌석{seat} -> {status}")
        for c in d.get("changes", []):
            print(f"   * 좌석{c['seat_id']}: {c['from']} -> {c['to']}")
        for s in d.get("seats", []):
            if s["seat_id"] == seat:
                print(f"   현재 {s['state']}({s['state_name']}) "
                      f"발권={'O' if s['ticketed'] else 'X'} "
                      f"공석={s['vacant_seconds']}초")
    except Exception as e:
        print(f"\n[실패] {e}")


print("=" * 46)
print("  가짜 데이터 발신  ->", URL)
print("=" * 46)
print("  2 occupied / 2 vacant / 3 occupied / quit")
print("=" * 46)

while True:
    try:
        c = input("\n>> ").strip().split()
    except (EOFError, KeyboardInterrupt):
        break
    if not c:
        continue
    if c[0].lower() == "quit":
        break
    if len(c) != 2:
        print("형식: 2 vacant"); continue
    if not c[0].isdigit() or not 1 <= int(c[0]) <= 6:
        print("좌석은 1~6"); continue
    if c[1].lower() not in ("occupied", "vacant", "illegal"):
        print("상태는 occupied / vacant / illegal"); continue
    send(int(c[0]), c[1].lower())
