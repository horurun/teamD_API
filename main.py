from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
from datetime import datetime
from typing import Optional
import requests  # HTTP通信用に追加
from fastapi.middleware.cors import CORSMiddleware

# config.py からゲートサーバーのURLを読み込む
try:
    from config import GATE_SERVER_URL
except ImportError:
    # ファイルがない場合のフォールバック（エラー防止）
    GATE_SERVER_URL = "http://192.168.4.2:8000"

app = FastAPI(title="駐車場管理システム API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 設定値 ---
FREE_TIME_LIMIT_MINUTES = 3
DB_FILE = "parking_system.db"

# ==========================================
# ゲート開放APIを叩く共通関数
# ==========================================
def open_gate():
    try:
        url = f"{GATE_SERVER_URL}/updatePermit"
        payload = {"status": "OPEN"}
        # タイムアウトを短め（3秒）に設定し、ゲートサーバーが落ちていてもこちらのシステムが止まらないようにする
        response = requests.post(url, json=payload, timeout=3)
        print(f"ゲート開放要求を送信しました: HTTP {response.status_code}")
    except Exception as e:
        print(f"ゲート開放要求に失敗しました: {e}")

# ==========================================
# データベースの初期設定
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS db1_parking_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_number TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS db2_pre_permits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_number TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            user_type TEXT NOT NULL,
            allowed_parking_lot INTEGER NOT NULL,
            QRnumber TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS db3_temp_permits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_number TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            QRnumber TEXT NOT NULL,
            userStatus TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

# ==========================================
# データ構造の定義 (Pydantic Models)
# ==========================================
class EntryRequest(BaseModel):
    car_number: str
    time: str

class CanExitRequest(BaseModel):
    car_number: str
    time: str

class ExitRequest(BaseModel):
    car_number: str

class PrePermitRequest(BaseModel):
    car_number: str
    owner_name: str
    user_type: str
    allowed_parking_lot: int
    QRnumber: str

class TempPermitRequest(BaseModel):
    car_number: str
    entry_time: str
    QRnumber: str
    userStatus: str
    
class QRCheckRequest(BaseModel):
    QRnumber: str

# ==========================================
# API 1: /entry (入ってきた車の登録 ＋ 重複時の上書き機能)
# ==========================================
@app.post("/entry")
def vehicle_entry(data: EntryRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 1. まず、許可証の状況を確認して登録するステータスを決定する
        cursor.execute("SELECT id FROM db2_pre_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        has_pre_permit = cursor.fetchone() is not None
        
        cursor.execute("SELECT id FROM db3_temp_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        has_temp_permit = cursor.fetchone() is not None
        
        if has_pre_permit:
            status = "事前許可証での滞在中"
        elif has_temp_permit:
            status = "一時許可証での滞在中"
        else:
            status = "未許可滞在中"
            
        # 2. 既に「退出済み」ではない（＝現在滞在中の）同じ車の記録があるかチェックする
        cursor.execute("""
            SELECT id FROM db1_parking_logs 
            WHERE car_number = ? AND status != '退出済み'
        """, (data.car_number,))
        
        existing_record = cursor.fetchone()
        
        if existing_record:
            # 3A. 既に滞在中の記録があれば、新しい時間とステータスで「上書き」する
            cursor.execute("""
                UPDATE db1_parking_logs 
                SET entry_time = ?, status = ? 
                WHERE car_number = ? AND status != '退出済み'
            """, (data.time, status, data.car_number))
            message = "既存の入場記録を新しい情報で上書きしました"
            
        else:
            # 3B. 滞在中の記録がなければ、通常通り「新規登録」する
            cursor.execute("""
                INSERT INTO db1_parking_logs (car_number, entry_time, status)
                VALUES (?, ?, ?)
            """, (data.car_number, data.time, status))
            message = "入場記録を保存しました"
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": message, "recorded_status": status}
        
    except Exception as e:
        # 万が一エラーが起きた場合も確実にDBを閉じる
        if 'conn' in locals():
            conn.close()
        return {"status": "error", "message": f"登録失敗: {str(e)}"}

# ==========================================
# API 2: /canExit (退場可能かどうかの事前判定 ＋ 可能ならそのまま退場処理)
# ==========================================
@app.post("/canExit")
def check_can_exit(data: CanExitRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        result_msg = ""
        should_exit = False
        
        cursor.execute("SELECT id FROM db2_pre_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        if cursor.fetchone():
            result_msg = "事前許可証あり"
            should_exit = True
        else:
            cursor.execute("SELECT id FROM db3_temp_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
            if cursor.fetchone():
                result_msg = "一時許可証あり"
                should_exit = True
            else:
                cursor.execute("""
                    SELECT entry_time FROM db1_parking_logs 
                    WHERE car_number = ? AND status != '退出済み' 
                    ORDER BY id DESC LIMIT 1
                """, (data.car_number,))
                row = cursor.fetchone()
                
                if not row:
                    conn.close()
                    return {"status": "error", "message": "入場記録が見つかりません"}
                    
                entry_time_str = row[0]
                time_format = "%Y-%m-%d %H:%M:%S"
                entry_dt = datetime.strptime(entry_time_str, time_format)
                exit_dt = datetime.strptime(data.time, time_format)
                
                duration_minutes = (exit_dt - entry_dt).total_seconds() / 60
                
                if duration_minutes <= FREE_TIME_LIMIT_MINUTES:
                    result_msg = "許可証なしかつ時間内"
                    should_exit = True
                else:
                    result_msg = "許可証なしかつ時間外"
                    should_exit = False
        
        if should_exit:
            cursor.execute("""
                UPDATE db1_parking_logs 
                SET status = '退出済み' 
                WHERE car_number = ? AND status != '退出済み'
            """, (data.car_number,))
            
            cursor.execute("""
                UPDATE db3_temp_permits 
                SET status = '無効' 
                WHERE car_number = ? AND status = '有効'
            """, (data.car_number,))
            
            conn.commit()
            
            # DBの退場処理が確定したらゲートを開ける
            open_gate()
            
        conn.close()
        return {"status": "success", "result": result_msg}
            
    except Exception as e:
        return {"status": "error", "message": f"判定・退場処理失敗: {str(e)}"}

# ==========================================
# API 3: /exit (車の退場処理)
# ==========================================
@app.post("/exit")
def vehicle_exit(data: ExitRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE db1_parking_logs 
            SET status = '退出済み' 
            WHERE car_number = ? AND status != '退出済み'
        """, (data.car_number,))
        
        cursor.execute("""
            UPDATE db3_temp_permits 
            SET status = '無効' 
            WHERE car_number = ? AND status = '有効'
        """, (data.car_number,))
        
        conn.commit()
        conn.close()
        
        # 手動・強制退場時にもゲートを開ける
        open_gate()
        
        return {"status": "success", "message": "退場処理が完了しました"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 4: /issuePrePermit (事前許可証の発行)
# ==========================================
@app.post("/issuePrePermit")
def issue_pre_permit(data: PrePermitRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM db2_pre_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        if cursor.fetchone():
            conn.close()
            return {"status": "error", "message": "既に有効な事前許可証が登録されています"}
            
        cursor.execute("""
            INSERT INTO db2_pre_permits (car_number, owner_name, user_type, allowed_parking_lot, QRnumber, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data.car_number, data.owner_name, data.user_type, data.allowed_parking_lot, data.QRnumber, "有効"))
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": "事前許可証を発行・登録しました"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 5: /issueTempPermit (一時許可証の発行)
# ==========================================
@app.post("/issueTempPermit")
def issue_temp_permit(data: TempPermitRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM db3_temp_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        if cursor.fetchone():
            conn.close()
            return {"status": "error", "message": "既に有効な一時許可証が登録されています"}
            
        cursor.execute("""
            INSERT INTO db3_temp_permits (car_number, entry_time, QRnumber, userStatus, status)
            VALUES (?, ?, ?, ?, ?)
        """, (data.car_number, data.entry_time, data.QRnumber, data.userStatus, "有効"))
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": "一時許可証を発行・登録しました"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 6: /getPermits (現在有効な事前許可証の取得)
# ==========================================
@app.get("/getPermits")
def get_permits():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM db2_pre_permits WHERE status = '有効'")
        rows = cursor.fetchall()
        conn.close()
        
        return {"status": "success", "permits": [dict(row) for row in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 7: /getTemporaryPermits (現在有効な一時許可証の取得)
# ==========================================
@app.get("/getTemporaryPermits")
def get_temporary_permits():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM db3_temp_permits WHERE status = '有効'")
        rows = cursor.fetchall()
        conn.close()
        
        return {"status": "success", "temp_permits": [dict(row) for row in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 8: /getAllCars (現在駐車中の車のデータをすべて取得)
# ==========================================
@app.get("/getAllCars")
def get_all_cars():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM db1_parking_logs WHERE status != '退出済み'")
        rows = cursor.fetchall()
        conn.close()
        
        return {"status": "success", "cars": [dict(row) for row in rows]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# API 9: /checkQR (QRコードからの許可証判別 ＋ 自動退場処理)
# ==========================================
@app.post("/checkQR")
def check_qr_permit(data: QRCheckRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT car_number FROM db2_pre_permits 
            WHERE QRnumber = ? AND status = '有効'
        """, (data.QRnumber,))
        
        pre_permit = cursor.fetchone()
        
        if pre_permit:
            car_number = pre_permit[0]
            cursor.execute("""
                UPDATE db1_parking_logs 
                SET status = '退出済み' 
                WHERE car_number = ? AND status != '退出済み'
            """, (car_number,))
            
            conn.commit()
            
            # QRコード（事前許可証）で退場条件を満たしたためゲートを開ける
            open_gate()
            
            conn.close()
            return {
                "status": "success",
                "permit_type": "事前許可証",
                "car_number": car_number,
                "message": "有効な事前許可証として認識し、退場処理を完了しました"
            }
            
        cursor.execute("""
            SELECT car_number FROM db3_temp_permits 
            WHERE QRnumber = ? AND status = '有効'
        """, (data.QRnumber,))
        
        temp_permit = cursor.fetchone()
        
        if temp_permit:
            car_number = temp_permit[0]
            
            cursor.execute("""
                UPDATE db1_parking_logs 
                SET status = '退出済み' 
                WHERE car_number = ? AND status != '退出済み'
            """, (car_number,))
            
            cursor.execute("""
                UPDATE db3_temp_permits 
                SET status = '無効' 
                WHERE QRnumber = ? AND status = '有効'
            """, (data.QRnumber,))
            
            conn.commit()
            
            # QRコード（一時許可証）で退場条件を満たしたためゲートを開ける
            open_gate()
            
            conn.close()
            return {
                "status": "success",
                "permit_type": "一時許可証",
                "car_number": car_number,
                "message": "有効な一時許可証として認識し、退場処理を完了しました（許可証は無効化されました）"
            }
            
        conn.close()
        return {
            "status": "error",
            "message": "該当する有効な許可証が見つかりません（無効または未登録）"
        }
        
    except Exception as e:
        if 'conn' in locals():
            conn.close()
        return {"status": "error", "message": f"データベース照会エラー: {str(e)}"}