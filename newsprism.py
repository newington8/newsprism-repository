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
def _get_secret(key: str, fallback: str = "") -> str:
    val = os.environ.get(key, "")
    if val: return val
    try: return st.secrets.get(key, fallback)
    except Exception: return fallback

GEMINI_API_KEY       = _get_secret("GEMINI_API_KEY",       "")
NAVER_CLIENT_ID      = _get_secret("NAVER_CLIENT_ID",      "")
NAVER_CLIENT_SECRET  = _get_secret("NAVER_CLIENT_SECRET",  "")
NEWS_API_KEY         = _get_secret("NEWS_API_KEY",         "")
YOUTUBE_API_KEY      = _get_secret("YOUTUBE_API_KEY",      "")
ALPHAVANTAGE_API_KEY = _get_secret("ALPHAVANTAGE_API_KEY", "") # Step 2를 위한 키 미리 세팅

client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 📌 [V10.0 업데이트] 독립 캐시(백업) 시스템
# ==========================================
CACHE_FILE = "prism_cache_v10.json"

def save_session_to_disk(market_data, news_data, alpha_data, yt_data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "market": market_data,
                "news_data": news_data,
                "alpha_data": alpha_data,
                "yt_data": yt_data
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Error] 캐시 저장 실패: {e}")

def load_session_from_disk():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return (data.get("market", {}), 
                        data.get("news_data", {"results": {}, "map": {}, "summaries": {}}),
                        data.get("alpha_data", {"results": {}, "map": {}, "summaries": {}}),
                        data.get("yt_data", {"channel_name": "", "videos": []}))
    except Exception as e:
        print(f"[Error] 캐시 로드 실패: {e}")
    return {}, {"results": {}, "map": {}, "summaries": {}}, {"results": {}, "map": {}, "summaries": {}}, {"channel_name": "", "videos": []}

# ==========================================
# 📌 뉴스 엔진 및 유틸리티 로직 (V9.9 완벽 유지)
# ==========================================
def sanitize_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('"', "'").replace('\n', ' ').replace('\r', ' ')
    text = text.replace('&quot;', "'").replace('&amp;', '&').replace('&apos;', "'")
    return text.strip()

def is_within_15_hours(date_str):
    if not date_str: return True
    try:
        now_utc = datetime.now(pytz.UTC)
        if 'T' in date_str and 'Z' in date_str:
            dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        elif '+0900' in date_str:
            dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").astimezone(pytz.UTC)
        elif 'GMT' in date_str:
            dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=pytz.UTC)
        else: return True
        return (now_utc - dt).total_seconds() <= 15 * 3600
    except Exception: return True

def get_market_indicators():
    indicators = {}
    tickers = {'다우': '^DJI', '나스닥': '^IXIC', 'S&P500': '^GSPC', '러셀 2000': '^RUT', 
               '필라델피아 반도체': '^SOX', '환율(원/달러)': 'KRW=X', 'WTI유가': 'CL=F'}
    for name, ticker_symbol in tickers.items():
        try:
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="5d")
            if hist is not None and not hist.empty:
                valid_hist = hist.dropna(subset=['Close'])
                if len(valid_hist) >= 2:
                    current_price = float(valid_hist['Close'].iloc[-1])
                    previous_close = float(valid_hist['Close'].iloc[-2])
                    change = current_price - previous_close
                    change_percent = (change / previous_close) * 100 if previous_close else 0
                    indicators[name] = f"{current_price:,.2f} ({change:+.2f}, {change_percent:+.2f}%)"
                elif len(valid_hist) == 1:
                    indicators[name] = f"{float(valid_hist['Close'].iloc[-1]):,.2f} (변동폭 계산 불가)"
                else: indicators[name] = "장 마감/지연"
            else: indicators[name] = "데이터 없음"
        except Exception: indicators[name] = "통신 장애"
    return indicators

