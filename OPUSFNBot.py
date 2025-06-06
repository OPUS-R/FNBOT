import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, request, jsonify
import httpx
import json
import os
import asyncio
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseUpload
import requests
from googleapiclient.errors import HttpError

#初期設定#################################################################################################################
# Google Sheets 認証
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("GCOA.json", scope)
client = gspread.authorize(creds)

# Google Drive認証
SCOPES = ['https://www.googleapis.com/auth/drive.file']
DRIVE_CREDENTIALS = service_account.Credentials.from_service_account_file("GCOA.json", scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=DRIVE_CREDENTIALS)

# スプレッドシートの指定
debt_spreadsheet = client.open("2025年度会計書類")

# Google Drive の親フォルダ ID<URLのfolder以降>/ID以降に領収書フォルダがぶら下がる
DRIVE_FOLDER_ID = "1_2k_Pxewv71rZzY-1qmXwM7-HfKJGpnd"

# LINE Bot 認証
config = json.load(open("line.json"))
LINE_ACCESS_TOKEN = config["line_bot_token"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

#認証コードのロード
key_config = json.load(open("Lkey.json"))
SECRET_KEY = key_config["key_token"]

# 認証ユーザーの保存ファイル
USERS_FILE = "users.json"

# 領収証一時保存ディレクトリ
TEMP_DIR = "/home/eimin/BOT/drive/"
os.makedirs(TEMP_DIR, exist_ok=True)  # ディレクトリがなければ作成


# 認証済みユーザー・グループのロード
if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
        authenticated_users = set(json.load(f))
else:
    authenticated_users = set()

# 会計処理の状態を管理
accounting_state = {}
# シート作成の状態を管理
sheet_creation_state = {}

app = Flask(__name__)



#LINE Webhook処理########################################################################################################

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Received Webhook:", data)

    for event in data.get("events", []):
        message_type = event["message"]["type"]
        source = event["source"]
        sender_id = event["source"].get("userId") or event["source"].get("groupId")
        reply_token = event["replyToken"]


        if "groupId" in source:
            sender_id = source["groupId"]  # グループなら groupId を優先
        elif "userId" in source:
            sender_id = source["userId"]  # それ以外なら userId

        if not sender_id:
            asyncio.run(send_line_message(reply_token, "❌ この環境では使用できません。"))
            continue

        # 画像メッセージ受信
        if message_type == "image":
            message_id = event["message"]["id"]
            print(f"✅ 画像メッセージ受信: {message_id}")  # 確実に画像を受け取ったか確認
            asyncio.run(handle_image_message(sender_id, reply_token, message_id))
            return jsonify({"status": "ok"})

        # テキストメッセージ受信
        elif message_type == "text":
            user_message = event["message"]["text"].strip()

            # 認証キー処理
            if user_message == SECRET_KEY:
                authenticated_users.add(sender_id)
                save_authenticated_users()
                asyncio.run(send_line_message(reply_token, "✅ 認証に成功しました！コマンドが使用可能です。"))
                continue

            # 認証チェック
            if sender_id not in authenticated_users:
                asyncio.run(send_line_message(reply_token, "❌ 認証が必要です。最初に認証キーを送信してください。"))
                continue

            # シート作成処理起動
            if user_message == "シート" or sender_id in sheet_creation_state:
                asyncio.run(handle_sheet_creation(sender_id, user_message, reply_token))
                continue

            # 滞納情報取得起動
            if user_message == "滞納":
                debt_message = asyncio.run(get_debt_info())
                asyncio.run(send_line_message(reply_token, debt_message))
                continue

            # 会計処理起動
            if user_message == "会計処理":
                asyncio.run(start_accounting_process(sender_id, reply_token))
                continue

            # 会計処理のステップ管理
            if sender_id in accounting_state:
                asyncio.run(handle_accounting_step(sender_id, user_message, reply_token, event))
                continue

    return jsonify({"status": 200}), 200


#認証済みユーザーの保存
def save_authenticated_users():
    with open(USERS_FILE, "w") as f:
        json.dump(list(authenticated_users), f)




#LINEBotからメッセージを送信する関数#########################################################################################
async def send_line_message(reply_token, message):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": message}]}

    timeout = httpx.Timeout(10.0)  # タイムアウトを10秒に設定
    max_retries = 3  # 最大3回リトライ

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
                if response.status_code == 200:
                    return  # 成功したら終了
                else:
                    print(f"⚠️ LINE API エラー ({response.status_code}): {response.text}")
            except httpx.ConnectTimeout:
                print(f"⏳ LINE API 接続タイムアウト (試行 {attempt+1}/{max_retries})")
            except httpx.RequestError as e:
                print(f"❌ LINE API 送信エラー: {e}")

            await asyncio.sleep(2)  # 2秒待機して再試行

    print("❌ LINE API との通信が完全に失敗しました")  #接続失敗

#LINEBotにメッセージを送る関数予備。replyTokenで返せない時に使う。
async def push_line_message(user_id, message):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }

    push_url = "https://api.line.me/v2/bot/message/push"

    async with httpx.AsyncClient() as client:
        response = await client.post(push_url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"❌ push_message 送信失敗: {response.status_code} - {response.text}")



