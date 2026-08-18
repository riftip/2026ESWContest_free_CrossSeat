import cv2
import time
import sys
from ultralytics import YOLO

# 1. 초경량 YOLO 모델 로드
print("⏳ YOLO 모델 로드 중...")
model = YOLO('yolov8n.pt')

# 2. 웹캠 연결 및 코덱/해상도 설정
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

# 3x2 좌석 설정
ROWS = 3
COLS = 2
CHECK_INTERVAL = 10.0  # 💡 사람 감지 주기 (10초)

def get_grid_rois(frame_w, frame_h, rows, cols):
    rois = []
    cell_w = frame_w // cols
    cell_h = frame_h // rows
    seat_id = 1
    for r in range(rows):
        for c in range(cols):
            rois.append({
                "seat_id": seat_id,
                "box": (c * cell_w, r * cell_h, (c + 1) * cell_w, (r + 1) * cell_h)
            })
            seat_id += 1
    return rois

print("\n==================================================")
print(f"🎥 3x2 좌석 감지 시작 (검사 주기: {CHECK_INTERVAL}초)")
print("👉 종료하려면 화면 창에서 'q' 키를 누르세요.")
print("==================================================\n")

last_check_time = 0
person_centers = []
seat_status = {i: 0 for i in range(1, 7)}  # 초기 좌석 상태 (모두 빈자리)

while True:
    ret, frame = cap.read()
    if not ret or frame is None:
        continue

    frame = cv2.flip(frame, 1)
    h, w, _ = frame.shape
    seat_rois = get_grid_rois(w, h, ROWS, COLS)

    current_time = time.time()

    # 🚀 10초가 지났을 때만 YOLO 사람 인식 실행
    if current_time - last_check_time >= CHECK_INTERVAL:
        last_check_time = current_time

        # YOLO 추론 실행
        results = model.predict(source=frame, classes=[0], conf=0.45, imgsz=320, verbose=False)

        person_centers = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            person_centers.append((cx, cy))

        # 좌석 점유 여부 갱신
        for seat in seat_rois:
            s_id = seat["seat_id"]
            sx1, sy1, sx2, sy2 = seat["box"]
            seat_status[s_id] = 1 if any(sx1 <= cx <= sx2 and sy1 <= cy <= sy2 for cx, cy in person_centers) else 0

        # 콘솔에 10초마다 갱신된 좌석 현황 출력
        print(f"\n[{time.strftime('%H:%M:%S')}] 좌석 점유 현황 ➜ {seat_status}")

    # 다음 검사까지 남은 시간 계산 (UI 표시용)
    remaining_time = max(0.0, CHECK_INTERVAL - (current_time - last_check_time))

    # 감지된 사람 위치 표시
    for (cx, cy) in person_centers:
        cv2.circle(frame, (cx, cy), 5, (0, 255, 255), -1)

    # 3. 화면 UI 표시 (초록: 빈자리 / 빨강: 사람 있음)
    for seat in seat_rois:
        s_id = seat["seat_id"]
        sx1, sy1, sx2, sy2 = seat["box"]
        is_occupied = seat_status[s_id]

        color = (0, 0, 255) if is_occupied else (0, 255, 0)
        status_text = f"Seat {s_id}: {'OCCUPIED' if is_occupied else 'EMPTY'}"

        cv2.rectangle(frame, (sx1, sy1), (sx2, sy2), color, 2)
        cv2.putText(frame, status_text, (sx1 + 10, sy1 + 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

    # 상단에 다음 검사 카운트다운 표시
    cv2.putText(frame, f"Next Check: {remaining_time:.1f}s", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

    cv2.imshow("Laptop YOLO 3x2 Seat Monitor", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()