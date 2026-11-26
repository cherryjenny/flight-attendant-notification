import os
import sys
import json
import asyncio
import datetime
import pytz
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai
import edge_tts

# 환경변수 (GitHub Secrets에서 주입)
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BAND_ACCESS_TOKEN = os.environ["BAND_ACCESS_TOKEN"]
BAND_KEY = os.environ["BAND_KEY"]
GITHUB_PAGES_BASE_URL = os.environ["GITHUB_PAGES_BASE_URL"].rstrip("/")  # 예: https://아이디.github.io/저장소명

VOICE = "en-GB-SoniaNeural"  # 영국 여성 성우 (미국식은 en-US-JennyNeural 추천)
PENDING_FILE = "pending_task.json"
DAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]


def get_gspread_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def generate_recruiter_question(raw_question: str) -> str:
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
You are a senior international airline cabin crew recruiter conducting a final face-to-face interview.
Transform the core interview topic below into a realistic, conversational, and slightly indirect question that a real recruiter would ask in the interview room.

Rules:
1. Do NOT use the exact textbook phrasing directly if it is a classic cliché (e.g., instead of "Tell me about yourself", set a warm context and ask for their story in their own words).
2. Keep it natural, polite, and realistic (2 to 4 sentences).
3. Output ONLY the spoken English script—no labels, no quotes, no explanations.

Core Question Topic: {raw_question}
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


async def text_to_mp3(text: str, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    communicate = edge_tts.Communicate(text, VOICE, rate="-5%")
    await communicate.save(output_path)


def prepare():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    today_str = now.strftime("%Y-%m-%d")
    today_day = DAYS_KR[now.weekday()]

    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    # 1. '제어' 시트에서 오늘 요일 휴무(X) 여부 확인
    control_ws = sh.worksheet("제어")
    headers = control_ws.row_values(1)
    flags = control_ws.row_values(2)
    day_map = {h.strip(): (flags[i].strip().upper() if i < len(flags) else "") for i, h in enumerate(headers)}

    if day_map.get(today_day) == "X":
        print(f"[{today_str} ({today_day})] 휴무일로 설정되어 작업을 건너뜁니다.")
        return

    # 2. '질문목록' 시트에서 미진행(C열이 빈칸인) 첫 번째 질문 조회
    list_ws = sh.worksheet("질문목록")
    rows = list_ws.get_all_values()
    target_row_idx = None
    q_num = ""
    raw_q = ""

    for idx, row in enumerate(rows[1:], start=2):  # 1행은 헤더
        num = row[0].strip() if len(row) > 0 else ""
        question = row[1].strip() if len(row) > 1 else ""
        status = row[2].strip() if len(row) > 2 else ""
        if question and not status:
            target_row_idx = idx
            q_num = num or str(idx - 1)
            raw_q = question
            break

    if not target_row_idx:
        print("모든 질문이 이미 출제되었습니다.")
        return

    # 3. LLM으로 면접관 화법 변환 및 MP3 생성
    recruiter_q = generate_recruiter_question(raw_q)
    audio_rel_path = f"docs/audio/q_{q_num}_{now.strftime('%Y%m%d')}.mp3"
    asyncio.run(text_to_mp3(recruiter_q, audio_rel_path))

    audio_url = f"{GITHUB_PAGES_BASE_URL}/audio/q_{q_num}_{now.strftime('%Y%m%d')}.mp3"

    # 다음 단계(--publish)를 위해 임시 상태 저장
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "today_str": today_str,
                "today_day": today_day,
                "row_idx": target_row_idx,
                "q_num": q_num,
                "raw_q": raw_q,
                "recruiter_q": recruiter_q,
                "audio_url": audio_url,
            },
            f,
            ensure_ascii=False,
        )
    print(f"준비 완료: Q{q_num} -> {audio_url}")


def publish():
    if not os.path.exists(PENDING_FILE):
        print("게시할 대기 작업이 없습니다.")
        return

    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        task = json.load(f)

    # 4. 네이버 밴드 게시글 업로드
    post_content = (
        f"✈️ 오늘의 외항사 파이널 리스닝 Q{task['q_num']} ({task['today_str']} {task['today_day']})\n\n"
        f"🎧 아래 링크를 눌러 면접관의 질문을 듣고 답변을 연습해 보세요!\n"
        f"{task['audio_url']}\n\n"
        f"💡 스크립트를 먼저 보지 말고 귀로 먼저 요지를 파악해 보세요."
    )

    res = requests.post(
        "https://openapi.band.us/v2.2/band/post/create",
        data={
            "access_token": BAND_ACCESS_TOKEN,
            "band_key": BAND_KEY,
            "content": post_content,
            "do_push": "true",
        },
        timeout=15,
    )
    res.raise_for_status()
    res_json = res.json()
    if res_json.get("result_code") != 1:
        raise RuntimeError(f"밴드 업로드 실패: {res_json}")

    # 5. 밴드 게시 성공 후 구글 시트 업데이트 ('질문목록' 완료 표시 + '생성기록' 추가)
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    list_ws = sh.worksheet("질문목록")
    list_ws.update_cell(task["row_idx"], 3, f"완료 ({task['today_str']})")

    log_ws = sh.worksheet("생성기록")
    log_ws.append_row([task["q_num"], task["raw_q"], task["recruiter_q"], task["audio_url"]])

    os.remove(PENDING_FILE)
    print("밴드 게시 및 구글 시트 기록 완료!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--publish":
        publish()
    else:
        prepare()
