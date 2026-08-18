import cv2
import time
import json
import os
import sys
import threading
import torch
from ultralytics import YOLO

# 🚀 CPU 점유율 폭주 방지 (YOLO가 CPU 코어를 독점해 화면이 멈추는 현상 해결)
torch.set_num_threads(1)

CONFIG_FILE = "seats_config.json"
CHECK_INTERVAL = 10.0  # 💡 10초 주기 검사

# 1. 모델 로드
print("⏳ 초경량 YOLO 모델 로드 중...")
model = YOLO('yolov8n.pt')

# 2. 웹캠 연결 및 안정화
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
if not cap.isOpened():
    cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("❌ 웹캠을 열 수 없습니다.")
    sys.exit()

cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# 설정 파일 로드 및 저장 함수
def save_seats(seat_list):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(seat_list, f, indent=4)
    print(f"\n💾 좌석 설정이 '{CONFIG_FILE}'에 저장되었습니다!")

def load_seats():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # 기본 4인 좌석 좌표
    return [
        {"id": 1, "box": [50, 50, 280, 230]},
        {"id": 2, "box": [360, 50, 590, 230]},
        {"id": 3, "box": [50, 250, 280, 430]},
        {"id": 4, "box": [360, 250, 590, 430]}
    ]

seats = load_seats()

# 스레드 공유 변수
lock = threading.Lock()
person_centers = []
seat_status = {1: 0, 2: 0, 3: 0, 4: 0}
latest_frame = None
is_running = True
is_setting_mode = False
temp_new_seats = []
last_check_timestamp = time.time()

# 🚀 10초마다 동작하는 백그라운드 AI 작업자
def ai_detection_worker():
    global person_centers, seat_status, last_check_timestamp, is_running
    
    while is_running:
        # 10초 대기 (0.1초 단위로 쪼개어 종료 신호 즉각 반응)
        for _ in range(int(CHECK_INTERVAL * 10)):
            if not is_running:
                return
            time.sleep(0.1)

        if is_setting_mode or latest_frame is None:
            continue

        # 프레임 복사
        with lock:
            img_to_process = latest_frame.copy()
            current_seats = list(seats)

        # 연산 크기 192x192로 극소화하여 초고속 추론
        results = model.predict(source=img_to_process, classes=[0], conf=0.45, imgsz=192, verbose=False)

        new_centers = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            new_centers.append((int((x1 + x2) / 2), int((y1 + y2) / 2)))

        new_status = {}
        for seat in current_seats:
            s_id = seat["id"]
            sx1, sy1, sx2, sy2 = seat["box"]
            new_status[s_id] = 1 if any(sx1 <= cx <= sx2 and sy1 <= cy <= sy2 for cx, cy in new_centers) else 0

        with lock:
            person_centers = new_centers
            seat_status = new_status
            last_check_timestamp = time.time()

        print(f"\r[{time.strftime('%H:%M:%S')}] 10초 주기 좌석 현황 ➜ {seat_status}", end="")

# 백그라운드 AI 스레드 시작
thread = threading.Thread(target=ai_detection_worker, daemon=True)
thread.start()

# 원클릭 좌석 설정 마우스 콜백
def mouse_handler(event, x, y, flags, param):
    global is_setting_mode, temp_new_seats, seats
    if not is_setting_mode:
        return

    if event == cv2.EVENT_LBUTTONDOWN:
        seat_num = len(temp_new_seats) + 1
        box_w, box_h = 200, 160
        x1 = max(0, int(x - box_w / 2))
        y1 = max(0, int(y - box_h / 2))
        x2 = min(640, int(x + box_w / 2))
        y2 = min(480, int(y + box_h / 2))

        temp_new_seats.append({"id": seat_num, "box": [x1, y1, x2, y2]})
        print(f"👉 [Seat {seat_num}] 클릭 위치({x}, {y}) 설정 완료")

        if len(temp_new_seats) == 4:
            with lock:
                seats = list(temp_new_seats)
            save_seats(seats)
            is_setting_mode = False
            print("🎉 4개 좌석 설정 완료! 10초 주기 모니터링을 시작합니다.")

cv2.namedWindow("10s Cycle UltraLight Seat Monitor")
cv2.setMouseCallback("10s Cycle UltraLight Seat Monitor", mouse_handler)

print("\n==================================================")
print("🚀 [10초 주기 초경량 모드] 좌석 모니터링 시작")
print("👉 's' 키: 좌석 위치 재설정 (의자 4곳 클릭)")
print("👉 'q' 키: 프로그램 종료")
print("==================================================\n")

while True:
    ret, frame = cap.read()
    if not ret or frame is None:
        continue

    # 백그라운드 스레드용 프레임 전달
    with lock:
        latest_frame = frame

    display_frame = frame.copy()

    if is_setting_mode:
        cv2.rectangle(display_frame, (0, 0), (640, 35), (0, 0, 0), -1)
        step = len(temp_new_seats) + 1
        cv2.putText(display_frame, f"[Click Mode] Click Seat {step}/4 Center", (15, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)

        for seat in temp_new_seats:
            x1, y1, x2, y2 = seat["box"]
            cv2.rectangle(display_frame, (x1, y1), (x2, y2), (255, 200, 0), 2)
            cv2.putText(display_frame, f"Seat {seat['id']}", (x1 + 10, y1 + 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 0), 2)
    else:
        with lock:
            cur_centers = list(person_centers)
            cur_seats = list(seats)
            cur_status = dict(seat_status)
            last_ts = last_check_timestamp

        # 감지된 사람 위치 점 표시
        for (cx, cy) in cur_centers:
            cv2.circle(display_frame, (cx, cy), 5, (0, 255, 255), -1)

        # 좌석 박스 및 상태 렌더링
        for seat in cur_seats:
            s_id = seat["id"]
            x1, y1, x2, y2 = seat["box"]
            is_occ = cur_status.get(s_id, 0)
            color = (0, 0, 255) if is_occ else (0, 255, 0)

            cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(display_frame, f"Seat {s_id}: {'OCCUPIED' if is_occ else 'EMPTY'}", 
                        (x1 + 8, y1 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

        # 10초 카운트다운 표시
        time_elapsed = time.time() - last_ts
        remaining = max(0.0, CHECK_INTERVAL - time_elapsed)
        cv2.putText(display_frame, f"Next Check: {remaining:.1f}s | Cycle: 10s | 's': Set", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)

    cv2.imshow("10s Cycle UltraLight Seat Monitor", display_frame)

    key = cv2.waitKey(1) & 0xFF
    if key == ord('q'):
        is_running = False
        break
    elif key == ord('s'):
        is_setting_mode = True
        temp_new_seats = []
        print("\n🛠️ [설정 모드] 화면에서 1번~4번 좌석 위치를 차례대로 클릭하세요.")

cap.release()
cv2.destroyAllWindows()