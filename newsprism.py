import streamlit as st
import streamlit.components.v1 as components
from gnews import GNews
from google import genai
import yfinance as yf
import requests
import re
from datetime import datetime, timedelta
import pytz
import time
import json
import os
import yt_dlp
import ast

# ==========================================
# 📌 [보안 강화] API 키 환경변수 / st.secrets 우선 로드
# ==========================================
# ✅ FIX #7: API 키 하드코딩 제거 → 환경변수 또는 Streamlit secrets 사용
# 사용법: .streamlit/secrets.toml 파일에 키를 저장하거나 환경변수로 주입하세요.
# 예시 secrets.toml:
#   GEMINI_API_KEY = "실제_키_입력"
#   NAVER_CLIENT_ID = "실제_키_입력"
#   NAVER_CLIENT_SECRET = "실제_키_입력"
#   NEWS_API_KEY = "실제_키_입력"
#   YOUTUBE_API_KEY = "실제_키_입력"

def _get_secret(key: str, fallback: str = "") -> str:
    """환경변수 → st.secrets → fallback 순으로 키를 로드합니다."""
    val = os.environ.get(key, "")
    if val:
        return val
    try:
        return st.secrets.get(key, fallback)
    except Exception:
        return fallback

# 🚨 [중요] fallback 값을 빈 문자열("")로 비워두어 깃허브 노출을 원천 차단했습니다.
GEMINI_API_KEY      = _get_secret("GEMINI_API_KEY",      "")
NAVER_CLIENT_ID     = _get_secret("NAVER_CLIENT_ID",     "")
NAVER_CLIENT_SECRET = _get_secret("NAVER_CLIENT_SECRET", "")
NEWS_API_KEY        = _get_secret("NEWS_API_KEY",        "")
YOUTUBE_API_KEY     = _get_secret("YOUTUBE_API_KEY",     "")

# 초고속 gemini-2.5-flash 단일 엔진
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 📌 캐시(백업) 시스템
# ==========================================
CACHE_FILE = "prism_cache_v9.json"

def save_session_to_disk(market_data, filtered_data, news_map_data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "market": market_data,
                "filtered": filtered_data,
                "news_map": news_map_data
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Error] 캐시 저장 실패: {e}")

def load_session_from_disk():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("market", {}), data.get("filtered", {}), data.get("news_map", {})
    except Exception as e:
        print(f"[Error] 캐시 로드 실패: {e}")
    return {}, {}, {}

# ==========================================
# 📌 뉴스 엔진 및 유틸리티 로직
# ==========================================
def sanitize_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('"', "'").replace('\n', ' ').replace('\r', ' ')
    text = text.replace('&quot;', "'").replace('&amp;', '&').replace('&apos;', "'")
    return text.strip()

def is_within_15_hours(date_str):
    if not date_str:
        return True
    try:
        now_utc = datetime.now(pytz.UTC)
        if 'T' in date_str and 'Z' in date_str:
            dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        elif '+0900' in date_str:
            dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").astimezone(pytz.UTC)
        elif 'GMT' in date_str:
            dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=pytz.UTC)
        else:
            return True
        return (now_utc - dt).total_seconds() <= 15 * 3600
    except Exception:
        return True

# 🚨 Finple 완벽 모방형: yf.Ticker 사용 및 period="5d" 축소 적용
def get_market_indicators():
    import yfinance as yf
    
    indicators = {}
    tickers = {
        '다우': '^DJI',
        '나스닥': '^IXIC',
        'S&P500': '^GSPC',
        '러셀 2000': '^RUT',
        '필라델피아 반도체': '^SOX',
        '환율(원/달러)': 'KRW=X',
        'WTI유가': 'CL=F'
    }

    for name, ticker_symbol in tickers.items():
        try:
            # Finple과 완전히 동일한 객체 생성 및 기간(5d) 설정
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="5d")
            
            if hist is not None and not hist.empty:
                valid_hist = hist.dropna(subset=['Close'])
                
                if len(valid_hist) >= 2:
                    # Finple 로직 동일 적용 (iloc[-1], iloc[-2] 추출)
                    current_price = valid_hist['Close'].iloc[-1]
                    previous_close = valid_hist['Close'].iloc[-2]
                    
                    # 스칼라 값 안전 변환 (pandas 버전 호환성 방어)
                    current_price = float(current_price.item() if hasattr(current_price, 'item') else current_price)
                    previous_close = float(previous_close.item() if hasattr(previous_close, 'item') else previous_close)
                    
                    change = current_price - previous_close
                    change_percent = (change / previous_close) * 100 if previous_close else 0
                    
                    indicators[name] = f"{current_price:,.2f} ({change:+.2f}, {change_percent:+.2f}%)"
                elif len(valid_hist) == 1:
                    current_price = valid_hist['Close'].iloc[-1]
                    current_price = float(current_price.item() if hasattr(current_price, 'item') else current_price)
                    indicators[name] = f"{current_price:,.2f} (변동폭 계산 불가)"
                else:
                    indicators[name] = "장 마감/지연"
            else:
                indicators[name] = "데이터 없음"
                
        except Exception as e:
            print(f"[Error] 시장 지표({name}) 로드 실패: {e}")
            indicators[name] = "통신 장애"

    return indicators

