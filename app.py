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
    """환경변수 → st.secrets → fallback 순으로 키를 로드합니다."""
    val = os.environ.get(key, "")
    if val:
        return val
    try:
        return st.secrets.get(key, fallback)
    except Exception:
        return fallback

# 🚨 [중요] fallback 값을 빈 문자열("")로 비워두어 깃허브 노출을 원천 차단했습니다.
GEMINI_API_KEY       = _get_secret("GEMINI_API_KEY",       "")
NAVER_CLIENT_ID      = _get_secret("NAVER_CLIENT_ID",      "")
NAVER_CLIENT_SECRET  = _get_secret("NAVER_CLIENT_SECRET",  "")
NEWS_API_KEY         = _get_secret("NEWS_API_KEY",         "")
YOUTUBE_API_KEY      = _get_secret("YOUTUBE_API_KEY",      "")
# 🚀 Alpha Vantage 유료 API 키 로드
ALPHAVANTAGE_API_KEY = _get_secret("ALPHAVANTAGE_API_KEY", "")

# 초고속 gemini-2.5-flash 단일 엔진
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 📌 캐시(백업) 시스템 (V10.2 독립 서랍 구조)
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
                return (
                    data.get("market", {}), 
                    data.get("news_data", {"results": {}, "map": {}, "summaries": {}}),
                    data.get("alpha_data", {"results": {}, "map": {}, "summaries": {}, "tabloid_results": []}),
                    data.get("yt_data", {"channel_name": "", "videos": [], "summaries": {}})
                )
    except Exception as e:
        print(f"[Error] 캐시 로드 실패: {e}")
    return {}, {"results": {}, "map": {}, "summaries": {}}, {"results": {}, "map": {}, "summaries": {}, "tabloid_results": []}, {"channel_name": "", "videos": [], "summaries": {}}

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

def get_lookback_hours(mode="general"):
    """한국시간 기준으로 뉴스 수집 시간 범위를 동적으로 결정"""
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.now(kst)
    weekday = now_kst.weekday()  # 0=월요일, 6=일요일
    hour = now_kst.hour

    if mode == "alpha":
        # 일요일 또는 월요일 오전 8시 이전 → 72시간
        if weekday == 6 or (weekday == 0 and hour < 8):
            return 72
        return 15
    else:
        # 일요일 또는 월요일 → 24시간
        if weekday == 6 or weekday == 0:
            return 24
        return 15

def is_within_hours(date_str, hours=15):
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
        return (now_utc - dt).total_seconds() <= hours * 3600
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
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="5d")
            
            if hist is not None and not hist.empty:
                valid_hist = hist.dropna(subset=['Close'])
                
                if len(valid_hist) >= 2:
                    current_price = valid_hist['Close'].iloc[-1]
                    previous_close = valid_hist['Close'].iloc[-2]
                    
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

# 🚨 일반 뉴스용 (국내 중심) 언론사 화이트리스트
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

