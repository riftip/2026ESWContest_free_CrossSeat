from flask import Flask, render_template, jsonify, request
import re  # 정규표현식 라이브러리 추가

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)