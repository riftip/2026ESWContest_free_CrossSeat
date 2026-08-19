#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[AI 실시간 웹캠 좌석 감지 스크립트 - 진단 강화판]

원본과 달라진 점
  1. 시작하자마자 모델/카메라를 한 번 시험해 보고 결과를 출력합니다.
     (5초 기다렸다가 조용히 죽는 일이 없도록)
  2. 매 주기마다 감지 결과를 그림으로 저장합니다. -> /tmp/detect_view.jpg
     격자선, 사람 박스, 좌석 번호가 다 그려집니다. 이걸 맥으로 받아서 보면
     "감지가 안 되는 건지" "감지는 되는데 칸이 어긋난 건지" 바로 갈립니다.
  3. 사람 배정 방식을 중심점 -> 겹친 넓이 최대 칸으로 바꿨습니다.
     한 사람이 두 칸에 걸쳐도 한 좌석에만 배정됩니다.
  4. 오래된 프레임을 확실히 버립니다. (5초 자고 일어나면 버퍼가 낡아 있음)
  5. imgsz 320 / conf 0.35 로 완화. 상단 뷰는 신뢰도가 낮게 나옵니다.
"""
import os
import sys
import time

import cv2
import requests
from ultralytics import YOLO

# =============================================================================
# 1. 설정   ★★★ 고칠 일이 있으면 여기만 보면 됩니다 ★★★
# =============================================================================
SERVER_URL = "http://127.0.0.1:5000/detect"
CHECK_INTERVAL = 5          # 전송 주기(초)

CAM_INDEX = 0               # /dev/video0. 안 되면 1, 2 로 바꿔보세요
FRAME_W, FRAME_H = 640, 480

IMG_SIZE = 320              # 192는 너무 작아 사람이 안 잡힙니다. 320 또는 416 권장
CONF_THRESHOLD = 0.35       # 상단 뷰는 낮춰야 잡힙니다. 오탐 많으면 0.45로

ROWS, COLS = 2, 3           # 2행 3열 = 6좌석

# 격자 칸 순서(왼쪽 위 -> 오른쪽 아래)에 좌석 번호를 붙입니다.
# 카메라가 뒤집혀 달려서 번호가 반대라면 이 목록 순서만 바꾸면 됩니다.
# 예) 180도 뒤집힘: [6, 5, 4, 3, 2, 1]
SEAT_ID_MAP = [1, 2, 3, 4, 5, 6]

DEBUG_SAVE = True           # 감지 결과 그림 저장 (시연 때는 False로 꺼도 됨)
DEBUG_PATH = "/tmp/detect_view.jpg"

# =============================================================================
# 2. 준비 단계 - 여기서 막히면 원인이 바로 보이도록 하나씩 확인
# =============================================================================
print("=" * 60, flush=True)
print("🚀 좌석 감지 모듈 시작", flush=True)
print("=" * 60, flush=True)

# ── 모델 로드 ────────────────────────────────────────────────
print("⏳ [1/3] YOLOv8n 모델 로드 중... (2GB 램이면 여기서 죽을 수 있음)", flush=True)
t0 = time.time()
try:
    model = YOLO("yolov8n.pt")
except Exception as e:
    print(f"❌ 모델 로드 실패: {e}", flush=True)
    print("💡 yolov8n.pt 파일이 있는지, 인터넷이 되는지 확인하세요.", flush=True)
    sys.exit(1)
print(f"✅ 모델 로드 완료 ({time.time() - t0:.1f}초)", flush=True)

# ── 카메라 열기 ──────────────────────────────────────────────
print(f"⏳ [2/3] USB 웹캠 열기 (/dev/video{CAM_INDEX})...", flush=True)
cap = cv2.VideoCapture(CAM_INDEX)
if not cap.isOpened():
    print("❌ 웹캠을 열 수 없습니다.", flush=True)
    print("💡 ls /dev/video* 로 장치 번호를 확인하고 CAM_INDEX 를 바꿔보세요.", flush=True)
    sys.exit(1)

# MJPG로 받으면 USB 대역폭을 훨씬 적게 씁니다 (YUYV는 640x480에서도 버벅임)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

ret, frame = cap.read()
if not ret or frame is None:
    print("❌ 카메라는 열렸는데 프레임을 못 읽습니다.", flush=True)
    print("💡 장치가 두 개(video0/video1) 잡히는 경우입니다. CAM_INDEX 를 1로 바꿔보세요.", flush=True)
    cap.release()
    sys.exit(1)
print(f"✅ 카메라 정상 ({frame.shape[1]}x{frame.shape[0]})", flush=True)

# ── 추론 한 번 시험 ──────────────────────────────────────────
print("⏳ [3/3] 첫 추론 시험 중...", flush=True)
t0 = time.time()
_ = model.predict(source=frame, classes=[0], conf=CONF_THRESHOLD,
                  imgsz=IMG_SIZE, verbose=False)
print(f"✅ 추론 정상 (1회 {time.time() - t0:.2f}초)", flush=True)
print("-" * 60, flush=True)
if DEBUG_SAVE:
    print(f"🖼  감지 그림 저장 위치: {DEBUG_PATH}", flush=True)
    print("    맥에서 확인:  scp choi@192.168.0.113:/tmp/detect_view.jpg ~/Desktop/", flush=True)
print("-" * 60, flush=True)


# =============================================================================
# 3. 격자 계산
# =============================================================================
def get_grid_rois(frame_w, frame_h):
    """화면을 ROWS x COLS 격자로 나누고 좌석 번호를 붙입니다."""
    rois = []
    cell_w = frame_w // COLS
    cell_h = frame_h // ROWS
    idx = 0
    for r in range(ROWS):
        for c in range(COLS):
            rois.append({
                "seat_id": SEAT_ID_MAP[idx],
                "box": (c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h),
            })
            idx += 1
    return rois


def overlap_area(a, b):
    """두 사각형이 겹치는 넓이"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    return w * h if w > 0 and h > 0 else 0