# 🚨 [원상복구 완료] 강력한 언론사 화이트리스트 필터링
def is_valid_article(title, publisher, link):
    if "포토" in title or "[사진]" in title or "M포토" in title:
        return False

    ALLOWED_PUBLISHERS = [
        "경향신문", "국민일보", "동아일보", "문화일보", "서울신문", "세계일보", "조선일보", "중앙일보", "한겨레", "한국일보",
        "뉴스1", "뉴시스", "연합뉴스", "연합뉴스TV", "채널A", "한국경제TV", "JTBC", "KBS", "MBC", "MBN", "SBS", "SBS Biz", "TV조선", "YTN",
        "매일경제", "머니투데이", "비즈워치", "서울경제", "아시아경제", "이데일리", "조선비즈", "조세일보", "파이낸셜뉴스", "한국경제", "헤럴드경제",
        "노컷뉴스", "더팩트", "데일리안", "시대일보", "미디어오늘", "아이뉴스24", "오마이뉴스", "프레시안",
        "디지털데일리", "디지털타임스", "블로터", "전자신문", "지디넷코리아",
        "더스쿠프", "레이디경향", "매경이코노미", "시사IN", "시사저널", "신동아", "월간 산", "이코노미스트", "주간경향", "주간동아", "주간조선", "중앙SUNDAY", "한겨레21", "한경비즈니스",
        "기자협회보", "농민신문", "뉴스타파", "동아사이언스", "여성신문", "일다", "코리아중앙데일리", "코리아헤럴드", "코메디닷컴", "헬스조선",
        "강원도민일보", "강원일보", "경기일보", "국제신문", "대구MBC", "대전일보", "매일신문", "부산일보", "전주MBC", "CJB청주방송", "JIBS", "kbc광주방송",
        "블룸버그", "로이터", "AP통신", "AFP통신", "CNN", "BBC", "월스트리트저널", "WSJ", "뉴욕타임스", "NYT", "파이낸셜타임스", "FT", "CNBC"
    ]

    ALLOWED_DOMAINS = [
        "khan", "kmib", "donga", "munhwa", "seoul.co.kr", "segye", "chosun", "joongang", "hani", "hankookilbo",
        "news1", "newsis", "yna", "yonhap", "channela", "wowtv", "jtbc", "kbs", "mbc", "mbn", "sbs", "sbsbiz", "tvchosun", "ytn",
        "mk.co.kr", "mt.co.kr", "bizwatch", "sedaily", "asiae", "edaily", "biz.chosun", "joseilbo", "fnnews", "hankyung", "heraldcorp",
        "nocutnews", "tf.co.kr", "dailian", "mediatoday", "inews24", "ohmynews", "pressian",
        "ddaily", "dt.co.kr", "bloter", "etnews", "zdnet",
        "thescoop", "sisain", "sisajournal", "shindonga", "economist", "newstapa", "dongascience", "koreaherald", "koreajoongangdaily", "kormedi", "healthchosun",
        "kado.net", "kwnews", "kyeonggi", "kookje", "daejonilbo", "imaeil", "busan.com",
        "bloomberg", "reuters", "apnews", "afp", "cnn", "bbc", "wsj", "nytimes", "ft.com", "cnbc"
    ]

    if publisher:
        for allowed in ALLOWED_PUBLISHERS:
            if allowed.lower() in publisher.lower():
                return True

    if link:
        for domain in ALLOWED_DOMAINS:
            if domain in link.lower():
                return True

    return False

