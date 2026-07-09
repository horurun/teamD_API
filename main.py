from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="駐車場管理システム API")

app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
allow_credentials=True,
allow_methods=["*"],
allow_headers=["*"],
)

# --- 設定値 ---
FREE_TIME_LIMIT_MINUTES = 60
DB_FILE = "parking_system.db"

# ==========================================
# データベースの初期設定
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # db1: 駐車中の車の管理 (変更なし)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS db1_parking_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_number TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)
    
    # db2: 事前に発行される駐車場使用許可証の管理 (QRnumberを追加)
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
    
    # db3: 一時駐車場許可証の管理 (QRnumber, userStatusを追加)
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

# (変更) QRnumber (int) を追加
class PrePermitRequest(BaseModel):
    car_number: str
    owner_name: str
    user_type: str
    allowed_parking_lot: int
    QRnumber: str

# (変更) QRnumber (int) と userStatus (str) を追加
class TempPermitRequest(BaseModel):
    car_number: str
    entry_time: str
    QRnumber: str
    userStatus: str
    
class QRCheckRequest(BaseModel):
    QRnumber: str

# ==========================================
# API 1: /entry (入ってきた車の登録)
# ==========================================
@app.post("/entry")
def vehicle_entry(data: EntryRequest):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
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
            
        cursor.execute("""
            INSERT INTO db1_parking_logs (car_number, entry_time, status)
            VALUES (?, ?, ?)
        """, (data.car_number, data.time, status))
        
        conn.commit()
        conn.close()
        
        return {"status": "success", "message": "入場記録を保存しました", "recorded_status": status}
    except Exception as e:
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
        should_exit = False  # 退場処理を行うかどうかのフラグ
        
        # db2 (事前許可証) をチェック
        cursor.execute("SELECT id FROM db2_pre_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
        if cursor.fetchone():
            result_msg = "事前許可証あり"
            should_exit = True
        else:
            # db3 (一時許可証) をチェック
            cursor.execute("SELECT id FROM db3_temp_permits WHERE car_number = ? AND status = '有効'", (data.car_number,))
            if cursor.fetchone():
                result_msg = "一時許可証あり"
                should_exit = True
            else:
                # 許可証がない場合、db1 から入場時間を取り出して時間計算を行う
                cursor.execute("""
                    SELECT entry_time FROM db1_parking_logs 
                    WHERE car_number = ? AND status != '退出済み' 
                    ORDER BY id DESC LIMIT 1
                """, (data.car_number,))
                row = cursor.fetchone()
                
                # db1に入場記録すらない場合 (予期せぬエラーケース)
                if not row:
                    conn.close()
                    return {"status": "error", "message": "入場記録が見つかりません"}
                    
                # 滞在時間の計算
                entry_time_str = row[0]
                time_format = "%Y-%m-%d %H:%M:%S"
                entry_dt = datetime.strptime(entry_time_str, time_format)
                exit_dt = datetime.strptime(data.time, time_format)
                
                duration_minutes = (exit_dt - entry_dt).total_seconds() / 60
                
                # 時間内か時間外かを判定
                if duration_minutes <= FREE_TIME_LIMIT_MINUTES:
                    result_msg = "許可証なしかつ時間内"
                    should_exit = True
                else:
                    result_msg = "許可証なしかつ時間外"
                    should_exit = False
        
        # ==========================================
        # ここから追加：退場条件を満たしている場合のみ、データベースを更新する
        # ==========================================
        if should_exit:
            # db1 のナンバーの車を「退出済み」にする
            cursor.execute("""
                UPDATE db1_parking_logs 
                SET status = '退出済み' 
                WHERE car_number = ? AND status != '退出済み'
            """, (data.car_number,))
            
            # db3 に一時許可証があれば「無効」にする
            cursor.execute("""
                UPDATE db3_temp_permits 
                SET status = '無効' 
                WHERE car_number = ? AND status = '有効'
            """, (data.car_number,))
            
            # 変更を保存
            conn.commit()
            
        conn.close()
        
        # 判定結果を返す（APIの戻り値の形は今まで通り）
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
            
        # (変更) INSERT文に QRnumber を追加
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
            
        # (変更) INSERT文に QRnumber と userStatus を追加
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
# API 9: /checkQR (QRコードからの許可証判別)
# ==========================================
@app.post("/checkQR")
def check_qr_permit(data: QRCheckRequest):
    try:
        # 1. データベースに接続
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 2. まず「事前許可証 (db2_pre_permits)」のテーブルから検索
        # QRnumberが一致し、かつステータスが「有効」なものを探す
        cursor.execute("""
            SELECT car_number FROM db2_pre_permits 
            WHERE QRnumber = ? AND status = '有効'
        """, (data.QRnumber,))
        
        pre_permit = cursor.fetchone()
        
        # もし見つかったら、事前許可証であると返す
        if pre_permit:
            conn.close()
            return {
                "status": "success",
                "permit_type": "事前許可証",
                "car_number": pre_permit[0],
                "message": "有効な事前許可証として認識しました"
            }
            
        # 3. 事前許可証になければ、次に「一時許可証 (db3_temp_permits)」のテーブルを検索[cite: 2]
        cursor.execute("""
            SELECT car_number FROM db3_temp_permits 
            WHERE QRnumber = ? AND status = '有効'
        """, (data.QRnumber,))
        
        temp_permit = cursor.fetchone()
        
        # もし見つかったら、一時許可証であると返す
        if temp_permit:
            conn.close()
            return {
                "status": "success",
                "permit_type": "一時許可証",
                "car_number": temp_permit[0],
                "message": "有効な一時許可証として認識しました"
            }
            
        # 4. どちらのテーブルにも「有効」な状態で存在しない場合
        conn.close()
        return {
            "status": "error",
            "message": "該当する有効な許可証が見つかりません（無効または未登録）"
        }
        
    except Exception as e:
        # エラー発生時の処理（DBを確実に閉じる）
        if 'conn' in locals():
            conn.close()
        return {"status": "error", "message": f"データベース照会エラー: {str(e)}"}