#シート作成プロセス########################################################################################################

#シート作成関数
async def handle_sheet_creation(sender_id, user_message, reply_token):
    if user_message == "キャンセル":
        if sender_id in sheet_creation_state:
            del sheet_creation_state[sender_id]  # シート作成をキャンセル
            await send_line_message(reply_token, "❌ キャンセルしました。最初からやり直してください。")
        else:
            await send_line_message(reply_token, "⚠ 現在進行中の作業はありません。")
        return

    if sender_id not in sheet_creation_state:   #シート作成ステップ処理
        if user_message == "シート":
            sheet_creation_state[sender_id] = {"step": 1}
            await send_line_message(reply_token, "📌 イベントかライブを選択してください（「イベント」または「ライブ」）")
        return

    step = sheet_creation_state[sender_id]["step"]

    if step == 1:
        if user_message in ["イベント", "ライブ"]:
            sheet_creation_state[sender_id]["type"] = user_message
            sheet_creation_state[sender_id]["step"] = 2
            await send_line_message(reply_token, f"📌 {user_message}名を入力してください。")
        else:
            await send_line_message(reply_token, "❌ 無効な選択です。「イベント」または「ライブ」と入力してください。")

    elif step == 2:
        name = user_message
        sheet_creation_state[sender_id]["name"] = name


        # シート名が既に存在するか確認
        existing_sheets = [sheet.title for sheet in debt_spreadsheet.worksheets()]
        if f"{name}個別" in existing_sheets or f"滞納({name})" in existing_sheets:
            await send_line_message(reply_token, f"❌ シート {name} は既に存在します。別の名前を入力してください。")
            return

        if sheet_creation_state[sender_id]["type"] == "イベント":
            await asyncio.to_thread(create_event_sheets, name)
        else:
            await asyncio.to_thread(create_live_sheets, name)

        await send_line_message(reply_token, f"✅ シート {name} を作成しました！")
        del sheet_creation_state[sender_id]




#Sheet上にイベントシートを複製し作成、
def create_event_sheets(event_name):
    template_individual = debt_spreadsheet.worksheet("個別テンプレート")
    template_debt = debt_spreadsheet.worksheet("滞納(イ)テンプレート")

    new_individual = debt_spreadsheet.duplicate_sheet(template_individual.id, new_sheet_name=f"{event_name}個別")
    new_debt = debt_spreadsheet.duplicate_sheet(template_debt.id, new_sheet_name=f"滞納({event_name})")

    new_individual.update("W4", [[f"='滞納({event_name})'!I6"]],raw=False)    #滞納と個別会計書類をリンクさせる

#Sheet上にライブ滞納シートを複製し作成
def create_live_sheets(live_name):
    template_live = debt_spreadsheet.worksheet("滞納(ラ)テンプレート")
    debt_spreadsheet.duplicate_sheet(template_live.id, new_sheet_name=f"滞納({live_name})")



#滞納合算タスク###########################################################################################################

async def get_debt_info():
    debt_sheets = [sheet for sheet in debt_spreadsheet.worksheets() if "滞納" in sheet.title] #滞納が付くタブを取得

    debt_data = {}

    for sheet in debt_sheets:
        names_a = sheet.col_values(1)[4:]
        names_k = sheet.col_values(11)[4:]
        debts_a = sheet.col_values(5)[4:]
        debts_k = sheet.col_values(15)[4:]

        for name, debt in zip(names_a, debts_a):
            if name and debt.isdigit() and int(debt) > 0:
                debt_data[name] = debt_data.get(name, 0) + int(debt)

        for name, debt in zip(names_k, debts_k):
            if name and debt.isdigit() and int(debt) > 0:
                debt_data[name] = debt_data.get(name, 0) + int(debt)

    if not debt_data:
        return "滞納者は居ません。"

    return "📝 滞納一覧 📝\n" + "\n".join([f"{name}: {total_debt}円" for name, total_debt in debt_data.items()])