# 🚀 단일 섹션 뉴스 수집 엔진
def fetch_single_sector_news(sector_name, search_query, start_idx):
    all_news_context = []
    news_map = {}
    article_idx = start_idx
    hours = get_lookback_hours(mode="general")

    # 1. GNews 엔진
    google_news = GNews(language='ko', country='KR', max_results=30, period=f'{hours}h')
    try:
        for item in google_news.get_news(search_query):
            if is_within_hours(item.get('published date', ''), hours):
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
                if is_within_hours(item.get('pubDate', ''), hours):
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

    # 3. NewsAPI 외신 수집 엔진
    try:
        newsapi_url = "https://newsapi.org/v2/everything"
        newsapi_params = {"apiKey": NEWS_API_KEY, "q": search_query, "language": "ko", "sortBy": "publishedAt", "pageSize": 30}
        newsapi_res = requests.get(newsapi_url, params=newsapi_params)
        if newsapi_res.status_code == 200:
            for item in newsapi_res.json().get('articles', []):
                if item.get('title') and item['title'] != "[Removed]":
                    if is_within_hours(item.get('publishedAt', ''), hours):
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
    1. 원본의 `[ID:N숫자]` 또는 `[ID:A숫자]` 꼬리표를 확인하여 ID를 정확히 매칭하세요.
    2. JSON의 모든 키(key)와 문자열 값(value)은 반드시 쌍따옴표(")로 감싸야 합니다 (표준 JSON 규격).
    3. 기사 제목(title) 내부에 인용구가 있다면, 파싱 에러 방지를 위해 기사 제목 내부의 따옴표만 홑따옴표(')로 변경하세요.
    4. 부가 설명 없이 대괄호 [] 로 시작하고 끝나는 JSON 배열만 출력하세요.
    5. 각 기사 제목 끝에 붙은 `[도메인]` 형태의 출처 정보는 절대 생략하거나 수정하지 말고 그대로 유지하세요.

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
            return json.loads(cleaned_text)
        except json.JSONDecodeError:
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
# 📈 [NEW] Alpha Vantage 프리미엄 통신 로직 (V10.2 투 트랙 필터링 적용)
# ==========================================
def fetch_alpha_vantage_news(sector_name, start_idx, sort="RELEVANCE", use_tickers=False):
    if not ALPHAVANTAGE_API_KEY:
        return "", {}, [], start_idx, False

    topic_map = {
        "글로벌 빅테크": "technology",
        "기업 실적·공시": "earnings",
        "거시경제 지표": "economy_macro",
        "해외 증시·자산": "financial_markets",
        "정부 정책·규제": "economy_fiscal",
        "글로벌 지정학": "economy_politics"
    }

    ticker_map = {
        "글로벌 빅테크":  "NVDA,AAPL,MSFT,GOOGL,META,TSLA,AMZN",
        "기업 실적·공시": "AAPL,MSFT,GOOGL,AMZN,META,NVDA,TSLA,JPM",
        "거시경제 지표":  "SPY,TLT,GLD,USO,VIX",
        "해외 증시·자산": "SPY,QQQ,DIA,GLD,BTC",
        "정부 정책·규제": "XLF,XLE,JPM,GS,BAC",
        "글로벌 지정학":  "XOM,CVX,LMT,RTX,BA,GLD"
    }

    hours = get_lookback_hours(mode="alpha")
    time_from = (datetime.now(pytz.UTC) - timedelta(hours=hours)).strftime("%Y%m%dT%H%M")
    url = "https://www.alphavantage.co/query"

    if use_tickers:
        tickers = ticker_map.get(sector_name)
        if not tickers:
            return "", {}, [], start_idx, False
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": tickers,
            "time_from": time_from,
            "limit": 200,
            "sort": sort,
            "apikey": ALPHAVANTAGE_API_KEY
        }
    else:
        topic = topic_map.get(sector_name)
        if not topic:
            return "", {}, [], start_idx, False
        params = {
            "function": "NEWS_SENTIMENT",
            "topics": topic,
            "time_from": time_from,
            "limit": 200,
            "sort": sort,
            "apikey": ALPHAVANTAGE_API_KEY
        }

    # 🛡️ 1.5차 필터링용 리스트 정의 (~100개 확장판)
    ALPHA_PREMIUM_PUBLISHERS = [
        # 글로벌 통신사
        "reuters", "bloomberg", "apnews", "afp", "kyodonews",
        # 미국 주요 금융/경제
        "wsj", "ft.com", "cnbc", "marketwatch", "barrons", "forbes", "fortune",
        "thestreet", "investors", "morningstar", "foxbusiness", "finance.yahoo",
        "investing.com", "kiplinger", "investopedia", "bankrate", "spglobal",
        "institutionalinvestor", "venturebeat",
        # 미국 주요 종합뉴스
        "nytimes", "washingtonpost", "cnn", "bbc", "usatoday", "npr",
        "theatlantic", "newyorker", "newsweek", "time", "politico", "axios",
        "thehill", "abcnews", "nbcnews", "cbsnews", "vox", "latimes",
        "bostonglobe", "nypost", "chicagotribune", "sfgate", "propublica",
        # 영국/유럽
        "theguardian", "economist", "independent.co.uk", "telegraph.co.uk",
        "euronews", "dw.com", "france24", "lemonde", "spiegel",
        # 아시아/오세아니아
        "nikkei", "scmp", "straitstimes", "channelnewsasia", "japantimes",
        "timesofindia", "thehindu", "smh.com.au", "globeandmail", "cbc.ca",
        # 테크
        "techcrunch", "wired", "theverge", "arstechnica", "zdnet",
        "technologyreview", "cnet", "pcmag", "tomshardware", "eetimes",
        "semianalysis", "9to5mac", "macrumors", "androidauthority", "engadget",
        # 국제/정책
        "businessinsider", "aljazeera", "foreignpolicy", "foreignaffairs",
        "cfr.org", "brookings",
        # 크립토
        "coindesk", "cointelegraph", "decrypt", "theblock", "blockworks",
        # 에너지/원자재
        "oilprice",
        # 과학/헬스
        "statnews", "nature.com", "pbs",
    ]
    
    ALPHA_TABLOID_PUBLISHERS = [
        "fool", "motley fool", "benzinga", "zacks", "seeking alpha", "seekingalpha", "zerohedge"
    ]

    news_map = {}
    context_list = []
    tabloid_list = [] # 찌라시 전용 분리수거함
    idx = start_idx
    
    try:
        res = requests.get(url, params=params)
        if res.status_code == 200:
            data = res.json()
            if "Information" in data or "Note" in data:
                print(f"[Alpha Vantage] API Limit Reached: {data}")
                return "", {}, [], start_idx, True

            feed = data.get("feed", [])
            for item in feed:
                source_domain = item.get("source_domain", "External").lower()
                
                # 출처 검사 로직
                is_premium = any(p in source_domain for p in ALPHA_PREMIUM_PUBLISHERS)
                is_tabloid = any(t in source_domain for t in ALPHA_TABLOID_PUBLISHERS)
                
                # 둘 다 해당하지 않는 잡다한 매체는 아예 버림
                if not is_premium and not is_tabloid:
                    continue

                sentiment = item.get("overall_sentiment_label", "Neutral")
                clean_title = f"[{sentiment}] {sanitize_text(item.get('title', ''))} [{item.get('source_domain', 'External')}]"
                n_id = f"A{idx}"
                
                news_map[n_id] = {
                    "url": item.get('url', ''), 
                    "title": clean_title, 
                    "snippet": sanitize_text(item.get('summary', ''))
                }
                
                # 투 트랙 라우팅
                if is_tabloid:
                    tabloid_list.append({"id": n_id, "title": clean_title})
                else: # is_premium
                    context_list.append(f"[ID:{n_id}] {clean_title}")
                
                idx += 1
    except Exception as e:
        print(f"[Error] Alpha Vantage 통신 실패: {e}")

    return "\n".join(context_list), news_map, tabloid_list, idx, False

# 🇰🇷 [V10.2] 일괄 번역 엔진 (1회 Gemini 호출로 N개 제목 동시 번역)
def batch_translate_to_korean(titles: list) -> list:
    if not titles:
        return titles

    numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(titles)])
    prompt = f"""
    아래 영어 뉴스 헤드라인들을 자연스러운 한국어로 번역하세요.

    [절대 규칙]
    1. 문장 맨 앞의 `[Bullish]`, `[Bearish]`, `[Neutral]` 감성 배지는 번역하지 말고 원문 그대로 유지하세요.
    2. 문장 맨 끝의 `[www.example.com]` 형태의 출처 태그는 도메인을 떼고 `(언론사명)` 형태로 바꾸어 문장 맨 끝에 배치하세요.
       (예: [www.reuters.com] -> (Reuters), [finance.yahoo.com] -> (Yahoo Finance))
    3. 중간의 핵심 기사 내용만 한국어로 매끄럽게 번역하세요.
    4. 반드시 번호 순서대로, "번호. 번역된 제목" 형식으로만 출력하세요. 부가 설명 없이.

    [원본 텍스트]
    {numbered}
    """
    try:
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        lines = [l.strip() for l in response.text.strip().split('\n') if l.strip()]
        translated = []
        for line in lines:
            if line and line[0].isdigit() and '. ' in line:
                translated.append(line.split('. ', 1)[1])
        if len(translated) == len(titles):
            return translated
        print(f"[Warning] 번역 결과 수 불일치 ({len(translated)}/{len(titles)}), 원본 사용")
        return titles
    except Exception as e:
        print(f"[Error] 일괄 번역 실패: {e}")
        return titles

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
        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text
    except Exception as e:
        print(f"[Error] 유튜브 AI 영상 요약 실패: {e}")
        return "AI 요약 중 오류가 발생했습니다."