def flush_camera(duration=0.4):
    """오래된 프레임 버리기.
    5초 자고 일어나면 카메라 버퍼에는 5초 전 장면이 들어 있습니다.
    그냥 read() 하면 과거를 보고 판단하게 됩니다."""
    end = time.time() + duration
    while time.time() < end:
        cap.grab()


# =============================================================================
# 4. 본 루프
# =============================================================================
cycle = 0
try:
    while True:
        cycle += 1

        flush_camera()
        ret, frame = cap.read()
        if not ret or frame is None:
            print("⚠️ 프레임 읽기 실패. 재시도합니다.", flush=True)
            time.sleep(1)
            continue

        h, w = frame.shape[:2]
        seat_rois = get_grid_rois(w, h)

        t0 = time.time()
        results = model.predict(source=frame, classes=[0], conf=CONF_THRESHOLD,
                                imgsz=IMG_SIZE, verbose=False)
        infer_ms = (time.time() - t0) * 1000

        # 사람 박스 목록
        persons = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
            persons.append({"box": (x1, y1, x2, y2), "conf": float(box.conf[0])})

        # 사람마다 "가장 많이 겹친 칸" 하나에만 배정
        occupied = {s["seat_id"]: 0 for s in seat_rois}
        for p in persons:
            best_seat, best_area = None, 0
            for s in seat_rois:
                area = overlap_area(p["box"], s["box"])
                if area > best_area:
                    best_area, best_seat = area, s["seat_id"]
            if best_seat is not None:
                occupied[best_seat] = 1

        detections = [{"seat_id": s["seat_id"], "presence": occupied[s["seat_id"]]}
                      for s in seat_rois]
        presence_list = [d["presence"] for d in detections]

        # ── 진단용 그림 저장 ──────────────────────────────────
        if DEBUG_SAVE:
            vis = frame.copy()
            for s in seat_rois:
                x1, y1, x2, y2 = s["box"]
                on = occupied[s["seat_id"]]
                color = (0, 200, 0) if on else (120, 120, 120)
                cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
                cv2.putText(vis, f"SEAT {s['seat_id']}", (x1 + 6, y1 + 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            for p in persons:
                x1, y1, x2, y2 = p["box"]
                cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(vis, f"{p['conf']:.2f}", (x1, max(y1 - 6, 12)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            cv2.putText(vis, time.strftime("%H:%M:%S"), (8, h - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            try:
                cv2.imwrite(DEBUG_PATH, vis)
            except Exception:
                pass

        # ── 서버 전송 ────────────────────────────────────────
        stamp = time.strftime("%H:%M:%S")
        try:
            res = requests.post(SERVER_URL, json={"detections": detections}, timeout=2.5)
            if res.status_code == 200:
                print(f"[{stamp}] 📡 사람 {len(persons)}명 / {infer_ms:.0f}ms / 좌석 {presence_list}",
                      flush=True)
            else:
                print(f"[{stamp}] ⚠️ 서버 응답 코드 {res.status_code}", flush=True)
        except Exception as e:
            print(f"[{stamp}] ❌ 서버 전송 실패: {e}", flush=True)

        time.sleep(CHECK_INTERVAL)

except KeyboardInterrupt:
    print("\n👋 종료합니다.", flush=True)
finally:
    cap.release()