# 🚀 단일 섹션 뉴스 수집 엔진 (트리플 엔진 완벽 복구)
def fetch_single_sector_news(sector_name, search_query, start_idx):
    all_news_context = []
    news_map = {}
    article_idx = start_idx

    # 1. GNews 엔진
    google_news = GNews(language='ko', country='KR', max_results=30, period='15h')
    try:
        for item in google_news.get_news(search_query):
            if is_within_15_hours(item.get('published date', '')):
                pub = item.get('publisher', {})
                publisher_name = pub.get('title', '') if isinstance(pub, dict) else str(pub)

                if not is_valid_article(item['title'], publisher_name, item.get('url', '')):
                    continue

                clean_title = sanitize_text(item['title'])
                n_id = f"N{article_idx}"
                news_map[n_id] = {"url": item['url'], "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                all_news_context.append(f"[ID:{n_id}] {clean_title}")
                article_idx += 1
    except Exception as e:
        print(f"[Error] GNews 수집 실패 ({sector_name}): {e}")

    # 2. Naver Search API 엔진
    try:
        naver_url = "https://openapi.naver.com/v1/search/news.json"
        naver_headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
        naver_res = requests.get(naver_url, headers=naver_headers, params={"query": search_query, "display": 30, "sort": "sim"})
        if naver_res.status_code == 200:
            for item in naver_res.json().get('items', []):
                if is_within_15_hours(item.get('pubDate', '')):
                    link = item.get('originallink', item.get('link', ''))
                    clean_title = sanitize_text(item['title'])

                    if not is_valid_article(clean_title, "", link):
                        continue

                    n_id = f"N{article_idx}"
                    news_map[n_id] = {"url": link, "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                    all_news_context.append(f"[ID:{n_id}] {clean_title}")
                    article_idx += 1
    except Exception as e:
        print(f"[Error] 네이버 뉴스 수집 실패 ({sector_name}): {e}")

    # 🚨 3. [원상복구 완료] NewsAPI 외신 수집 엔진
    try:
        newsapi_url = "https://newsapi.org/v2/everything"
        newsapi_params = {"apiKey": NEWS_API_KEY, "q": search_query, "language": "ko", "sortBy": "publishedAt", "pageSize": 30}
        newsapi_res = requests.get(newsapi_url, params=newsapi_params)
        if newsapi_res.status_code == 200:
            for item in newsapi_res.json().get('articles', []):
                if item.get('title') and item['title'] != "[Removed]":
                    if is_within_15_hours(item.get('publishedAt', '')):
                        pub = item.get('source', {})
                        publisher_name = pub.get('name', '') if isinstance(pub, dict) else str(pub)
                        clean_title = sanitize_text(item['title'])

                        if not is_valid_article(clean_title, publisher_name, item.get('url', '')):
                            continue

                        n_id = f"N{article_idx}"
                        news_map[n_id] = {"url": item.get('url', ''), "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                        all_news_context.append(f"[ID:{n_id}] {clean_title}")
                        article_idx += 1
    except Exception as e:
        print(f"[Error] NewsAPI 수집 실패 ({sector_name}): {e}")

    return "\n".join(all_news_context), news_map, article_idx

def apply_prism_lens_single(sector_name, news_context, user_interest, target_kw):
    if not news_context.strip():
        return []

    # ✅ FIX #2: raw_text를 함수 스코프 상단에 미리 초기화 → NameError 방지
    raw_text = ""

    prompt = f"""
    당신은 데이터 분류 및 중복 제거 전문가입니다.
    아래 [{sector_name}] 섹션에 수집된 원본 기사들 중, 중복된 이슈를 하나로 묶고 가장 정보가 풍부한 기사를 최대 10개만 선별하세요.
    반드시 마크다운 코드 블록 없는 '순수 JSON 배열(Array) 형식'으로만 응답하세요.

    [수집 원본]
    {news_context}

    [사용자의 선택 기준 / 섹션 타겟 키워드]
    {user_interest} / {target_kw if target_kw else "없음"}

    [★★★ 절대 준수 규칙 ★★★]
    1. 원본의 `[ID:N숫자]` 꼬리표를 확인하여 ID를 정확히 매칭하세요.
    2. JSON의 모든 키(key)와 문자열 값(value)은 반드시 쌍따옴표(")로 감싸야 합니다 (표준 JSON 규격).
    3. 기사 제목(title) 내부에 인용구가 있다면, 파싱 에러 방지를 위해 기사 제목 내부의 따옴표만 홑따옴표(')로 변경하세요.
    4. 부가 설명 없이 대괄호 [] 로 시작하고 끝나는 JSON 배열만 출력하세요.

    [출력 JSON 구조 예시]
    [
        {{"id": "N1", "title": "순수한 뉴스 제목 1"}},
        {{"id": "N2", "title": "이것은 '인용구'가 포함된 제목입니다"}}
    ]
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        raw_text = response.text.strip()
        cleaned_text = raw_text.replace("```json", "").replace("```", "").strip()

        json_match = re.search(r'\[.*\]', cleaned_text, re.DOTALL)
        if json_match:
            cleaned_text = json_match.group(0)

        try:
            # 1차 시도: 표준 JSON 파싱
            return json.loads(cleaned_text)
        except json.JSONDecodeError:
            # 2차 시도: AI가 홑따옴표를 썼을 경우 파이썬 기본 해석기(ast)로 강제 파싱 구출
            return ast.literal_eval(cleaned_text)

    except Exception as e:
        print(f"[Error] JSON 파싱 2차 구출 실패 ({sector_name}): {e}\n원본응답: {raw_text[:100]}...")
        return [{"id": f"err_{sector_name}", "title": f"🚨 {sector_name} 데이터 정제 중 오류 발생"}]

def generate_headline_data_summary(title, snippet):
    prompt = f"""
    "{title}"
    이 기사의 내용을 찾아줘.
    기사에 나온 "통계", "인용구", "숫자", "데이터" 등 중요한 요소를 꼭 포함시켜서 이 기사 내용을 3문단으로 요약해줘.
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text
    except Exception as e:
        print(f"[Error] 뉴스 헤드라인 요약 생성 실패: {e}")
        return f"요약 생성 중 오류 발생: {e}"

# ==========================================
# 📺 yt-dlp 기반 유튜브 엔진
# ==========================================
def fetch_youtube_videos_15h(channel_id):
    time_15h_ago = datetime.now(pytz.UTC) - timedelta(hours=15)
    published_after = time_15h_ago.isoformat().replace("+00:00", "Z")

    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "maxResults": 10,
        "order": "date",
        "type": "video",
        "publishedAfter": published_after,
        "key": YOUTUBE_API_KEY
    }

    try:
        res = requests.get(url, params=params)
        if res.status_code == 200:
            return res.json().get("items", [])
        return []
    except Exception as e:
        print(f"[Error] 유튜브 영상 리스트 수집 실패: {e}")
        return []

def extract_youtube_info_sync(url: str):
    try:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['ko', 'en'],
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"[Error] yt_dlp 정보 추출 실패: {e}")
        return None

def extract_transcript_and_summarize(video_id, title, description):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"📥 영상 데이터 스캔 시작: {video_url}")

    info = extract_youtube_info_sync(video_url)
    full_text = ""

    if info:
        subtitles = info.get('subtitles')
        auto_subs = info.get('automatic_captions')
        target_sub = None

        if subtitles and 'ko' in subtitles:       target_sub = subtitles['ko']
        elif auto_subs and 'ko' in auto_subs:     target_sub = auto_subs['ko']
        elif subtitles and 'en' in subtitles:     target_sub = subtitles['en']
        elif auto_subs and 'en' in auto_subs:     target_sub = auto_subs['en']

        if target_sub:
            # ✅ FIX #6: next()에 기본값 None 적용 + target_sub 빈 리스트 방어 처리
            json3_url = next((fmt['url'] for fmt in target_sub if fmt.get('ext') == 'json3'), None)
            if not json3_url and target_sub:
                json3_url = target_sub[0].get('url')

            if json3_url:
                try:
                    sub_resp = requests.get(json3_url)
                    if sub_resp.status_code == 200:
                        data = sub_resp.json()
                        events = data.get('events', [])
                        texts = []
                        for event in events:
                            if 'segs' in event:
                                for seg in event['segs']:
                                    if 'utf8' in seg:
                                        texts.append(seg['utf8'])
                        full_text = " ".join(texts)
                except Exception as e:
                    print(f"[Error] JSON3 자막 파싱 실패: {e}")

    if not full_text or len(full_text) < 50:
        print("⚠️ 자막이 없거나 짧아 설명란 메타데이터로 대체합니다.")
        full_text = f"영상 설명: {description}\n(주의: 이 영상은 자막 추출이 불가하여 설명란으로 요약합니다.)"

    prompt = f"""
    아래는 유튜브 영상 '{title}'의 대본(자막) 또는 설명란 내용입니다.
    이를 바탕으로 영상을 직접 본 것처럼 완벽하게 요약해 주세요.

    [영상 대본/설명]
    {full_text[:200000]}

    [요약 규칙]
    0. 당신은 국제정세/경제 전문 애널리스트이자 탑티어 뉴스 큐레이터입니다.
    1. 영상에서 나온 통계, 인용구, 숫자, 데이터, 인물 등 핵심 요소를 반드시 포함시키세요
    2. 1번에서 나온 요소들의 타임스탬프도 함께 표기하여 영상 어느 부분에서 언급되었는지 명시하세요.
    3. 요약은 최대 5문단으로 구성하고 각 문단은 5~7문장으로 풍부하게 작성하세요.
    4. 각 문단에서 가장 중요한 데이터들은 굵은 글씨로 강조하세요.
    """

    try:
        response = client.models.generate_content(model='gemini-2.5-pro', contents=prompt)
        return response.text
    except Exception as e:
        print(f"[Error] 유튜브 AI 영상 요약 실패: {e}")
        return "AI 요약 중 오류가 발생했습니다."