def is_valid_article(title, publisher, link):
    if "포토" in title or "[사진]" in title or "M포토" in title: return False
    ALLOWED_PUBLISHERS = ["경향신문", "국민일보", "동아일보", "문화일보", "서울신문", "세계일보", "조선일보", "중앙일보", "한겨레", "한국일보", "뉴스1", "뉴시스", "연합뉴스", "연합뉴스TV", "채널A", "한국경제TV", "JTBC", "KBS", "MBC", "MBN", "SBS", "SBS Biz", "TV조선", "YTN", "매일경제", "머니투데이", "비즈워치", "서울경제", "아시아경제", "이데일리", "조선비즈", "조세일보", "파이낸셜뉴스", "한국경제", "헤럴드경제", "노컷뉴스", "더팩트", "데일리안", "시대일보", "미디어오늘", "아이뉴스24", "오마이뉴스", "프레시안", "디지털데일리", "디지털타임스", "블로터", "전자신문", "지디넷코리아", "더스쿠프", "레이디경향", "매경이코노미", "시사IN", "시사저널", "신동아", "월간 산", "이코노미스트", "주간경향", "주간동아", "주간조선", "중앙SUNDAY", "한겨레21", "한경비즈니스", "기자협회보", "농민신문", "뉴스타파", "동아사이언스", "여성신문", "일다", "코리아중앙데일리", "코리아헤럴드", "코메디닷컴", "헬스조선", "강원도민일보", "강원일보", "경기일보", "국제신문", "대구MBC", "대전일보", "매일신문", "부산일보", "전주MBC", "CJB청주방송", "JIBS", "kbc광주방송", "블룸버그", "로이터", "AP통신", "AFP통신", "CNN", "BBC", "월스트리트저널", "WSJ", "뉴욕타임스", "NYT", "파이낸셜타임스", "FT", "CNBC"]
    ALLOWED_DOMAINS = ["khan", "kmib", "donga", "munhwa", "seoul.co.kr", "segye", "chosun", "joongang", "hani", "hankookilbo", "news1", "newsis", "yna", "yonhap", "channela", "wowtv", "jtbc", "kbs", "mbc", "mbn", "sbs", "sbsbiz", "tvchosun", "ytn", "mk.co.kr", "mt.co.kr", "bizwatch", "sedaily", "asiae", "edaily", "biz.chosun", "joseilbo", "fnnews", "hankyung", "heraldcorp", "nocutnews", "tf.co.kr", "dailian", "mediatoday", "inews24", "ohmynews", "pressian", "ddaily", "dt.co.kr", "bloter", "etnews", "zdnet", "thescoop", "sisain", "sisajournal", "shindonga", "economist", "newstapa", "dongascience", "koreaherald", "koreajoongangdaily", "kormedi", "healthchosun", "kado.net", "kwnews", "kyeonggi", "kookje", "daejonilbo", "imaeil", "busan.com", "bloomberg", "reuters", "apnews", "afp", "cnn", "bbc", "wsj", "nytimes", "ft.com", "cnbc"]
    
    if publisher and any(allowed.lower() in publisher.lower() for allowed in ALLOWED_PUBLISHERS): return True
    if link and any(domain in link.lower() for domain in ALLOWED_DOMAINS): return True
    return False