#会計処理タスク############################################################################################################

#会計処理はじめ
async def start_accounting_process(sender_id, reply_token):
    sheets = [sheet.title for sheet in debt_spreadsheet.worksheets() if "個別" in sheet.title and "個別テンプレート" not in sheet.title]
    accounting_state[sender_id] = {"step": 1, "sheets": sheets}
    await send_line_message(reply_token, "📌 シートを選択してください:\n" + "\n".join(sheets))

#会計処理ステップ管理
async def handle_accounting_step(sender_id, user_message, reply_token, event):
    state = accounting_state[sender_id]

    if user_message.lower() == "キャンセル":
        del accounting_state[sender_id]  # 処理状態をリセットする
        await send_line_message(reply_token, "❌ 会計処理をキャンセルしました。")
        return

    if state["step"] == 1:
        #ユーザーがリストにないシートを選択した場合、エラーを送信する
        if user_message not in state["sheets"]:
            await send_line_message(reply_token, "❌ 無効なシート名です。リストにあるシート名を入力してください。")
            return

        state["sheet_name"] = user_message
        state["step"] = 2
        await send_line_message(reply_token, "📌 費用か収入を入力してください（「費用」または「収入」）")
        return


    if state["step"] == 2:
        if user_message in ["費用", "収入"]:
            state["type"] = user_message
            state["step"] = 3
            await send_line_message(reply_token, f"📌 {user_message}の項目名を入力してください。")
        else:
            await send_line_message(reply_token, "❌ 無効な入力です。「費用」または「収入」を選択してください。")
        return

    if state["step"] == 3:
        state["item_name"] = user_message
        state["step"] = 4
        await send_line_message(reply_token, "📌 内訳（数）を入力してください。")
        return

    if state["step"] == 4:
        state["quantity"] = user_message
        state["step"] = 5
        await send_line_message(reply_token, "📌 単価を入力してください。")
        return

    if state["step"] == 5:
        state["price"] = user_message
        state["step"] = 6
        state["receipt_images"] = []  # 複数画像を保存する
        await send_line_message(reply_token, "📌 領収書の写真(⚠️写真のみ。pdf未対応)をアップロードしてください(複数枚可！)。完了したら「完了」と入力してください。<収入の場合完了とだけ送ってください>")
        return

    if state["step"] == 6:
        if user_message.lower() == "完了":
            state["step"] = 7
            await send_line_message(reply_token, "📌 支払い済みですか？（はい / いいえ）<収入の場合いいえを選択してください>")
            return

    # 画像メッセージを受け取った場合
    if event["message"]["type"] == "image":
        message_id = event["message"]["id"]
        print(f"✅ 取得した message_id: {message_id}")  # デバッグ用メッセージ
        await handle_image_message(sender_id, reply_token, message_id)
        return

    if state["step"] == 7:
        if user_message == "はい":
            state["step"] = 8
            state["payment_confirmed"] = True  # 支払い済みの場合Trueを格納
            await finalize_accounting(sender_id, reply_token)  # `finalize_accounting()` を呼ぶ
            return
        elif user_message == "いいえ":
            state["step"] = 8  # ✅ 次のステップへ
            state["payment_confirmed"] = False  # ✅ 未払いとしてFalseを格納
            await finalize_accounting(sender_id, reply_token)  # `finalize_accounting()` を呼ぶ
            return
        else:
            await send_line_message(reply_token, "❌ 無効な文字列です。「はい」か「いいえ」で入力してください。")
            return