# ==========================================
# 🧱 [V10.0] 진정한 비동기 프래그먼트(Fragments) 렌더링 부대
# ==========================================

@st.fragment
def render_tab_news_fragment(target_keywords, user_interest, default_keywords):
    """일반 뉴스 수집 및 렌더링을 담당하는 완전히 독립된 구역"""
    col_run, col_stop = st.columns(2)
    if col_run.button("🚀 뉴스 가동", type="primary", use_container_width=True, key="btn_run_general_news"):
        st.session_state.news_data = {"results": {}, "map": {}, "summaries": {}}
        st.session_state.selected_news_id = None
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

        st.session_state.market_data = get_market_indicators()
        market_str = " | ".join([f"{k}: {v}" for k, v in st.session_state.market_data.items()])
        market_ph.success(f"**[시장 지표]** {market_str}")

        sectors_keys = list(target_keywords.keys())
        sector_containers = {sec: st.empty() for sec in sectors_keys}
        
        current_article_idx = 1
        
        for idx, sector_name in enumerate(sectors_keys):
            target_kw = target_keywords.get(sector_name, "")
            search_query = target_kw if target_kw else default_keywords[sector_name]
            
            ui_status_text.markdown(f"**🔍 [{sector_name}] 데이터 수집 및 AI 정제 중... ({idx+1}/10)**")
            ui_progress_bar.progress(int(((idx+1) / 10) * 100))
            
            raw_context, local_map, current_article_idx = fetch_single_sector_news(sector_name, search_query, current_article_idx)
            curated_list = apply_prism_lens_single(sector_name, raw_context, user_interest, search_query)
            
            st.session_state.news_data["map"].update(local_map)
            st.session_state.news_data["results"][sector_name] = curated_list

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
        ui_status_text.markdown("✨ **모든 브리핑 조립이 완료되었습니다!**")
        timer_placeholder.empty()

        save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)

    if col_stop.button("🛑 정지", use_container_width=True, key="btn_stop_general_news"):
        st.error("🚫 브리핑 가동이 취소되었습니다.")
        st.stop()

    if not st.session_state.news_data["results"]:
        st.info("👆 위쪽의 '🚀 뉴스 가동' 버튼을 눌러주세요.")
    else:
        col1, col2 = st.columns([1, 1])
        with col1:
            st.markdown("### 📋 가나디의 뉴스가져오기 완료!!")
            st.success("우측의 [내용보기] 버튼을 누르면 요약창이 뜹니다.")
            if st.session_state.get('final_time_str'):
                st.markdown(f"**⏱️ 브리핑 조립 소요 시간:** `{st.session_state.final_time_str}`")

            st.markdown(f"**개장전★주요이슈 점검 Updated at ({datetime.now().strftime('%Y/%m/%d')})**")

            st.markdown("**[시장 지표]**")
            for k, v in st.session_state.market_data.items():
                st.markdown(f"* {k}: {v}")
            st.write("---")

            categories_data = st.session_state.news_data["results"]
            for category, items in categories_data.items():
                if not items:
                    continue
                st.markdown(f"#### [{category}]")
                for idx, item in enumerate(items):
                    title = item.get('title', '제목 없음') if isinstance(item, dict) else item
                    item_id = item.get('id') if isinstance(item, dict) else None

                    if not item_id:
                        for k, v in st.session_state.news_data["map"].items():
                            if v['title'] == title or title in v['title']:
                                item_id = k
                                break
                        if not item_id:
                            item_id = f"fallback_{category}_{idx}"

                    title = re.sub(r'^\[.*?\]\s*', '', title).strip()
                    
                    c_text, c_btn = st.columns([8.5, 1.5])
                    c_text.markdown(f"• {title}")
                    if c_btn.button("내용보기", key=f"btn_gen_{category}_{idx}_{item_id}", use_container_width=True):
                        st.session_state.selected_news_id = item_id
                st.write("")

        with col2:
            st.markdown("### 뉴스내용 간단히 요약")
            if st.session_state.selected_news_id and st.session_state.selected_news_id in st.session_state.news_data["map"]:
                selected_info = st.session_state.news_data["map"][st.session_state.selected_news_id]
                title   = selected_info['title']
                snippet = selected_info['snippet']
                url     = selected_info['url']

                st.markdown(f"**🔗 [웹 브라우저에서 원본 기사 따로 열기]({url})**")
                st.markdown(f"**📰 기사 제목:** {title}")
                st.markdown("---")

                if st.session_state.selected_news_id not in st.session_state.news_data["summaries"]:
                    with st.spinner("헤드라인에서 숫자와 통계를 뽑아내어 요약 중입니다..."):
                        summary_text = generate_headline_data_summary(title, snippet)
                        st.session_state.news_data["summaries"][st.session_state.selected_news_id] = summary_text

                st.info("💡 **헤드라인 기반 데이터 추출 결과**")
                st.write(st.session_state.news_data["summaries"][st.session_state.selected_news_id])
            else:
                st.markdown('''
                    <div style="padding: 2rem; background-color: #f8f9fa; border-radius: 10px; text-align: center; color: #6c757d;">
                        👈 왼쪽 브리핑 보드에서 <b>[내용보기]</b> 버튼을 클릭해 보세요.<br>
                        Gemini가 <b>"통계", "인용구", "숫자", "데이터"</b>를 포함하여 요약해 드립니다!
                    </div>
                ''', unsafe_allow_html=True)