# ==========================================
# 📌 메인 앱 렌더링
# ==========================================
def main():
    st.set_page_config(page_title="News Prism V9.7", page_icon="💎", layout="wide")

    st.markdown("""
        <style>
            .main, .main .block-container { overflow: visible !important; }
            div[data-testid="stColumn"]:nth-of-type(1),
            [data-testid="column"]:nth-of-type(1) { border-right: 2px solid #e6e6e6 !important; padding-right: 2rem !important; }
            div[data-testid="stColumn"]:nth-of-type(2),
            [data-testid="column"]:nth-of-type(2) {
                position: -webkit-sticky !important; position: sticky !important; top: 4rem !important;
                align-self: flex-start !important; max-height: calc(100vh - 4rem) !important;
                overflow-y: auto !important; z-index: 100 !important; padding-left: 1rem !important;
            }
            div[data-testid="column"]:nth-of-type(1) div[data-testid="stHorizontalBlock"] { margin-bottom: -15px !important; align-items: center !important; }
            ::-webkit-scrollbar { width: 6px; }
            ::-webkit-scrollbar-thumb { background-color: #cccccc; border-radius: 4px; }
        </style>
    """, unsafe_allow_html=True)

    # ------------------------------------------
    # 🖼️ [V9.9 Upgrade] 텍스트 이모지 -> PNG 로고 교체 로직
    # ------------------------------------------
    LOGO_PATH = "newsprismdog.png"
    
    if os.path.exists(LOGO_PATH):
        import base64
        with open(LOGO_PATH, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
            # 이미지 높이(height)를 55px로 설정하여 제목 텍스트와 크기를 맞췄습니다.
            st.markdown(
                f"""
                <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 10px;">
                    <img src="data:image/png;base64,{data}" style="height: 200px; border-radius: 8px;">
                    <h1 style="margin: 0; padding: 0; line-height: 1.2;"> 가나디: 신문배달 와써여 - V9.9 Masterpiece</h1>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        # 파일이 없을 경우 기존 텍스트 타이틀로 백업
        st.title("💎 가나디의 신문배달 - V9.9 Masterpiece")
        st.info(f"💡 '{LOGO_PATH}' 파일을 찾을 수 없습니다. 이미지를 깃허브에 업로드해 주세요.")

    st.markdown("##### 🚀 top10 섹션 헤드라인 + 📺 유튜브 주요 채널들 ")
    st.write("---")

    # ==========================================
    # ⚙️ 상태 관리 (✅ FIX #3: 각 키 독립 초기화 → KeyError 방지)
    # ==========================================
    if 'view_mode' not in st.session_state:
        st.session_state.view_mode = 'news'
    if 'yt_data' not in st.session_state:
        st.session_state.yt_data = []
    if 'yt_channel_name' not in st.session_state:
        st.session_state.yt_channel_name = ""
    if 'market_data' not in st.session_state:
        m_data, f_data, n_map = load_session_from_disk()
        st.session_state.market_data = m_data
        st.session_state.filtered_data = f_data
        st.session_state.news_map = n_map
    if 'filtered_data' not in st.session_state:
        st.session_state.filtered_data = {}
    if 'news_map' not in st.session_state:
        st.session_state.news_map = {}
    if 'selected_id' not in st.session_state:
        st.session_state.selected_id = None
    if 'summaries' not in st.session_state:
        st.session_state.summaries = {}
    if 'final_time_str' not in st.session_state:
        st.session_state.final_time_str = None

    # ==========================================
    # ⚙️ 사이드바 제어판
    # ==========================================
    st.sidebar.header("⚙️ 고급 설정")
    user_interest = st.sidebar.text_area(
        "커스터마이징 기본값",
        value="거시 경제 흐름, 미국 증시, 그리고 AI와 반도체 산업 변화에 특히 관심이 많음.",
        height=100
    )

    with st.sidebar.expander("🎯 10대 섹션별 키워드 타겟팅", expanded=False):
        t1  = st.text_input("① 국내 대장주",    placeholder="예: LG에너지솔루션")
        t2  = st.text_input("② 글로벌 빅테크",  placeholder="예: 메타")
        t3  = st.text_input("③ 기업 실적·공시", placeholder="예: 배당락")
        t4  = st.text_input("④ 미래 첨단산업",  placeholder="예: 전고체")
        t5  = st.text_input("⑤ 거시경제 지표",  placeholder="예: 실업률")
        t6  = st.text_input("⑥ 부동산·원자재",  placeholder="예: 구리")
        t7  = st.text_input("⑦ 국내 증시·시황", placeholder="예: 공매도")
        t8  = st.text_input("⑧ 해외 증시·자산", placeholder="예: 이더리움")
        t9  = st.text_input("⑨ 정부 정책·규제", placeholder="예: 금투세")
        t10 = st.text_input("⑩ 글로벌 지정학",  placeholder="예: 이스라엘")

    target_keywords = {
        "국내 대장주":   t1.strip(),  "글로벌 빅테크":  t2.strip(),
        "기업 실적·공시": t3.strip(), "미래 첨단산업":  t4.strip(),
        "거시경제 지표":  t5.strip(), "부동산·원자재":  t6.strip(),
        "국내 증시·시황": t7.strip(), "해외 증시·자산": t8.strip(),
        "정부 정책·규제": t9.strip(), "글로벌 지정학":  t10.strip()
    }

    col_run, col_stop = st.sidebar.columns(2)
    # ✅ FIX #1: width="stretch" → use_container_width=True (전체 동일하게 적용)
    run_news_btn = col_run.button("🚀/n뉴스 가동", type="primary", use_container_width=True)
    stop_news_btn = col_stop.button("🛑 정지", use_container_width=True)

    if stop_news_btn:
        st.sidebar.error("🚫 브리핑 가동이 취소되었습니다.")
        st.stop()

    st.sidebar.markdown("---")

    st.sidebar.header("📺 유튜브 프리즘 (영상)")
    yt_channels = {
        "오선의 미국증시 라이브": "UC_JJ_NhRqPKcIOj5Ko3W_3w",
        "이효석 아카데미":        "UCxvdCnvGODDyuvnELnLkQWw",
        "내일은 투자왕 김단테":   "UCKTMvIu9a4VGSrpWy-8bUrQ",
        "센서스튜디오":           "UC6dN6Rilzh9KmzymxnZGslg"
    }

    for ch_name, ch_id in yt_channels.items():
        # ✅ FIX #1 (계속): 유튜브 채널 버튼도 동일하게 수정
        if st.sidebar.button(f"▶️ {ch_name}", use_container_width=True):
            st.session_state.view_mode = 'youtube'
            st.session_state.yt_channel_name = ch_name
            with st.spinner(f"📡 '{ch_name}' 채널의 최근 15시간 영상을 스캔합니다..."):
                st.session_state.yt_data = fetch_youtube_videos_15h(ch_id)

    # ==========================================
    # 🚀 뉴스 가동 로직 (점진적 렌더링)
    # ==========================================
    if run_news_btn:
        st.session_state.view_mode = 'news'
        st.session_state.filtered_data = {"분류결과": {}}
        st.session_state.news_map = {}
        st.session_state.selected_id = None

        start_time = time.time()

        timer_placeholder = st.empty()
        with timer_placeholder:
            components.html(
                """
                <div style="font-family: 'Segoe UI', sans-serif; font-size: 16px; font-weight: 500; color: #0f5132; background-color: #d1e7dd; padding: 20px; border-radius: 8px; border: 1px solid #badbcc; text-align: center; margin-bottom: 10px;">
                    🚀 <b>뉴스프리즘 엔진 순차 렌더링 가동 중...</b> <br><br>
                    ⏱️ 소요시간: <span id="time" style="font-weight: 700; font-size: 20px;">00분 00초</span>
                </div>
                <script>
                    var start = Date.now();
                    setInterval(function() {
                        var delta = Math.floor((Date.now() - start) / 1000);
                        var m = Math.floor(delta / 60).toString().padStart(2, '0');
                        var s = (delta % 60).toString().padStart(2, '0');
                        document.getElementById('time').innerText = m + '분 ' + s + '초';
                    }, 1000);
                </script>
                """, height=120
            )

        ui_status_text = st.empty()
        ui_progress_bar = st.progress(0)

        st.markdown("### 📋 오늘의 텍스트 브리핑 (실시간 로딩 중... ⏳)")
        market_ph = st.empty()
        market_ph.info("📈 글로벌 마켓 지표를 스캔하고 있습니다...")

        sectors_keys = list(target_keywords.keys())
        sector_containers = {sec: st.empty() for sec in sectors_keys}

        # 0. 시장 지표 스캔
        st.session_state.market_data = get_market_indicators()
        market_str = " | ".join([f"{k}: {v}" for k, v in st.session_state.market_data.items()])
        market_ph.success(f"**[시장 지표]** {market_str}")

        # ✅ FIX #4: current_article_idx는 반드시 버튼 블록 내부에서만 초기화 (현재 위치 OK)
        current_article_idx = 1
        default_keywords = {
            "국내 대장주":   "삼성전자 OR SK하이닉스 OR 현대차 OR 에코프로 OR 금융주",
            "글로벌 빅테크":  "엔비디아 OR 애플 OR 테슬라 OR 마이크로소프트 OR TSMC",
            "기업 실적·공시": "어닝 OR 실적발표 OR 주주환원 OR M&A OR 자사주",
            "미래 첨단산업":  "AI OR 반도체 OR 2차전지 OR 자율주행 OR 로봇",
            "거시경제 지표":  "기준금리 OR 인플레이션 OR 환율 OR CPI OR 고용지표",
            "부동산·원자재":  "부동산 시장 OR PF OR 국제유가 OR 금 가격 OR 원자재",
            "국내 증시·시황": "코스피 OR 코스닥 OR 상법개정 OR 수급 OR 환율",
            "해외 증시·자산": "나스닥 OR S&P500 OR 연준 OR 국채 OR 비트코인",
            "정부 정책·규제": "상법개정 OR 정부 정책 OR 세제개편 OR 세제혜택",
            "글로벌 지정학":  "미중 OR 관세 OR 중동 OR 트럼프 OR 공급망"
        }

        for idx, sector_name in enumerate(sectors_keys):
            target_kw = target_keywords.get(sector_name, "")
            search_query = target_kw if target_kw else default_keywords[sector_name]

            ui_status_text.markdown(f"**🔍 [{sector_name}] 데이터 수집 및 AI 정제 중... ({idx+1}/10)**")
            ui_progress_bar.progress(int((idx / 10) * 100))

            raw_context, local_map, current_article_idx = fetch_single_sector_news(sector_name, search_query, current_article_idx)
            curated_list = apply_prism_lens_single(sector_name, raw_context, user_interest, search_query)

            st.session_state.news_map.update(local_map)
            st.session_state.filtered_data["분류결과"][sector_name] = curated_list

            with sector_containers[sector_name].container():
                if curated_list:
                    st.markdown(f"#### ✅ [{sector_name}]")
                    for item in curated_list:
                        title = item.get('title', '제목 없음') if isinstance(item, dict) else item
                        st.markdown(f"• {title}")
                    st.write("---")

        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        st.session_state.final_time_str = f"{mins:02d}분 {secs:02d}초"

        ui_progress_bar.progress(100)
        ui_status_text.markdown("✨ **모든 브리핑 조립이 완료되었습니다! (최종 데이터 저장 중...)**")

        save_session_to_disk(st.session_state.market_data, st.session_state.filtered_data, st.session_state.news_map)
        time.sleep(1)
        st.rerun()

    # ==========================================
    # 🖥️ 화면 렌더링 분기 (정식 뷰어 모드)
    # ==========================================
    if st.session_state.view_mode == 'news':
        if not st.session_state.filtered_data.get("분류결과"):
            st.info("👈 왼쪽 사이드바에서 '뉴스 가동' 버튼을 눌러주세요.")
        else:
            col1, col2 = st.columns([1, 1])
            with col1:
                st.markdown("### 📋 오늘의 텍스트 브리핑 (완료)")
                st.success("🎯 기사 우측의 [내용보기] 버튼을 누르면 통계 중심 요약창이 뜹니다.")
                if st.session_state.get('final_time_str'):
                    st.markdown(f"**⏱️ 브리핑 조립 소요 시간:** `{st.session_state.final_time_str}`")

                st.markdown(f"**개장전★주요이슈 점검 ({datetime.now().strftime('%Y/%m/%d')})**")

                st.markdown("**[시장 지표]**")
                for k, v in st.session_state.market_data.items():
                    st.markdown(f"* {k}: {v}")
                st.write("---")

                categories_data = st.session_state.filtered_data.get("분류결과", {})
                for category, items in categories_data.items():
                    if not items:
                        continue
                    st.markdown(f"#### [{category}]")
                    for idx, item in enumerate(items):
                        title = item.get('title', '제목 없음') if isinstance(item, dict) else item
                        item_id = item.get('id') if isinstance(item, dict) else None

                        if not item_id:
                            for k, v in st.session_state.news_map.items():
                                if v['title'] == title or title in v['title']:
                                    item_id = k
                                    break
                            if not item_id:
                                item_id = f"fallback_{category}_{idx}"

                        title = re.sub(r'^\[.*?\]\s*', '', title).strip()

                        c_text, c_btn = st.columns([8.5, 1.5])
                        c_text.markdown(f"• {title}")
                        # ✅ FIX #1 (계속): 내용보기 버튼도 동일하게 수정
                        if c_btn.button("내용보기", key=f"btn_{category}_{idx}_{item_id}", use_container_width=True):
                            st.session_state.selected_id = item_id
                    st.write("")

            with col2:
                st.markdown("### 🤖 통계 & 데이터 기반 3문단 요약")
                if st.session_state.selected_id and st.session_state.selected_id in st.session_state.news_map:
                    selected_info = st.session_state.news_map[st.session_state.selected_id]
                    title   = selected_info['title']
                    snippet = selected_info['snippet']
                    url     = selected_info['url']

                    st.markdown(f"**🔗 [웹 브라우저에서 원본 기사 따로 열기]({url})**")
                    st.markdown(f"**📰 기사 제목:** {title}")
                    st.markdown("---")

                    if st.session_state.selected_id not in st.session_state.summaries:
                        with st.spinner("헤드라인에서 숫자와 통계를 뽑아내어 3문단 요약 중입니다..."):
                            summary_text = generate_headline_data_summary(title, snippet)
                            st.session_state.summaries[st.session_state.selected_id] = summary_text

                    st.info("💡 **헤드라인 기반 데이터 추출 결과**")
                    st.write(st.session_state.summaries[st.session_state.selected_id])
                else:
                    st.markdown('''
                        <div style="padding: 2rem; background-color: #f8f9fa; border-radius: 10px; text-align: center; color: #6c757d;">
                            👈 왼쪽 브리핑 보드에서 <b>[내용보기]</b> 버튼을 클릭해 보세요.<br>
                            Gemini가 <b>"통계", "인용구", "숫자", "데이터"</b>를 포함하여 3문단으로 요약해 드립니다!
                        </div>
                    ''', unsafe_allow_html=True)

    elif st.session_state.view_mode == 'youtube':
        st.markdown(f"### 📺 **{st.session_state.yt_channel_name}** - 최근 15시간 업로드 영상")

        if not st.session_state.yt_data:
            st.warning(f"이런! 15시간 이내에 '{st.session_state.yt_channel_name}' 채널에 새로 올라온 영상이 없습니다.")
        else:
            for item in st.session_state.yt_data:
                # ✅ FIX #5: videoId 안전하게 추출 → KeyError 방지
                video_id = item.get('id', {}).get('videoId')
                if not video_id:
                    continue  # videoId 없는 항목은 건너뜀

                snippet      = item['snippet']
                title        = sanitize_text(snippet['title'])
                published_at = snippet['publishedAt']
                thumb_url    = snippet['thumbnails']['high']['url']
                video_url    = f"https://www.youtube.com/watch?v={video_id}"

                dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('Asia/Seoul'))
                pretty_time = dt.strftime("%Y년 %m월 %d일 %H시 %M분")

                with st.container():
                    col_img, col_info = st.columns([3, 7])

                    with col_img:
                        st.image(thumb_url)

                    with col_info:
                        st.markdown(f"#### [{title}]({video_url})")
                        st.markdown(f"🗓️ **업로드:** {pretty_time}")

                        # ✅ FIX #1 (계속): 유튜브 요약 버튼도 동일하게 수정
                        if st.button("🧠 영상 내용 프리즘 요약하기", key=f"yt_{video_id}", use_container_width=True):
                            with st.spinner("뉴스프리즘 엔진이 유튜브 데이터를 해독 중입니다. (약 5~10초 소요)"):
                                yt_summary = extract_transcript_and_summarize(video_id, title, snippet['description'])
                                st.success("🎯 **AI 영상 핵심 요약 완료!**")
                                st.write(yt_summary)
                st.write("---")

if __name__ == "__main__":
    main()