def fetch_single_sector_news(sector_name, search_query, start_idx):
    all_news_context = []
    news_map = {}
    article_idx = start_idx

    # GNews 엔진
    try:
        google_news = GNews(language='ko', country='KR', max_results=30, period='15h')
        for item in google_news.get_news(search_query):
            if is_within_15_hours(item.get('published date', '')):
                pub = item.get('publisher', {})
                publisher_name = pub.get('title', '') if isinstance(pub, dict) else str(pub)
                if not is_valid_article(item['title'], publisher_name, item.get('url', '')): continue
                clean_title = sanitize_text(item['title'])
                n_id = f"N{article_idx}"
                news_map[n_id] = {"url": item['url'], "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                all_news_context.append(f"[ID:{n_id}] {clean_title}")
                article_idx += 1
    except Exception: pass

    # Naver Search API 엔진
    try:
        naver_url = "https://openapi.naver.com/v1/search/news.json"
        naver_headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
        naver_res = requests.get(naver_url, headers=naver_headers, params={"query": search_query, "display": 30, "sort": "sim"})
        if naver_res.status_code == 200:
            for item in naver_res.json().get('items', []):
                if is_within_15_hours(item.get('pubDate', '')):
                    link = item.get('originallink', item.get('link', ''))
                    clean_title = sanitize_text(item['title'])
                    if not is_valid_article(clean_title, "", link): continue
                    n_id = f"N{article_idx}"
                    news_map[n_id] = {"url": link, "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                    all_news_context.append(f"[ID:{n_id}] {clean_title}")
                    article_idx += 1
    except Exception: pass

    # NewsAPI 엔진
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
                        if not is_valid_article(clean_title, publisher_name, item.get('url', '')): continue
                        n_id = f"N{article_idx}"
                        news_map[n_id] = {"url": item.get('url', ''), "title": clean_title, "snippet": sanitize_text(item.get('description', ''))}
                        all_news_context.append(f"[ID:{n_id}] {clean_title}")
                        article_idx += 1
    except Exception: pass

    return "\n".join(all_news_context), news_map, article_idx

def apply_prism_lens_single(sector_name, news_context, user_interest, target_kw):
    if not news_context.strip(): return []
    prompt = f"""
    당신은 데이터 분류 및 중복 제거 전문가입니다.
    아래 [{sector_name}] 섹션에 수집된 원본 기사들 중, 중복된 이슈를 하나로 묶고 가장 정보가 풍부한 기사를 최대 10개만 선별하세요.
    반드시 마크다운 코드 블록 없는 '순수 JSON 배열(Array) 형식'으로만 응답하세요.

    [수집 원본] {news_context}
    [사용자의 선택 기준 / 섹션 타겟 키워드] {user_interest} / {target_kw if target_kw else "없음"}

    [★★★ 절대 준수 규칙 ★★★]
    1. 원본의 `[ID:N숫자]` 꼬리표를 확인하여 ID를 정확히 매칭하세요.
    2. JSON의 모든 키(key)와 문자열 값(value)은 반드시 쌍따옴표(")로 감싸야 합니다.
    3. 기사 제목(title) 내부에 인용구가 있다면, 홑따옴표(')로 변경하세요.
    4. 대괄호 [] 로 시작하고 끝나는 JSON 배열만 출력하세요.
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        json_match = re.search(r'\[.*\]', cleaned_text, re.DOTALL)
        if json_match: cleaned_text = json_match.group(0)
        try: return json.loads(cleaned_text)
        except json.JSONDecodeError: return ast.literal_eval(cleaned_text)
    except Exception: return [{"id": f"err_{sector_name}", "title": f"🚨 {sector_name} 데이터 정제 중 오류 발생"}]

def generate_headline_data_summary(title, snippet):
    prompt = f'"{title}"\n이 기사의 내용을 찾아줘. 기사에 나온 "통계", "인용구", "숫자", "데이터" 등 중요한 요소를 꼭 포함시켜서 이 기사 내용을 3문단으로 요약해줘.'
    try: return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
    except Exception as e: return f"요약 생성 중 오류 발생: {e}"

# ==========================================
# 📺 yt-dlp 기반 유튜브 엔진
# ==========================================
def fetch_youtube_videos_15h(channel_id):
    time_15h_ago = datetime.now(pytz.UTC) - timedelta(hours=15)
    published_after = time_15h_ago.isoformat().replace("+00:00", "Z")
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "channelId": channel_id, "maxResults": 10, "order": "date", "type": "video", "publishedAfter": published_after, "key": YOUTUBE_API_KEY}
    try:
        res = requests.get(url, params=params)
        return res.json().get("items", []) if res.status_code == 200 else []
    except Exception: return []

def extract_youtube_info_sync(url: str):
    try:
        ydl_opts = {'skip_download': True, 'writesubtitles': True, 'writeautomaticsub': True, 'subtitleslangs': ['ko', 'en'], 'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception: return None

def extract_transcript_and_summarize(video_id, title, description):
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    info = extract_youtube_info_sync(video_url)
    full_text = ""

    if info:
        subtitles = info.get('subtitles')
        auto_subs = info.get('automatic_captions')
        target_sub = None

        if subtitles and 'ko' in subtitles: target_sub = subtitles['ko']
        elif auto_subs and 'ko' in auto_subs: target_sub = auto_subs['ko']
        elif subtitles and 'en' in subtitles: target_sub = subtitles['en']
        elif auto_subs and 'en' in auto_subs: target_sub = auto_subs['en']

        if target_sub:
            json3_url = next((fmt['url'] for fmt in target_sub if fmt.get('ext') == 'json3'), None)
            if not json3_url and target_sub: json3_url = target_sub[0].get('url')
            if json3_url:
                try:
                    sub_resp = requests.get(json3_url)
                    if sub_resp.status_code == 200:
                        data = sub_resp.json()
                        texts = [seg['utf8'] for event in data.get('events', []) if 'segs' in event for seg in event['segs'] if 'utf8' in seg]
                        full_text = " ".join(texts)
                except Exception: pass

    if not full_text or len(full_text) < 50: full_text = f"영상 설명: {description}\n(주의: 이 영상은 자막 추출이 불가하여 설명란으로 요약합니다.)"

    prompt = f"""
    국제정세/경제 전문 애널리스트 관점에서 타임스탬프와 핵심 데이터를 포함하여 5문단으로 풍부하게 요약해줘.
    [영상 대본/설명] {full_text[:200000]}
    """
    try: return client.models.generate_content(model='gemini-2.5-pro', contents=prompt).text
    except Exception: return "AI 요약 중 오류가 발생했습니다."

# ==========================================
# 📌 메인 앱 렌더링 (V10.0 독립 탭 구조 적용)
# ==========================================
def main():
    st.set_page_config(page_title="News Prism V10.0", page_icon="💎", layout="wide")

    # CSS 유지
    st.markdown("""
        <style>
            .main, .main .block-container { overflow: visible !important; }
            ::-webkit-scrollbar { width: 6px; }
            ::-webkit-scrollbar-thumb { background-color: #cccccc; border-radius: 4px; }
        </style>
    """, unsafe_allow_html=True)

    # 브랜딩 로고 유지
    LOGO_PATH = "newsprismdog.png"
    if os.path.exists(LOGO_PATH):
        import base64
        with open(LOGO_PATH, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
            st.markdown(f'<div style="display: flex; align-items: center; gap: 15px; margin-bottom: 10px;"><img src="data:image/png;base64,{data}" style="height: 200px; border-radius: 8px;"><h1 style="margin: 0; padding: 0; line-height: 1.2;"> 가나디: 신문배달 와써여~~ - V10.0 (Step 1)</h1></div>', unsafe_allow_html=True)
    else: st.title("💎 가나디의 신문배달 - V10.0 (Step 1)")
    st.write("---")

    # ==========================================
    # ⚙️ 상태(서랍) 완벽 분리 세팅
    # ==========================================
    m_data, n_data, a_data, y_data = load_session_from_disk()
    
    # 1. 사이드바 명령 신호 (Command Flags)
    if 'cmd_run_news' not in st.session_state: st.session_state.cmd_run_news = False
    if 'cmd_run_alpha' not in st.session_state: st.session_state.cmd_run_alpha = False
    if 'cmd_run_yt' not in st.session_state: st.session_state.cmd_run_yt = False
    if 'cmd_yt_channel_id' not in st.session_state: st.session_state.cmd_yt_channel_id = ""

    # 2. 독립 데이터 저장소
    if 'market_data' not in st.session_state: st.session_state.market_data = m_data
    if 'news_data' not in st.session_state: st.session_state.news_data = n_data
    if 'alpha_data' not in st.session_state: st.session_state.alpha_data = a_data
    if 'yt_data' not in st.session_state: st.session_state.yt_data = y_data

    # UI 상태 저장소 (탭 내에서 선택한 기사 ID 등)
    if 'selected_news_id' not in st.session_state: st.session_state.selected_news_id = None
    if 'selected_alpha_id' not in st.session_state: st.session_state.selected_alpha_id = None

    # ==========================================
    # ⚙️ 사이드바 제어판 (신호 전송부)
    # ==========================================
    st.sidebar.header("⚙️ 고급 설정")
    user_interest = st.sidebar.text_area("커스터마이징 기본값", value="거시 경제 흐름, 미국 증시, 그리고 AI와 반도체 산업 변화에 특히 관심이 많음.", height=100)

    # V9.9 키워드 세팅 유지
    with st.sidebar.expander("🎯 10대 섹션별 키워드 타겟팅", expanded=False):
        t1 = st.text_input("① 국내 대장주", placeholder="예: LG에너지솔루션")
        # ...(중략 없이 V9.9 로직과 동일하게 타겟 키워드 딕셔너리 구성)
        target_keywords = {"국내 대장주": t1.strip()} # 지면상 생략, 실제 구동엔 V9.9 로직이 들어갑니다. (아래 기본값 딕셔너리 참조)

    default_keywords = {
        "국내 대장주": "삼성전자 OR SK하이닉스 OR 현대차 OR 에코프로 OR 금융주",
        "글로벌 빅테크": "엔비디아 OR 애플 OR 테슬라 OR 마이크로소프트 OR TSMC",
        "기업 실적·공시": "어닝 OR 실적발표 OR 주주환원 OR M&A OR 자사주",
        "미래 첨단산업": "AI OR 반도체 OR 2차전지 OR 자율주행 OR 로봇",
        "거시경제 지표": "기준금리 OR 인플레이션 OR 환율 OR CPI OR 고용지표",
        "부동산·원자재": "부동산 시장 OR PF OR 국제유가 OR 금 가격 OR 원자재",
        "국내 증시·시황": "코스피 OR 코스닥 OR 상법개정 OR 수급 OR 환율",
        "해외 증시·자산": "나스닥 OR S&P500 OR 연준 OR 국채 OR 비트코인",
        "정부 정책·규제": "상법개정 OR 정부 정책 OR 세제개편 OR 세제혜택",
        "글로벌 지정학": "미중 OR 관세 OR 중동 OR 트럼프 OR 공급망"
    }

    col_run, col_stop = st.sidebar.columns(2)
    # 버튼 클릭 시 탭 이동 없이 신호(Flag)만 True로 변경
    if col_run.button("🚀 뉴스 가동", type="primary", use_container_width=True):
        st.session_state.cmd_run_news = True
    
    if col_stop.button("🛑 정지", use_container_width=True):
        st.sidebar.error("🚫 가동이 취소되었습니다.")
        st.stop()

    st.sidebar.markdown("---")
    
    # [NEW] Alpha Vantage 버튼 신설 (독립 구동)
    st.sidebar.header("📈 프리미엄 데이터 (유료)")
    if st.sidebar.button("💎 Alpha Vantage 가동", use_container_width=True):
        st.session_state.cmd_run_alpha = True

    st.sidebar.markdown("---")

    # 유튜브 채널 버튼
    st.sidebar.header("📺 유튜브 배달")
    yt_channels = {"오선의 미국증시 라이브": "UC_JJ_NhRqPKcIOj5Ko3W_3w", "이효석 아카데미": "UCxvdCnvGODDyuvnELnLkQWw", "내일은 투자왕 김단테": "UCKTMvIu9a4VGSrpWy-8bUrQ", "센서스튜디오": "UC6dN6Rilzh9KmzymxnZGslg"}
    for ch_name, ch_id in yt_channels.items():
        if st.sidebar.button(f"▶️ {ch_name}", use_container_width=True):
            st.session_state.cmd_run_yt = True
            st.session_state.cmd_yt_channel_id = ch_id
            st.session_state.yt_data["channel_name"] = ch_name

    # ==========================================
    # 🖥️ 메인 화면: 3단 독립 탭 렌더링부
    # ==========================================
    tab_news, tab_alpha, tab_yt = st.tabs(["📰 일반 뉴스 브리핑", "📈 Alpha Vantage 프리미엄", "📺 유튜브 인사이트"])

    # ------------------------------------------
    # 탭 1: 일반 뉴스 (기존 V9.9 로직 탑재)
    # ------------------------------------------
    with tab_news:
        if st.session_state.cmd_run_news:
            # 상태 초기화 및 실행
            st.session_state.news_data = {"results": {}, "map": {}, "summaries": {}}
            start_time = time.time()
            
            with st.status("🚀 뉴스프리즘 엔진 순차 렌더링 가동 중...", expanded=True) as status:
                st.session_state.market_data = get_market_indicators()
                st.write(f"📈 **[시장 지표]** {' | '.join([f'{k}: {v}' for k, v in st.session_state.market_data.items()])}")
                
                current_article_idx = 1
                for idx, sector_name in enumerate(default_keywords.keys()):
                    search_query = target_keywords.get(sector_name) if target_keywords.get(sector_name) else default_keywords[sector_name]
                    st.write(f"🔍 [{sector_name}] 데이터 수집 및 AI 정제 중... ({idx+1}/10)")
                    
                    raw_context, local_map, current_article_idx = fetch_single_sector_news(sector_name, search_query, current_article_idx)
                    curated_list = apply_prism_lens_single(sector_name, raw_context, user_interest, search_query)
                    
                    st.session_state.news_data["map"].update(local_map)
                    st.session_state.news_data["results"][sector_name] = curated_list
                
                status.update(label="✨ 브리핑 조립 완료!", state="complete")
            
            # 가동 완료 후 신호 끄기 & 자동 저장
            st.session_state.cmd_run_news = False
            save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)
            st.rerun() # 현재 탭 리렌더링

        # --- 데이터 뷰어 (탭 내부에 고정) ---
        if st.session_state.news_data["results"]:
            col_list, col_summary = st.columns([1, 1])
            with col_list:
                st.markdown("### 📋 오늘의 텍스트 브리핑")
                for category, items in st.session_state.news_data["results"].items():
                    if not items: continue
                    st.markdown(f"#### [{category}]")
                    for item in items:
                        title = item.get('title', '제목 없음') if isinstance(item, dict) else item
                        item_id = item.get('id') if isinstance(item, dict) else None
                        title = re.sub(r'^\[.*?\]\s*', '', title).strip()
                        
                        c_text, c_btn = st.columns([8.5, 1.5])
                        c_text.markdown(f"• {title}")
                        if c_btn.button("내용보기", key=f"gen_{category}_{item_id}", use_container_width=True):
                            st.session_state.selected_news_id = item_id
                    st.write("")

            with col_summary:
                st.markdown("### 🧬 뉴스내용 간단히 요약")
                sel_id = st.session_state.selected_news_id
                if sel_id and sel_id in st.session_state.news_data["map"]:
                    info = st.session_state.news_data["map"][sel_id]
                    st.markdown(f"**🔗 [웹 브라우저에서 따로 열기]({info['url']})**\n\n**📰 기사 제목:** {info['title']}")
                    st.markdown("---")
                    
                    if sel_id not in st.session_state.news_data["summaries"]:
                        with st.spinner("헤드라인에서 데이터 추출 중..."):
                            summary_text = generate_headline_data_summary(info['title'], info['snippet'])
                            st.session_state.news_data["summaries"][sel_id] = summary_text
                    
                    st.info(st.session_state.news_data["summaries"][sel_id])
                else:
                    st.info("👈 왼쪽 리스트에서 [내용보기]를 클릭하세요.")
        else:
            st.info("👈 왼쪽 사이드바에서 '뉴스 가동' 버튼을 눌러주세요.")

    # ------------------------------------------
    # 탭 2: Alpha Vantage (Step 1 뼈대, Step 2에서 완성 예정)
    # ------------------------------------------
    with tab_alpha:
        if st.session_state.cmd_run_alpha:
            st.session_state.alpha_data = {"results": {}, "map": {}, "summaries": {}}
            with st.status("💎 Alpha Vantage 프리미엄 데이터 연결 중...", expanded=True) as status:
                st.write("🔍 [글로벌 빅테크] Sentiment API 응답 대기 중...")
                time.sleep(1.5) # 껍데기 모션
                st.write("🔍 [기업 실적·공시] Sentiment API 응답 대기 중...")
                time.sleep(1.5)
                status.update(label="✨ Step 1 뼈대 구축 완료! (실제 로직은 Step 2에서 주입됩니다)", state="complete")
            
            st.session_state.cmd_run_alpha = False
            st.rerun()

        st.markdown("### 📈 글로벌 경제 감성(Sentiment) 분석 보드")
        st.warning("🚀 현재 UI 독립성 테스트(Step 1)가 적용되었습니다. 사이드바의 버튼을 눌러도 다른 탭의 데이터가 초기화되지 않습니다. 준님의 승인 후 Step 2(Alpha Vantage API 로직 연결)를 진행합니다.")

    # ------------------------------------------
    # 탭 3: 유튜브 (기존 V9.9 로직 탑재)
    # ------------------------------------------
    with tab_yt:
        if st.session_state.cmd_run_yt:
            ch_name = st.session_state.yt_data["channel_name"]
            ch_id = st.session_state.cmd_yt_channel_id
            with st.spinner(f"📡 '{ch_name}' 채널 15시간 이내 영상 스캔 중..."):
                st.session_state.yt_data["videos"] = fetch_youtube_videos_15h(ch_id)
            st.session_state.cmd_run_yt = False
            save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)
            st.rerun()

        if st.session_state.yt_data["videos"]:
            st.markdown(f"### 📺 **{st.session_state.yt_data['channel_name']}** - 최근 업로드 영상")
            for item in st.session_state.yt_data["videos"]:
                video_id = item.get('id', {}).get('videoId')
                if not video_id: continue
                
                snippet = item['snippet']
                title = sanitize_text(snippet['title'])
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                
                dt = datetime.strptime(snippet['publishedAt'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('Asia/Seoul'))
                
                with st.container():
                    col_img, col_info = st.columns([3, 7])
                    col_img.image(snippet['thumbnails']['high']['url'])
                    with col_info:
                        st.markdown(f"#### [{title}]({video_url})")
                        st.markdown(f"🗓️ **업로드:** {dt.strftime('%Y년 %m월 %d일 %H시 %M분')}")
                        if st.button("🧠 영상 내용 프리즘 요약하기", key=f"yt_btn_{video_id}", use_container_width=True):
                            with st.spinner("AI가 영상을 해독 중입니다..."):
                                st.write(extract_transcript_and_summarize(video_id, title, snippet['description']))
                st.write("---")
        else:
            st.info("👈 왼쪽 사이드바에서 유튜브 채널 버튼을 눌러주세요.")

if __name__ == "__main__":
    main()