#会計処理確定➡シートを更新➡領収証をドライブにアップロード
async def finalize_accounting(sender_id, reply_token):
    state = accounting_state.get(sender_id)
    if not state:
        return

    sheet = debt_spreadsheet.worksheet(state["sheet_name"])
    receipt_urls = []

    # Google Drive にすべての領収証をアップロード
    for image_path in state["receipt_images"]:
        receipt_url = await asyncio.to_thread(upload_receipt_to_drive, state["sheet_name"], state["item_name"], image_path)
        if receipt_url:
            receipt_urls.append(receipt_url)

    #スプレッドシートデータを更新
    col = "A" if state["type"] == "費用" else "N"
    row = len(sheet.col_values(1)) + 1

    try:
        sheet.update(range_name=f"{col}{row}", values=[[state["item_name"]]])
        sheet.update(range_name=f"{chr(ord(col) +1)}{row}", values=[[state["quantity"]]])
        sheet.update(range_name=f"{chr(ord(col) +2)}{row}", values=[[state["price"]]])
        if receipt_urls:
            sheet.update(range_name=f"{chr(ord(col) +4)}{row}", values=[[", ".join(receipt_urls)]])
    except Exception as e:
        print(f"❌ Google Sheets 更新エラー: {e}")
        await push_line_message(sender_id, "⚠️ 会計データの保存中にエラーが発生しました。")
        return

        # 支払い確認が "はい" の場合、Aの10➡に TRUE
    if state.get("payment_confirmed"):
        col = "A" if state["type"] == "費用" else "N"
        row = len(sheet.col_values(1))  # 最新の行を取得
        target_cell = f"{chr(ord(col) +10)}{row }"
        print(f"✅ {target_cell} に TRUE を設定します")  # ログ
        sheet.update(target_cell, [[True]])

    del accounting_state[sender_id]
    await push_line_message(sender_id, f"✅ 会計処理が完了しました！{len(receipt_urls)} 枚の領収書がアップロードされました。")





#受信画像メッセージ処理(取得➡保存)
async def handle_image_message(sender_id, reply_token, message_id):
    print(f"✅ 画像メッセージを処理中: {message_id}")#ログ

    # 画像の保存処理
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    image_path = os.path.join(TEMP_DIR, f"{message_id}.jpg")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code == 200:
                with open(image_path, "wb") as f:
                    f.write(response.content)
                print(f"✅ 画像保存完了: {image_path}")
            else:
                print(f"❌ 画像取得エラー: {response.status_code} - {response.text}")
                await send_line_message(reply_token, "⚠️ 画像の取得に失敗しました。もう一度送信してください。")
                return
    except Exception as e:
        print(f"❌ 画像取得エラー: {e}")
        await send_line_message(reply_token, "⚠️ 画像の取得に失敗しました。もう一度送信してください。")
        return

    # accounting_state に画像パスを追加
    if sender_id in accounting_state:
        accounting_state[sender_id]["receipt_images"].append(image_path)
        await send_line_message(reply_token, f"✅ 画像を保存しました ({len(accounting_state[sender_id]['receipt_images'])}枚)\n他の画像があれば続けて送信してください。完了したら「完了」と入力してください。")
    else:
        await send_line_message(reply_token, "⚠️ 画像を保存しましたが、会計処理が開始されていません。まず「会計処理」と入力してください。")



#Driveに写真をアップロード
def upload_receipt_to_drive(sheet_name, item_name, file_path):
    try:
        if not os.path.exists(file_path):
            print(f"❌ ファイルが存在しません: {file_path}")
            return None

        folder_id = get_or_create_folder(sheet_name, DRIVE_FOLDER_ID)
        item_folder_id = get_or_create_folder(item_name, folder_id)

        file_metadata = {"name": os.path.basename(file_path), "parents": [item_folder_id]}

        with open(file_path, "rb") as f:
            media = MediaIoBaseUpload(f, mimetype="image/jpeg")
            file_response = drive_service.files().create(body=file_metadata, media_body=media).execute()

        if "id" in file_response:
            drive_url = f"https://drive.google.com/file/d/{file_response['id']}/view?usp=sharing"
            print(f"✅ Google Drive アップロード完了: {drive_url}")
            return drive_url
        else:
            print("❌ Google Drive へのアップロード失敗")
            return None
    except Exception as e:
        print(f"❌ Google Drive API エラー: {e}")
        return None

#Driveフォルダ作成
def get_or_create_folder(folder_name, parent_id):
    query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'"
    response = drive_service.files().list(q=query, fields="files(id)").execute()
    folders = response.get("files", [])
    if folders:
        return folders[0]["id"]
    file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = drive_service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]

#Flask起動##########################################################################################################
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)