@st.fragment
def render_tab_alpha_fragment(target_keywords, user_interest, default_keywords):
    """Alpha Vantage 유료 뉴스를 수집/렌더링하는 전용 프래그먼트"""
    if st.button("💎 Alpha Vantage 프리미엄 가동", type="primary", use_container_width=True, key="btn_run_alpha_vantage"):
        if not ALPHAVANTAGE_API_KEY:
            st.error("🚨 Alpha Vantage API 키가 등록되지 않았습니다. Secrets를 확인해주세요.")
            return

        st.session_state.alpha_data = {"results": {}, "map": {}, "summaries": {}, "tabloid_results": []}
        st.session_state.selected_alpha_id = None
        start_time = time.time()
        
        timer_placeholder = st.empty()
        with timer_placeholder:
            components.html(
                """
                <div style="font-family: 'Segoe UI', sans-serif; font-size: 16px; font-weight: 500; color: #055160; background-color: #cff4fc; padding: 20px; border-radius: 8px; border: 1px solid #b6effb; text-align: center; margin-bottom: 10px;">
                    💎 <b>프리미엄 감성 데이터 수집 및 번역 중...</b> <br><br>
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
        
        sectors_keys = list(target_keywords.keys())
        alpha_idx = 1
        for sector_name in sectors_keys:
            target_kw = target_keywords.get(sector_name, "")
            search_query = target_kw if target_kw else default_keywords[sector_name]
            
            ui_status_text.markdown(f"🔍 [{sector_name}] 감성 분석 스캔 중...")
            raw_context, local_map, local_tabloid, alpha_idx, api_limit_hit = fetch_alpha_vantage_news(sector_name, alpha_idx)

            if api_limit_hit:
                timer_placeholder.empty()
                ui_status_text.empty()
                st.error("🚨 Alpha Vantage API 일일 한도를 초과했습니다. 내일 다시 시도해주세요. (무료 플랜: 25회/일)")
                return
            
            # 찌라시 분리수거 및 일괄 번역 (Gemini 1회 호출)
            if local_tabloid:
                ui_status_text.markdown(f"🗑️ [{sector_name}] 찌라시 분리수거 및 번역 중...")
                tabloid_titles = [t['title'] for t in local_tabloid]
                translated_tabloid = batch_translate_to_korean(tabloid_titles)
                for t_item, kor_title in zip(local_tabloid, translated_tabloid):
                    t_item['title'] = kor_title
                    if t_item['id'] in local_map:
                        local_map[t_item['id']]['title'] = kor_title
                st.session_state.alpha_data["tabloid_results"].extend(local_tabloid)

            # 프리미엄 1티어 뉴스 AI 필터링 및 일괄 번역 (최소 5개 보장 재시도 로직)
            if raw_context:
                ui_status_text.markdown(f"🧠 [{sector_name}] AI 엘리트 필터링 및 **한국어 번역 중...**")
                curated_list = apply_prism_lens_single(sector_name, raw_context, user_interest, search_query)

                # 5개 미만이면 LATEST 정렬로 2차 호출 후 합산 재필터링
                if len(curated_list) < 5:
                    ui_status_text.markdown(f"🔄 [{sector_name}] 뉴스 부족 ({len(curated_list)}개) → 추가 수집 중...")
                    raw_context2, local_map2, local_tabloid2, alpha_idx, api_limit_hit2 = fetch_alpha_vantage_news(sector_name, alpha_idx, sort="RELEVANCE", use_tickers=True)
                    if api_limit_hit2:
                        timer_placeholder.empty()
                        ui_status_text.empty()
                        st.error("🚨 Alpha Vantage API 일일 한도를 초과했습니다. 내일 다시 시도해주세요. (무료 플랜: 25회/일)")
                        return
                    if raw_context2:
                        local_map.update(local_map2)
                        local_tabloid.extend(local_tabloid2)
                        merged_context = raw_context + "\n" + raw_context2
                        ui_status_text.markdown(f"🧠 [{sector_name}] 합산 데이터 재필터링 중...")
                        curated_list = apply_prism_lens_single(sector_name, merged_context, user_interest, search_query)

                eng_titles = [item.get('title', '') for item in curated_list]
                kor_titles = batch_translate_to_korean(eng_titles)

                translated_list = []
                for item, kor_title in zip(curated_list, kor_titles):
                    item_id = item.get('id', '')
                    translated_list.append({"id": item_id, "title": kor_title})
                    if item_id in local_map:
                        local_map[item_id]['title'] = kor_title

                st.session_state.alpha_data["results"][sector_name] = translated_list

            # ✅ map은 항상 업데이트 (찌라시만 있는 섹터도 심층분석 가능하도록)
            st.session_state.alpha_data["map"].update(local_map)
        
        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        st.session_state.final_alpha_time_str = f"{mins:02d}분 {secs:02d}초"
        
        timer_placeholder.empty()
        ui_status_text.empty()
        st.success("✨ Alpha Vantage 프리미엄 분석 및 찌라시 분리 완료!")
        
        save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)

    if not st.session_state.alpha_data["results"]:
        st.info("👆 위의 '💎 Alpha Vantage 프리미엄 가동' 버튼을 눌러주세요.")
    else:
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown("### 📈 글로벌 프리미엄 브리핑")
            if st.session_state.get('final_alpha_time_str'):
                st.markdown(f"**⏱️ 프리미엄 브리핑 소요 시간:** `{st.session_state.final_alpha_time_str}`")
            st.write("---")

            for category, items in st.session_state.alpha_data["results"].items():
                if not items: 
                    continue
                st.markdown(f"#### [{category}]")
                for idx, item in enumerate(items):
                    title = item.get('title', '제목 없음') if isinstance(item, dict) else item
                    item_id = item.get('id') if isinstance(item, dict) else f"fallback_alpha_{idx}"
                    
                    c_text, c_btn = st.columns([8.5, 1.5])
                    c_text.markdown(f"• {title}")
                    if c_btn.button("심층분석", key=f"btn_alp_{category}_{item_id}", use_container_width=True):
                        st.session_state.selected_alpha_id = item_id
                st.write("")
            
            # 🚨 찌라시 전용 렌더링 구역 (10대 섹션이 끝난 하단에 배치)
            if st.session_state.alpha_data.get("tabloid_results"):
                st.write("---")
                st.markdown("#### 🚨 [주의] 오늘의 찌라시 & 가십성 리포트 모아보기")
                for idx, item in enumerate(st.session_state.alpha_data["tabloid_results"]):
                    title = item.get('title', '제목 없음')
                    item_id = item.get('id')
                    
                    c_text, c_btn = st.columns([8.5, 1.5])
                    c_text.markdown(f"• {title}")
                    if c_btn.button("심층분석", key=f"btn_alp_tabloid_{idx}_{item_id}", use_container_width=True):
                        st.session_state.selected_alpha_id = item_id
                st.write("")
        
        with c2:
            st.markdown("### 🧬 프리미엄 인사이트")
            sel_id = st.session_state.get('selected_alpha_id')
            if sel_id and sel_id in st.session_state.alpha_data["map"]:
                info = st.session_state.alpha_data["map"][sel_id]
                st.markdown(f"**🔗 [외신 원문 기사 열기]({info['url']})**")
                st.markdown(f"**📰 기사 제목:** {info['title']}")
                st.markdown("---")
                if sel_id not in st.session_state.alpha_data["summaries"]:
                    with st.spinner("프리미엄 데이터 요약 중..."):
                        st.session_state.alpha_data["summaries"][sel_id] = generate_headline_data_summary(info['title'], info['snippet'])
                st.success(st.session_state.alpha_data["summaries"][sel_id])
            else:
                st.markdown('''
                    <div style="padding: 2rem; background-color: #e2e3e5; border-radius: 10px; text-align: center; color: #383d41;">
                        👈 왼쪽 브리핑 보드에서 <b>[심층분석]</b> 버튼을 클릭하세요.
                    </div>
                ''', unsafe_allow_html=True)


@st.fragment
def render_tab_youtube_fragment():
    """유튜브 검색 및 요약을 담당하는 전용 프래그먼트"""
    yt_channels = {
        "오선의 미국증시 라이브": "UC_JJ_NhRqPKcIOj5Ko3W_3w",
        "이효석 아카데미":        "UCxvdCnvGODDyuvnELnLkQWw",
        "내일은 투자왕 김단테":   "UCKTMvIu9a4VGSrpWy-8bUrQ",
        "센서스튜디오":           "UC6dN6Rilzh9KmzymxnZGslg"
    }

    st.markdown("##### 📺 분석하고 싶은 채널을 선택하세요")
    for ch_name, ch_id in yt_channels.items():
        if st.button(f"▶️ {ch_name}", use_container_width=True, key=f"yt_ch_btn_{ch_id}"):
            with st.spinner(f"📡 '{ch_name}' 채널의 최근 15시간 영상을 스캔합니다..."):
                st.session_state.yt_data["videos"] = fetch_youtube_videos_15h(ch_id)
                st.session_state.yt_data["channel_name"] = ch_name
                save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)

    st.write("---")
    
    if not st.session_state.yt_data["videos"]:
        st.info("👆 위의 채널 버튼을 클릭하여 영상을 불러오세요.")
    else:
        st.markdown(f"### 📺 **{st.session_state.yt_data.get('channel_name', '')}** - 최근 15시간 업로드 영상")
        for item in st.session_state.yt_data["videos"]:
            video_id = item.get('id', {}).get('videoId')
            if not video_id: 
                continue

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

                    if st.button("🧠 영상 내용 프리즘 요약하기", key=f"yt_sum_btn_{video_id}", use_container_width=True):
                        with st.spinner("뉴스프리즘 엔진이 유튜브 데이터를 해독 중입니다. (약 5~10초 소요)"):
                            summary = extract_transcript_and_summarize(video_id, title, snippet['description'])
                            st.session_state.yt_data["summaries"][video_id] = summary
                            save_session_to_disk(st.session_state.market_data, st.session_state.news_data, st.session_state.alpha_data, st.session_state.yt_data)
                    
                    if video_id in st.session_state.yt_data.get("summaries", {}):
                        st.success("🎯 **AI 영상 핵심 요약 완료!**")
                        st.write(st.session_state.yt_data["summaries"][video_id])
            st.write("---")


# ==========================================
# 📌 Alpha Vantage MCP 탭
# ==========================================
def render_tab_mcp_fragment():
    """Alpha Vantage MCP 탭 - 탭 진입 시 자동 로딩"""
    import time as _time
    import io, csv
    AV_BASE = "https://www.alphavantage.co/query"

    def av_get(params):
        p = dict(params)
        p["apikey"] = ALPHAVANTAGE_API_KEY
        try:
            r = requests.get(AV_BASE, params=p, timeout=15)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def av_get_text(params):
        p = dict(params)
        p["apikey"] = ALPHAVANTAGE_API_KEY
        try:
            r = requests.get(AV_BASE, params=p, timeout=20)
            return r.text
        except Exception as e:
            return None

    # ── 세션 초기화 ──
    for _k, _v in [
        ('mcp_gainers', None), ('mcp_macro', None), ('mcp_commodities', None),
        ('mcp_earnings_cal', None), ('mcp_last_loaded', 0),
        ('mcp_insider', {}), ('mcp_transcript', {}), ('mcp_ticker_names', {}),
    ]:
        if _k not in st.session_state:
            st.session_state[_k] = _v

    def _parse_comm_csv(text):
        """원자재 CSV 응답 → {"data": [{date, value}, ...]} 형식 변환"""
        rows = []
        for line in (text or "").splitlines()[1:]:
            parts = line.split(',')
            if len(parts) == 2:
                val = parts[1].strip()
                if val and val != '.':
                    rows.append({"date": parts[0].strip(), "value": val})
        return {"data": rows}

    def _yahoo_name(ticker):
        """Yahoo Finance 검색으로 종목명 조회 (timeout 3초)"""
        try:
            r = requests.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": ticker, "quotesCount": 1, "newsCount": 0, "enableFuzzyQuery": False},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=3
            )
            quotes = r.json().get("quotes", [])
            if quotes and quotes[0].get("symbol", "").upper() == ticker.upper():
                return ticker, (quotes[0].get("shortname") or quotes[0].get("longname") or ticker)
        except Exception:
            pass
        return ticker, ticker

    # ── 자동 로딩: 첫 진입 또는 5분 경과 시 ──
    CACHE_TTL = 300
    needs_load = (
        st.session_state.mcp_gainers is None or
        (_time.time() - st.session_state.mcp_last_loaded) > CACHE_TTL
    )
    if needs_load:
        with st.spinner("📡 Alpha Vantage 데이터 로딩 중... (잠시만 기다려 주세요)"):
            st.session_state.mcp_gainers      = av_get({"function": "TOP_GAINERS_LOSERS"})
            def _fred_csv(series_id):
                """FRED 공개 CSV 데이터 (API 키 불필요)"""
                try:
                    r = requests.get(
                        "https://fred.stlouisfed.org/graph/fredgraph.csv",
                        params={"id": series_id}, timeout=10
                    )
                    rows = []
                    for line in r.text.splitlines()[1:]:
                        parts = line.split(',')
                        if len(parts) == 2 and parts[1].strip() not in ('.', ''):
                            rows.append({"date": parts[0].strip(), "value": parts[1].strip()})
                    return {"data": rows}
                except Exception:
                    return {"data": []}

            st.session_state.mcp_macro        = {
                "cpi":          av_get({"function": "CPI",                "interval": "monthly"}),
                "ppi":          _fred_csv("PPIACO"),
                "ffr":          av_get({"function": "FEDERAL_FUNDS_RATE", "interval": "monthly"}),
                "unemployment": av_get({"function": "UNEMPLOYMENT"}),
                "nfp":          av_get({"function": "NONFARM_PAYROLL"}),
            }
            st.session_state.mcp_commodities  = {
                "wti":    _parse_comm_csv(av_get_text({"function": "WTI",         "interval": "daily"})),
                "brent":  _parse_comm_csv(av_get_text({"function": "BRENT",       "interval": "daily"})),
                "gold":   av_get({"function": "GOLD_SILVER_SPOT"}),
                "copper": av_get({"function": "COPPER",      "interval": "monthly"}),
                "ng":     _parse_comm_csv(av_get_text({"function": "NATURAL_GAS", "interval": "daily"})),
            }
            st.session_state.mcp_earnings_cal = av_get_text({"function": "EARNINGS_CALENDAR", "horizon": "3month"})

            # ── 종목명 병렬 조회 (Yahoo Finance) ──
            _raw = st.session_state.mcp_gainers or {}
            _all_tickers = list({
                item['ticker']
                for _lst in ['top_gainers', 'top_losers', 'most_actively_traded']
                for item in _raw.get(_lst, [])[:10]
            })
            _unknown = [t for t in _all_tickers if t not in st.session_state.mcp_ticker_names]
            if _unknown:
                from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
                _ex = _TPE(max_workers=10)
                _futs = {_ex.submit(_yahoo_name, t): t for t in _unknown}
                _ex.shutdown(wait=False)
                try:
                    for _f in _asc(_futs, timeout=8):
                        try:
                            _t, _nm = _f.result()
                            st.session_state.mcp_ticker_names[_t] = _nm
                        except Exception:
                            pass
                except Exception:
                    pass
                for t in _unknown:
                    st.session_state.mcp_ticker_names.setdefault(t, t)

            st.session_state.mcp_last_loaded  = _time.time()

    # ── 헤더 ──
    last_dt = datetime.fromtimestamp(st.session_state.mcp_last_loaded, tz=pytz.timezone('Asia/Seoul'))
    st.markdown("### 🚀 Alpha Vantage 실시간 마켓 대시보드")
    st.caption(f"🕐 마지막 업데이트: {last_dt.strftime('%Y-%m-%d %H:%M')} KST  ·  5분마다 자동 갱신")
    st.write("---")

    # ── 섹션 1: TOP GAINERS / LOSERS / MOST ACTIVE ────────────
    st.markdown("#### 📊 TOP GAINERS / LOSERS / MOST ACTIVE")
    data = st.session_state.mcp_gainers or {}
    if "Information" in data:
        st.warning(data["Information"])
    elif "error" in data:
        st.error(data["error"])
    else:
        _names = st.session_state.get('mcp_ticker_names', {})

        def _fmt_vol(v):
            try:
                v = int(v)
                return f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K"
            except Exception:
                return str(v)

        def _render_items(items):
            for item in items[:10]:
                ticker = item['ticker']
                name   = _names.get(ticker, ticker)
                pct    = item.get("change_percentage", "")
                price  = item['price']
                vol    = _fmt_vol(item.get('volume', ''))
                label  = name if name != ticker else ticker
                st.markdown(f"**{label}**  \n`{ticker}` · ${price} · `{pct}` · {vol}")

        col_g, col_l, col_a = st.columns(3)
        with col_g:
            st.markdown("**🟢 Top Gainers**")
            _render_items(data.get("top_gainers", []))
        with col_l:
            st.markdown("**🔴 Top Losers**")
            _render_items(data.get("top_losers", []))
        with col_a:
            st.markdown("**🔵 Most Active**")
            _render_items(data.get("most_actively_traded", []))
    st.write("---")

    # ── 섹션 2: 거시경제 지표 ──────────────────────────────────
    st.markdown("#### 🏦 거시경제 지표")
    macro = st.session_state.mcp_macro or {}

    def _latest(d):
        return (d.get("data") or [{}])[0]

    def _pct_chg(d, periods):
        """periods개월 전 대비 % 변화율"""
        items = d.get("data") or []
        if len(items) > periods:
            try:
                curr = float(items[0]['value'])
                prev = float(items[periods]['value'])
                if prev:
                    return f"{(curr - prev) / prev * 100:+.2f}%"
            except Exception:
                pass
        return "N/A"

    def _abs_delta(d):
        items = d.get("data") or []
        if len(items) >= 2:
            try:
                return f"{float(items[0]['value']) - float(items[1]['value']):+.2f}"
            except Exception:
                return None
        return None

    if macro:
        # ── CPI / PPI (전월비 · 전년비) ──
        col1, col2, col3, col4 = st.columns(4)
        cpi = macro.get("cpi", {})
        ppi = macro.get("ppi", {})
        with col1:
            l = _latest(cpi)
            st.metric("🏷️ CPI 전월비", _pct_chg(cpi, 1), help=f"소비자물가지수 | 기준일: {l.get('date','')}")
        with col2:
            l = _latest(cpi)
            st.metric("🏷️ CPI 전년비", _pct_chg(cpi, 12), help=f"소비자물가지수 | 기준일: {l.get('date','')}")
        with col3:
            l = _latest(ppi)
            st.metric("🏭 PPI 전월비", _pct_chg(ppi, 1), help=f"생산자물가지수 (FRED PPIACO) | 기준일: {l.get('date','')}")
        with col4:
            l = _latest(ppi)
            st.metric("🏭 PPI 전년비", _pct_chg(ppi, 12), help=f"생산자물가지수 (FRED PPIACO) | 기준일: {l.get('date','')}")

        st.write("")

        # ── 연방기금금리 / 실업률 / 비농업고용 ──
        col5, col6, col7 = st.columns(3)
        with col5:
            l = _latest(macro.get("ffr", {}))
            st.metric("🏦 연방기금금리", f"{l.get('value','N/A')}%",
                      delta=_abs_delta(macro.get("ffr", {})), help=f"기준일: {l.get('date','')}")
        with col6:
            l = _latest(macro.get("unemployment", {}))
            st.metric("👷 실업률", f"{l.get('value','N/A')}%",
                      delta=_abs_delta(macro.get("unemployment", {})), help=f"기준일: {l.get('date','')}")
        with col7:
            l = _latest(macro.get("nfp", {}))
            try:
                val_k = f"{float(l.get('value', 0)) / 1000:.0f}K"
            except Exception:
                val_k = l.get("value", "N/A")
            st.metric("💼 비농업고용", val_k,
                      delta=_abs_delta(macro.get("nfp", {})), help=f"기준일: {l.get('date','')}")
    st.write("---")

    # ── 섹션 3: 원자재 시세판 ──────────────────────────────────
    st.markdown("#### 🛢️ 원자재 시세판")
    comm = st.session_state.mcp_commodities or {}

    def _comm_latest(d):
        return (d.get("data") or [{}])[0]

    def _comm_delta(d):
        items = d.get("data") or []
        if len(items) >= 2:
            try:
                return f"{float(items[0]['value']) - float(items[1]['value']):+.2f}"
            except Exception:
                return None
        return None

    if comm:
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            l = _comm_latest(comm.get("wti", {}))
            st.metric("🛢️ WTI ($/배럴)", f"${l.get('value','N/A')}", help=f"기준일: {l.get('date','')}")
        with col2:
            l = _comm_latest(comm.get("brent", {}))
            st.metric("🛢️ Brent ($/배럴)", f"${l.get('value','N/A')}", help=f"기준일: {l.get('date','')}")
        with col3:
            gold_raw = comm.get("gold", {})
            rcp = gold_raw.get("Realtime Commodity Prices", {})
            gold_price = rcp.get("Realtime Gold Price (USD)", "N/A")
            if isinstance(gold_price, dict):
                gold_price = list(gold_price.values())[0] if gold_price else "N/A"
            st.metric("🥇 금 ($/oz)", f"${gold_price}")
        with col4:
            l = _comm_latest(comm.get("copper", {}))
            st.metric("🔧 구리", f"${l.get('value','N/A')}", delta=_comm_delta(comm.get("copper", {})))
        with col5:
            l = _comm_latest(comm.get("ng", {}))
            st.metric("💨 천연가스 ($/MMBtu)", f"${l.get('value','N/A')}", help=f"기준일: {l.get('date','')}")
    st.write("---")

    # ── 섹션 4: 실적 발표 캘린더 ──────────────────────────────
    st.markdown("#### 📅 실적 발표 캘린더 (3개월)")
    raw_text = st.session_state.mcp_earnings_cal
    if raw_text:
        try:
            reader = csv.DictReader(io.StringIO(raw_text))
            rows = [r for r in reader]
            if rows:
                st.dataframe(rows[:50], use_container_width=True)
            else:
                st.info("예정된 실적 발표가 없습니다.")
        except Exception as e:
            st.error(f"파싱 오류: {e}")
    st.write("---")

    # ── 섹션 5: 인사이더 트랜잭션 ──────────────────────────────
    st.markdown("#### 🕵️ 인사이더 트랜잭션")
    col_i1, col_i2 = st.columns([4, 1])
    with col_i1:
        insider_ticker = st.text_input("종목 티커 입력", placeholder="예: AAPL, NVDA, TSLA", key="mcp_insider_ticker", label_visibility="collapsed")
    with col_i2:
        insider_btn = st.button("조회", key="mcp_insider_btn", use_container_width=True)

    if insider_btn and insider_ticker:
        t_up = insider_ticker.strip().upper()
        with st.spinner(f"{t_up} 인사이더 트랜잭션 로딩 중..."):
            result = av_get({"function": "INSIDER_TRANSACTIONS", "symbol": t_up})
            st.session_state.mcp_insider[t_up] = result

    if st.session_state.mcp_insider:
        for t_key, idata in st.session_state.mcp_insider.items():
            st.markdown(f"**{t_key}** 인사이더 트랜잭션")
            if "Information" in idata:
                st.warning(idata["Information"])
            elif "error" in idata:
                st.error(idata["error"])
            else:
                transactions = idata.get("data", [])
                if transactions:
                    cols_to_show = ["transaction_date", "executive", "executive_title", "transaction_type", "shares", "share_price", "value"]
                    rows = [{k: tx.get(k, "") for k in cols_to_show} for tx in transactions[:20]]
                    st.dataframe(rows, use_container_width=True)
                else:
                    st.info("트랜잭션 데이터가 없습니다.")
    else:
        st.caption("티커를 입력하고 조회 버튼을 클릭하면 인사이더 거래 내역을 표시합니다.")
    st.write("---")

    # ── 섹션 6: 어닝콜 트랜스크립트 AI 요약 ────────────────────
    st.markdown("#### 🎙️ 어닝콜 트랜스크립트 AI 요약")
    col_t1, col_t2, col_t3 = st.columns([2, 2, 1])
    with col_t1:
        tr_ticker = st.text_input("종목 티커", placeholder="예: AAPL", key="mcp_transcript_ticker", label_visibility="collapsed")
    with col_t2:
        tr_quarter = st.text_input("분기", placeholder="예: 2024Q4", key="mcp_transcript_quarter", label_visibility="collapsed")
    with col_t3:
        tr_btn = st.button("🧠 AI 요약", key="mcp_transcript_btn", use_container_width=True)

    if tr_btn:
        if tr_ticker and tr_quarter:
            t_up = tr_ticker.strip().upper()
            cache_key = f"{t_up}_{tr_quarter.strip()}"
            with st.spinner(f"{t_up} {tr_quarter} 어닝콜 AI 요약 중..."):
                result = av_get({"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": t_up, "quarter": tr_quarter.strip()})
                if "Information" in result:
                    st.warning(result["Information"])
                elif "error" in result:
                    st.error(result["error"])
                else:
                    transcript_text = result.get("transcript", "")
                    if not transcript_text:
                        for v in result.values():
                            if isinstance(v, str) and len(v) > 500:
                                transcript_text = v
                                break
                    if transcript_text:
                        prompt = f"""다음은 {t_up}의 {tr_quarter} 어닝콜 트랜스크립트입니다.
한국어로 핵심 내용을 아래 형식으로 요약해 주세요:

1. **실적 요약**: 매출, 순이익, EPS 주요 수치
2. **경영진 핵심 발언**: CEO/CFO의 중요 발언 3~5가지
3. **가이던스**: 다음 분기/연간 전망
4. **주요 리스크**: 경영진이 언급한 위험 요소
5. **투자 시사점**: 종합적인 투자 관점에서의 시사점

트랜스크립트:
{transcript_text[:15000]}"""
                        try:
                            response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
                            summary = response.text
                        except Exception as e:
                            summary = f"요약 생성 실패: {str(e)}"
                        st.session_state.mcp_transcript[cache_key] = {
                            "ticker": t_up, "quarter": tr_quarter.strip(),
                            "summary": summary, "raw_preview": transcript_text[:500],
                        }
                    else:
                        st.warning("트랜스크립트 데이터를 찾을 수 없습니다.")
        else:
            st.warning("티커와 분기를 모두 입력해 주세요.")

    if st.session_state.mcp_transcript:
        for key, item in st.session_state.mcp_transcript.items():
            st.success(f"🎯 **{item['ticker']} {item['quarter']} 어닝콜 요약 완료**")
            st.write(item["summary"])
            with st.expander("원문 미리보기 (첫 500자)", expanded=False):
                st.text(item.get("raw_preview", ""))
            st.write("---")
    else:
        st.caption("종목 티커와 분기를 입력하면 어닝콜 트랜스크립트를 AI로 요약합니다.")


# ==========================================
# 📌 메인 앱 렌더링
# ==========================================
def main():
    st.set_page_config(page_title="News Prism V10.3", page_icon="💎", layout="wide")

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

    LOGO_PATH = "newsprismdog.png"
    if os.path.exists(LOGO_PATH):
        import base64
        with open(LOGO_PATH, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
            st.markdown(
                f"""
                <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 10px;">
                    <img src="data:image/png;base64,{data}" style="height: 200px; border-radius: 8px;">
                    <h1 style="margin: 0; padding: 0; line-height: 1.2;"> 가나디: 신문배달 와써여~~ - V10.3</h1>
                </div>
                """, unsafe_allow_html=True
            )
    else:
        st.title("💎 가나디의 신문배달 - V10.3")
        st.info(f"💡 '{LOGO_PATH}' 파일을 찾을 수 없습니다. 이미지를 깃허브에 업로드해 주세요.")

    st.markdown("##### 🚀top10 섹션 헤드라인 + 📺유튜브 주요채널들")

    with st.expander("📋 버전 히스토리", expanded=False):
        st.markdown("""
| 버전 | 업데이트 내용 |
|------|-------------|
| **V10.3** | Alpha Vantage 2차 요청 티커 기반으로 변경 (완전히 다른 뉴스 풀) |
| **V10.2** | Alpha Vantage 투 트랙 필터링 (프리미엄/찌라시 분리), 탭 구조 + Fragment 도입 |
| **V10.1** | 영어 뉴스 한국어 일괄 번역 엔진 추가 |
| **V10.0** | 캐시 독립 서랍 구조 개편, 세션 상태 전면 재설계 |
| **V9.7** | API 키 하드코딩 제거 (보안 강화), Triple Engine 복구 |
        """)

    st.write("---")

    m_data, n_data, a_data, y_data = load_session_from_disk()
    if 'market_data' not in st.session_state: st.session_state.market_data = m_data
    if 'news_data' not in st.session_state: st.session_state.news_data = n_data
    
    # 🚨 V10.2 찌라시 보관함 글로벌 초기화
    if 'alpha_data' not in st.session_state: 
        st.session_state.alpha_data = a_data
    if 'tabloid_results' not in st.session_state.alpha_data:
        st.session_state.alpha_data['tabloid_results'] = []
        
    if 'yt_data' not in st.session_state: st.session_state.yt_data = y_data

    if 'selected_news_id' not in st.session_state: st.session_state.selected_news_id = None
    if 'selected_alpha_id' not in st.session_state: st.session_state.selected_alpha_id = None
    if 'final_time_str' not in st.session_state: st.session_state.final_time_str = None
    if 'final_alpha_time_str' not in st.session_state: st.session_state.final_alpha_time_str = None

    st.sidebar.header("⚙️ 고급 설정")
    st.sidebar.info("💡 V10.0 업데이트: 사이드바의 가동 버튼들이 각 탭 내부로 이사했습니다! 탭을 넘나들며 동시에 여러 기능을 실행해 보세요.")
    
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
        "국내 대장주": t1.strip(), "글로벌 빅테크": t2.strip(), "기업 실적·공시": t3.strip(),
        "미래 첨단산업": t4.strip(), "거시경제 지표": t5.strip(), "부동산·원자재": t6.strip(),
        "국내 증시·시황": t7.strip(), "해외 증시·자산": t8.strip(), "정부 정책·규제": t9.strip(),
        "글로벌 지정학": t10.strip()
    }

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

    tab_news, tab_alpha, tab_yt, tab_mcp = st.tabs(["📰 일반 뉴스 브리핑", "📈 Alpha Vantage 프리미엄", "📺 유튜브 인사이트", "📊 MCP 마켓 대시보드"])

    with tab_news:
        render_tab_news_fragment(target_keywords, user_interest, default_keywords)

    with tab_alpha:
        render_tab_alpha_fragment(target_keywords, user_interest, default_keywords)

    with tab_yt:
        render_tab_youtube_fragment()

    with tab_mcp:
        render_tab_mcp_fragment()

if __name__ == "__main__":
    main()



