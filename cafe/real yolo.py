#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[AI 실시간 웹캠 좌석 감지 스크립트]
- 2행 3열 (총 6좌석) 상단 뷰 격자 분할
- YOLOv8 초경량 사람(Class 0) 감지
- Flask 메인 서버(/detect)로 5초 주기 자동 POST 전송
"""

import cv2
import time
import sys
import requests
from ultralytics import YOLO

# 1. 모델 및 서버 주소 설정
SERVER_URL = "http://127.0.0.1:5000/detect"
CHECK_INTERVAL = 5  # 5초 주기 전송

print("⏳ [AI] YOLOv8n 모델 로드 중...", flush=True)
model = YOLO('yolov8n.pt')

# 2. USB 웹캠 연결 (/dev/video0)
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("❌ [오류] USB 웹캠을 열 수 없습니다. 포트 연결을 확인하세요.", flush=True)
    sys.exit(1)

# 해상도 및 버퍼 최적화
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# 3. 2행 3열 (총 6좌석) 레이아웃 설정
ROWS = 2  # 세로 2분할
COLS = 3  # 가로 3분할

def get_grid_rois(frame_w, frame_h, rows, cols):
    """카메라 해상도 기준 2x3 격자(ROI) 분할"""
    rois = []
    cell_w = frame_w // cols
    cell_h = frame_h // rows
    seat_id = 1

    for r in range(rows):
        for c in range(cols):
            x1 = c * cell_w
            y1 = r * cell_h
            x2 = (c + 1) * cell_w
            y2 = (r + 1) * cell_h
            rois.append({
                "seat_id": seat_id,
                "box": (x1, y1, x2, y2)
            })
            seat_id += 1
    return rois

print("=" * 60, flush=True)
print("🚀 [YOLO AI] 2x3 좌석 감지 및 실시간 전송 모듈 구동 완료", flush=True)
print("=" * 60, flush=True)

while True:
    # 5초 대기
    time.sleep(CHECK_INTERVAL)

    # 웹캠 하드웨어 버퍼 최신화
    for _ in range(3):
        cap.grab()
    ret, frame = cap.read()

    if not ret or frame is None:
        print("⚠️ [경고] 웹캠 프레임을 읽어오지 못했습니다. 재시도 중...", flush=True)
        continue

    h, w = frame.shape[:2]
    seat_rois = get_grid_rois(w, h, ROWS, COLS)

    # YOLO 사람(Class 0) 경량 추론 (imgsz=192)
    results = model.predict(source=frame, classes=[0], conf=0.45, imgsz=192, verbose=False)

    # 감지된 사람들의 정중앙 좌표 추출
    person_centers = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cx = int((x1 + x2) / 2)
        cy = int((y1 + y2) / 2)
        person_centers.append((cx, cy))

    # 각 좌석 구역별 착석 여부 판별 (1: 사람 감지, 0: 빈자리)
    detections = []
    presence_list = []
    for seat in seat_rois:
        s_id = seat["seat_id"]
        sx1, sy1, sx2, sy2 = seat["box"]
        is_occupied = 1 if any(sx1 <= cx <= sx2 and sy1 <= cy <= sy2 for cx, cy in person_centers) else 0
        
        detections.append({"seat_id": s_id, "presence": is_occupied})
        presence_list.append(is_occupied)

    # 백엔드 서버(/detect)로 데이터 전송
    try:
        res = requests.post(SERVER_URL, json={"detections": detections}, timeout=2.5)
        if res.status_code == 200:
            print(f"[{time.strftime('%H:%M:%S')}] 📡 좌석 감지 전송 완료: {presence_list}", flush=True)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️ 서버 응답 에러 (코드: {res.status_code})", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] ❌ 서버 전송 실패: {e}", flush=True)

cap.release()