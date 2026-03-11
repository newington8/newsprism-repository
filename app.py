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
    
    # 🗑️ 찌라시 섹션 표시 (밈/바이럴성 - 구독자 많고 사람들이 참조함)
    ALPHA_TABLOID_PUBLISHERS = [
        "fool", "motley fool", "benzinga", "zacks", "seeking alpha", "seekingalpha",
        "zerohedge", "247wallst", "valuewalk", "marketbeat", "tipranks",
    ]

    # 🚫 완전 폐기 (PR 배포 서비스 / 크립토 타블로이드 / 순수 클릭베이트)
    ALPHA_DISCARD_PUBLISHERS = [
        # PR 배포 서비스 (기업 자체 보도자료 - 저널리즘 아님)
        "globenewswire", "prnewswire", "businesswire", "accesswire", "einpresswire",
        # 크립토 타블로이드
        "newsbtc", "bitcoinist", "ambcrypto", "dailyhodl", "beincrypto",
        "cryptopotato", "u.today", "coingape", "cryptonews", "cryptoslate",
        # 저품질 금융 블로그 / 클릭베이트
        "stocknews", "financhill", "finbold", "schaeffersresearch",
        "pulse2", "simplywallst",
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
                
                # 3단계 라우팅
                is_discard = any(t in source_domain for t in ALPHA_DISCARD_PUBLISHERS)
                if is_discard:
                    continue  # 완전 폐기

                is_tabloid = any(t in source_domain for t in ALPHA_TABLOID_PUBLISHERS)

                sentiment = item.get("overall_sentiment_label", "Neutral")
                clean_title = f"[{sentiment}] {sanitize_text(item.get('title', ''))} [{item.get('source_domain', 'External')}]"
                n_id = f"A{idx}"

                news_map[n_id] = {
                    "url": item.get('url', ''),
                    "title": clean_title,
                    "snippet": sanitize_text(item.get('summary', ''))
                }

                # 찌라시 vs 일반 통과
                if is_tabloid:
                    tabloid_list.append({"id": n_id, "title": clean_title})
                else:
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

                # 3개 미만이면 티커 기반 2차 호출 후 합산 재필터링
                if len(curated_list) < 3:
                    ui_status_text.markdown(f"🔄 [{sector_name}] 뉴스 부족 ({len(curated_list)}개/3개 미만) → 티커 기반 추가 수집 중...")
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
@st.cache_data(ttl=86400)
def get_sp500_tickers():
    """GitHub CSV에서 S&P 500 구성 종목 티커 목록 가져오기 (24시간 캐시)"""
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
            timeout=10
        )
        tickers = set()
        for line in r.text.splitlines()[1:]:  # 헤더 제외
            parts = line.split(',')
            if parts and parts[0].strip():
                tickers.add(parts[0].strip())
        return tickers if tickers else set()
    except Exception:
        return set()


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
        ('mcp_brief', {}), ('mcp_tr_candidates', None), ('mcp_tr_query', ''),
        ('mcp_cal_selected', None), ('mcp_cal_data', {}),
        ('mcp_watchlist', None),
        ('mcp_chart_sp', None), ('mcp_chart_nq', None),
        ('mcp_timeline', ''), ('mcp_events', []), ('mcp_news_raw', ''),
        ('mcp_sector', None),
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
        st.session_state.mcp_sector is None or
        (_time.time() - st.session_state.mcp_last_loaded) > CACHE_TTL
    )
    if needs_load:
        with st.spinner("📡 마켓 데이터 로딩 중... (잠시만 기다려 주세요)"):
            # ── S&P 500 HIGHLIGHTS: yfinance 배치 → 4개 리스트 계산 ──
            def _fetch_sp500_highlights():
                # 메모리 절약: 대형주 150개 고정 리스트 (S&P500 시총 상위)
                _SP150 = [
                    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK-B","AVGO","JPM",
                    "LLY","UNH","XOM","V","MA","COST","HD","PG","ORCL","ABBV",
                    "WMT","CVX","MRK","BAC","NFLX","KO","CRM","AMD","PEP","TMO",
                    "ACN","LIN","MCD","CSCO","ABT","TXN","DHR","NKE","WFC","PM",
                    "NEE","INTC","INTU","AMGN","QCOM","RTX","HON","IBM","GE","CAT",
                    "SPGI","AMAT","NOW","BKNG","GS","MS","UNP","BLK","ISRG","SYK",
                    "DE","AXP","ELV","ADI","VRTX","GILD","REGN","C","MMC","PLD",
                    "CB","ZTS","SBUX","AMT","EOG","SLB","CI","MO","CME","SO",
                    "DUK","BSX","ITW","NOC","MDLZ","GD","MCO","PNC","USB","TGT",
                    "AON","HCA","MAR","EMR","FCX","APD","SHW","MMM","ECL","EW",
                    "KLAC","LRCX","SNPS","CDNS","PANW","CRWD","FTNT","MRVL","NXPI","ON",
                    "ADSK","TEAM","PH","ETN","ADP","PAYX","VRSK","CSGP","IDXX","IQV",
                    "MTD","A","DXCM","PODD","GEHC","HUM","CVS","MCK","COR","MOH",
                    "F","GM","TT","GWW","CARR","OTIS","IR","DOV","XYL","IEX",
                    "UBER","LYFT","ABNB","DASH","SPOT","PINS","SNAP","RBLX","U","COIN",
                ]
                _valid = []
                try:
                    _raw = yf.download(
                        _SP150, period="5d", interval="1d",
                        group_by="ticker", threads=True,
                        progress=False, auto_adjust=True
                    )
                except Exception as _e:
                    print(f"[Error] yfinance download 실패: {_e}")
                    return None
                for _tk in _SP150:
                    try:
                        _closes = _raw[_tk]["Close"].dropna()
                        _vols   = _raw[_tk]["Volume"].dropna()
                        if len(_closes) < 2:
                            continue
                        _last = float(_closes.iloc[-1])
                        _prev = float(_closes.iloc[-2])
                        _vol  = int(_vols.iloc[-1]) if len(_vols) > 0 else 0
                        _pct  = (_last - _prev) / _prev * 100 if _prev else 0
                        _amt  = _last * _vol
                        _valid.append({
                            "ticker": _tk,
                            "price":  _last,
                            "volume": _vol,
                            "amount": _amt,
                            "pct":    _pct,
                            "chg":    _last - _prev,
                        })
                    except Exception:
                        pass
                del _raw  # 메모리 즉시 해제
                if not _valid:
                    return None
                _result = {
                    "gainers":   sorted(_valid, key=lambda x: x["pct"],    reverse=True)[:5],
                    "losers":    sorted(_valid, key=lambda x: x["pct"])[:5],
                    "by_amount": sorted(_valid, key=lambda x: x["amount"], reverse=True)[:5],
                    "by_volume": sorted(_valid, key=lambda x: x["volume"], reverse=True)[:5],
                }
                # 상위 종목 1일 intraday (5분봉) 수집
                _top_tks = list({item["ticker"] for lst in _result.values() for item in lst})
                _intra = {}
                for _tk2 in _top_tks:
                    try:
                        _id = yf.Ticker(_tk2).history(period="1d", interval="5m")
                        _intra[_tk2] = _id["Close"].dropna().tolist()
                    except Exception:
                        _intra[_tk2] = []
                for lst in _result.values():
                    for item in lst:
                        item["closes_1d"] = _intra.get(item["ticker"], [])
                return _result

            st.session_state.mcp_gainers = _fetch_sp500_highlights()

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
            def _yf_price(symbol):
                """Yahoo Finance 차트 API로 현재가 + 전일 종가 + 5거래일 전 종가 조회"""
                try:
                    r = requests.get(
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                        params={"interval": "1d", "range": "1mo"},
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=6
                    )
                    result = r.json().get("chart", {}).get("result", [{}])[0]
                    meta   = result.get("meta", {})
                    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c is not None]
                    week_ago = closes[-6] if len(closes) >= 6 else None
                    return {
                        "price":      meta.get("regularMarketPrice"),
                        "prev_close": meta.get("regularMarketPreviousClose"),
                        "week_ago":   week_ago,
                        "currency":   meta.get("currency", "USD"),
                    }
                except Exception:
                    return {}
            _yf_map = {
                "wti":    "CL=F",
                "brent":  "BZ=F",
                "dubai":  "DBLc1",
                "gold":   "GC=F",
                "silver": "SI=F",
                "copper": "HG=F",
                "ng":     "NG=F",
            }
            st.session_state.mcp_commodities = {k: _yf_price(v) for k, v in _yf_map.items()}
            st.session_state.mcp_earnings_cal = av_get_text({"function": "EARNINGS_CALENDAR", "horizon": "3month"})

            # ── 섹터 퍼포먼스 (Alpha Vantage SECTOR) ──
            st.session_state.mcp_sector = av_get({"function": "SECTOR"})

            # ── 종목명 병렬 조회 (Yahoo Finance) ──
            _raw = st.session_state.mcp_gainers or {}
            _all_tickers = list({
                item['ticker']
                for _lst in ['gainers', 'losers', 'by_amount', 'by_volume']
                for item in _raw.get(_lst, [])
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

            # ── 관심종목 워치리스트 수집 ──
            _WATCHLIST = [
                ("VIX",     "^VIX",  "CBOE 변동성 지수"),
                ("CLmain",  "CL=F",  "WTI 원유 선물"),
                ("KORU",    "KORU",  "Direxion 한국 불 3X ETF"),
                ("EWY",     "EWY",   "iShares MSCI 한국 ETF"),
                ("LABU",    "LABU",  "Direxion S&P 바이오텍 3X ETF"),
                ("USDKRW",  "KRW=X", "달러/원 환율"),
                ("IBBQ",    "IBBQ",  "Invesco 나스닥 바이오 ETF"),
                ("SOX",     "^SOX",  "필라델피아 반도체 지수"),
                ("SOXX",    "SOXX",  "iShares 반도체 ETF"),
                ("SOXL",    "SOXL",  "Direxion 반도체 3X ETF"),
            ]
            _wl_rows = []
            for _sym, _tk, _nm in _WATCHLIST:
                try:
                    _th    = yf.Ticker(_tk).history(period="1mo")
                    _th_1d = yf.Ticker(_tk).history(period="1d", interval="5m")
                    if _th.empty:
                        continue
                    _closes    = _th['Close'].dropna().tolist()
                    _closes_1d = _th_1d['Close'].dropna().tolist() if not _th_1d.empty else []
                    _last   = _closes[-1]
                    _prev   = _closes[-2] if len(_closes) >= 2 else _last
                    _chg    = _last - _prev
                    _pct    = _chg / _prev * 100 if _prev else 0
                    _wl_rows.append({"symbol": _sym, "name": _nm,
                                     "closes": _closes, "closes_1d": _closes_1d,
                                     "last": _last, "change": _chg, "pct": _pct})
                except Exception:
                    pass
            st.session_state.mcp_watchlist = _wl_rows

            # ── 24h 차트 데이터 ──
            st.session_state.mcp_chart_sp = yf.Ticker("^GSPC").history(period="1d", interval="5m")
            st.session_state.mcp_chart_nq = yf.Ticker("NQ=F").history(period="1d", interval="5m")

            st.session_state.mcp_last_loaded  = _time.time()

    # ── 헤더 ──
    last_dt = datetime.fromtimestamp(st.session_state.mcp_last_loaded, tz=pytz.timezone('Asia/Seoul'))
    st.markdown("### 🚀 Alpha Vantage 실시간 마켓 대시보드")
    st.caption(f"🕐 마지막 업데이트: {last_dt.strftime('%Y-%m-%d %H:%M')} KST  ·  5분마다 자동 갱신")
    st.write("---")

    # ── 섹션 0-A: S&P500 · NQ 24h 차트 ───────────────────────
    try:
        import plotly.graph_objects as go
        _plotly_ok = True
    except ImportError:
        _plotly_ok = False

    def _get_events_with_global_idx(hist):
        """전역 인덱스 유지하며 차트 범위 내 이벤트 추출 → [(global_idx, naive_dt, desc), ...]"""
        if hist is None or hist.empty or not st.session_state.mcp_events:
            return []
        _et = pytz.timezone('America/New_York')
        _idx_tz = hist.index.tz_convert(_et) if hist.index.tzinfo else hist.index.tz_localize('UTC').tz_convert(_et)
        _mn = _idx_tz.tz_localize(None).min().to_pydatetime()
        _mx = _idx_tz.tz_localize(None).max().to_pydatetime()
        _result = []
        for _gi, _evt in enumerate(st.session_state.mcp_events):
            _dt   = _evt[0]
            _desc = _evt[1]
            _det  = _evt[2] if len(_evt) > 2 else ''
            _eet  = _dt.astimezone(_et) if _dt.tzinfo else _et.localize(_dt)
            _en   = _eet.replace(tzinfo=None)
            if _mn <= _en <= _mx:
                _result.append((_gi, _en, _desc, _det))
        return _result

    def _build_chart(hist, title, color, events_in_range):
        """events_in_range: [(global_idx, naive_dt, title, detail), ...]"""
        if hist is None or hist.empty:
            return None
        try:
            _et  = pytz.timezone('America/New_York')
            _idx_tz = hist.index.tz_convert(_et) if hist.index.tzinfo else hist.index.tz_localize('UTC').tz_convert(_et)
            _idx = _idx_tz.tz_localize(None)
            _cls = hist['Close'].tolist()
            fig  = go.Figure()
            fig.add_trace(go.Scatter(
                x=_idx, y=_cls, mode='lines',
                line=dict(color=color, width=1.5),
                hovertemplate='%{x|%H:%M} ET<br><b>%{y:,.2f}</b><extra></extra>',
                showlegend=False,
            ))
            if events_in_range:
                _y_top = max(_cls)
                _y_bot = min(_cls)
                _marker_y = _y_top + (_y_top - _y_bot) * 0.03
                _mxs, _mdescs = [], []
                for _evt_item in events_in_range:
                    _gi, _en, _desc = _evt_item[0], _evt_item[1], _evt_item[2]
                    _det = _evt_item[3] if len(_evt_item) > 3 else ''
                    _lbl = str(_gi + 1)
                    _xs = _en.strftime('%Y-%m-%d %H:%M:%S')
                    _mxs.append(_xs)
                    _mdescs.append(f"<b>{_lbl}</b>  {_en.strftime('%H:%M')} ET<br>{_desc}")
                    fig.add_shape(
                        type='line', x0=_xs, x1=_xs, y0=0, y1=1, yref='paper',
                        line=dict(color='#E53935', width=1, dash='dot'),
                    )
                    fig.add_annotation(
                        x=_xs, y=1.0, yref='paper',
                        text=f"<b>{_lbl}</b>",
                        showarrow=False,
                        font=dict(size=11, color='white'),
                        bgcolor='#E53935',
                        borderpad=3,
                        xanchor='center',
                        yanchor='bottom',
                    )
                fig.add_trace(go.Scatter(
                    x=_mxs, y=[_marker_y] * len(_mxs),
                    mode='markers',
                    marker=dict(symbol='square', size=20, color='rgba(0,0,0,0)'),
                    hovertemplate='%{customdata}<extra></extra>',
                    customdata=_mdescs,
                    showlegend=False,
                ))
            fig.update_layout(
                title=dict(text=title, font=dict(size=14)),
                height=380,
                margin=dict(l=55, r=15, t=40, b=40),
                xaxis=dict(tickformat='%H:%M', showgrid=True, gridcolor='#333', title='시간 (ET)'),
                yaxis=dict(showgrid=True, gridcolor='#333', tickformat=',.0f', title='포인트', autorange=True),
                hovermode='x unified',
                template='plotly_dark',
                showlegend=False,
            )
            return fig
        except Exception as _e:
            print(f"[Error] 차트 생성 실패: {_e}")
            return None

    _sp_events = _get_events_with_global_idx(st.session_state.get('mcp_chart_sp'))
    _nq_events = _get_events_with_global_idx(st.session_state.get('mcp_chart_nq'))

    st.markdown("#### 📊 S&P 500 · NQ Futures — 24h 차트")
    _col_sp, _col_nq = st.columns(2)
    if not _plotly_ok:
        st.warning("plotly 라이브러리가 필요합니다. requirements.txt를 확인해주세요.")
    else:
        with _col_sp:
            _fig_sp = _build_chart(st.session_state.mcp_chart_sp, "S&P 500  (^GSPC)", "#2196F3", _sp_events)
            if _fig_sp:
                st.plotly_chart(_fig_sp, width="stretch")
            else:
                st.caption("S&P 500 데이터 없음 (장 마감 또는 로딩 중)")
        with _col_nq:
            _fig_nq = _build_chart(st.session_state.mcp_chart_nq, "NQ Futures  (NQ=F)", "#FF9800", _nq_events)
            if _fig_nq:
                st.plotly_chart(_fig_nq, width="stretch")
            else:
                st.caption("NQ Futures 데이터 없음 (장 마감 또는 로딩 중)")

    # 차트 아래 통합 범례 (SP+NQ 합집합, 전역 번호 기준 정렬)
    _all_shown = {}
    for _item in (_sp_events + _nq_events):
        _gi = _item[0]
        if _gi not in _all_shown:
            _all_shown[_gi] = _item
    if _all_shown:
        _legend_rows = ''
        for _gi, _item in sorted(_all_shown.items()):
            _en   = _item[1]
            _desc = _item[2]
            _det  = _item[3] if len(_item) > 3 else ''
            _legend_rows += (
                f"<div style='margin-bottom:6px'>"
                f"<span style='display:inline-block;background:#E53935;color:#fff;font-weight:bold;"
                f"font-size:11px;padding:1px 6px;border-radius:3px;margin-right:6px'>{_gi + 1}</span>"
                f"<b style='color:#000;font-size:13px'>{_en.strftime('%H:%M')} &nbsp;{_desc}</b>"
                + (f"<div style='margin-left:36px;color:#444;font-size:12px;margin-top:1px'>{_det}</div>" if _det else "")
                + "</div>"
            )
        st.markdown(f"<div style='padding:4px 0'>{_legend_rows}</div>", unsafe_allow_html=True)

    # ── 섹션 0-B: 시황 뉴스 타임라인 분석 ────────────────────
    st.markdown("#### 📋 시황 뉴스 타임라인 분석")
    _news_input = st.text_area(
        "Yahoo Finance 등 시황 뉴스를 붙여넣으세요",
        value=st.session_state.mcp_news_raw,
        height=180,
        placeholder="뉴스 본문을 여기에 붙여넣은 후 아래 버튼을 클릭하세요...",
        label_visibility="collapsed"
    )
    _btn_col1, _btn_col2 = st.columns([3, 1])
    with _btn_col1:
        _do_gemini = st.button("🧠 Gemini 타임라인 요약", use_container_width=True, key="mcp_timeline_btn")
    with _btn_col2:
        st.button("📊 차트 반영", use_container_width=True, key="mcp_chart_apply_btn")
    if _do_gemini:
        if _news_input.strip():
            st.session_state.mcp_news_raw = _news_input
            with st.spinner("Gemini가 타임라인을 분석 중입니다..."):
                _now_et  = datetime.now(pytz.timezone('America/New_York'))
                _now_utc = datetime.now(pytz.utc)
                _now_kst = datetime.now(pytz.timezone('Asia/Seoul'))
                _tl_prompt = f"""당신은 미국 금융시장 전문 애널리스트입니다.
아래는 미국 증시 시황과 관련된 뉴스 텍스트입니다. 이 내용을 분석하여 타임라인 형식으로 정리하세요.

[★ 현재 기준 시각 (요약 실행 시점) ★]
- ET (미국 동부): {_now_et.strftime('%Y-%m-%d %H:%M')}
- UTC: {_now_utc.strftime('%Y-%m-%d %H:%M')}
- KST (한국): {_now_kst.strftime('%Y-%m-%d %H:%M')}

[★ 상대 시간 변환 규칙 ★]
- "Today at HH:MM GMT+9" → KST 기준으로 계산 후 ET로 변환 (KST = ET + 14시간)
- "X hours ago" → 현재 ET 시각에서 X시간 빼서 계산
- "X minutes ago" → 현재 ET 시각에서 X분 빼서 계산
- 날짜 없이 시각만 있는 경우 → 오늘 날짜로 간주
- 위 계산으로 ET 시각을 반드시 산출할 것

[★ 출력 형식 — 반드시 준수 ★]
[HH:MM] 이벤트 제목 (한 줄, 핵심만)
- 핵심: 수치·통계·데이터 중심 요약 (2~3문장)
- 발언: "인물명: 인용문" (있는 경우에만 작성)
- 시장 반응: S&P500·나스닥 즉각 반응 (파악 가능한 경우에만 작성)

[작성 원칙]
1. 반드시 [HH:MM] 형식 ET 타임스탬프로 시작 — 명시된 시간 우선, 상대시간은 위 규칙으로 변환
2. %, 달러($), 베이시스포인트(bp), 고용자수(만명) 등 모든 수치 반드시 포함
3. 우선순위: 연준·FOMC 발언 > CPI·NFP·PCE 등 경제지표 > 기업 실적·이슈 > 지정학
4. 시간 오름차순 정렬 (가장 오래된 이벤트 → 최신 순)
5. 광고, 구독 유도, 무관 컨텐츠 완전 제외
6. 전체 한국어로 작성

[원본 뉴스]
{_news_input}"""
                try:
                    _tl_resp = client.models.generate_content(model='gemini-2.5-flash', contents=_tl_prompt)
                    _tl_text = _tl_resp.text
                    st.session_state.mcp_timeline = _tl_text
                    # 이벤트 파싱 → [HH:MM] 또는 [YYYY-MM-DD HH:MM] 추출
                    _et_tz   = pytz.timezone('America/New_York')
                    _today   = datetime.now(_et_tz)
                    # 차트 데이터 시간 범위 계산 (SP500 기준, 없으면 오늘 사용)
                    _chart_min = None
                    _chart_max = None
                    _sp_data = st.session_state.get('mcp_chart_sp')
                    if _sp_data is not None and not _sp_data.empty:
                        _cidx = _sp_data.index.tz_convert(_et_tz) if _sp_data.index.tzinfo else _sp_data.index.tz_localize('UTC').tz_convert(_et_tz)
                        _chart_min = _cidx.min()
                        _chart_max = _cidx.max()
                    # 이벤트 블록 분리: [HH:MM] 타임스탬프 기준으로 split
                    _evt_pattern = r'\[(?:(?:\d{4})-(?:\d{2})-(?:\d{2})\s+)?\d{1,2}:\d{2}\]'
                    _splits = re.split(f'({_evt_pattern})', _tl_text)
                    # _splits: ['앞텍스트', '[09:41]', '제목\n- 핵심:...', '[10:30]', '제목\n...', ...]
                    _evts = []
                    _i = 1
                    while _i < len(_splits) - 1:
                        _ts_str  = _splits[_i]        # '[09:41]' 또는 '[2026-03-11 09:41]'
                        _content = _splits[_i + 1] if _i + 1 < len(_splits) else ''
                        _i += 2
                        try:
                            _tm = re.match(r'\[(?:(\d{4})-(\d{2})-(\d{2})\s+)?(\d{1,2}):(\d{2})\]', _ts_str)
                            if not _tm:
                                continue
                            _yr, _mo, _dy = _tm.group(1), _tm.group(2), _tm.group(3)
                            _h, _mi = int(_tm.group(4)), int(_tm.group(5))
                            # 제목: content 첫 줄
                            _lines = _content.strip().splitlines()
                            _title = _lines[0].strip() if _lines else ''
                            # 상세: 이후 줄에서 핵심/발언/시장반응 추출
                            _detail_lines = []
                            for _ln in _lines[1:]:
                                _ln = _ln.strip()
                                if _ln.startswith('- ') or _ln.startswith('* '):
                                    _detail_lines.append(_ln[2:].strip())
                                elif _ln:
                                    _detail_lines.append(_ln)
                            _detail = ' / '.join(_detail_lines) if _detail_lines else ''
                            if _yr:
                                _dt_evt = _et_tz.localize(datetime(int(_yr), int(_mo), int(_dy), _h, _mi, 0))
                            else:
                                _base = _chart_min.date() if _chart_min else _today.date()
                                _dt_evt = _et_tz.localize(datetime(_base.year, _base.month, _base.day, _h, _mi, 0))
                            _evts.append((_dt_evt, _title, _detail))
                        except Exception:
                            pass
                    st.session_state.mcp_events = _evts
                except Exception as _te:
                    st.error(f"Gemini 요약 실패: {_te}")
        else:
            st.warning("뉴스 텍스트를 먼저 붙여넣어 주세요.")

    if st.session_state.mcp_timeline:
        with st.expander("📊 타임라인 요약 보기", expanded=True):
            st.markdown(st.session_state.mcp_timeline)
        if st.session_state.mcp_events:
            st.caption(f"✅ {len(st.session_state.mcp_events)}개 이벤트 파싱 완료 → 📊 차트 반영 버튼을 눌러 적용하세요")
    st.write("---")

    # ── 섹션 0-C: 섹터맵 ──────────────────────────────────────
    st.markdown("#### 🗺️ S&P 500 섹터 퍼포먼스")
    _sector_data = st.session_state.get('mcp_sector')
    _SECTOR_KR = {
        "Information Technology":    "IT·기술",
        "Health Care":               "헬스케어",
        "Financials":                "금융",
        "Consumer Discretionary":    "임의소비재",
        "Communication Services":    "통신·미디어",
        "Industrials":               "산업재",
        "Consumer Staples":          "필수소비재",
        "Energy":                    "에너지",
        "Materials":                 "소재",
        "Real Estate":               "부동산",
        "Utilities":                 "유틸리티",
    }
    # 섹터별 S&P500 시총 비중 (2024 기준 근사치)
    _SECTOR_WEIGHT = {
        "Information Technology": 29, "Health Care": 13, "Financials": 13,
        "Consumer Discretionary": 10, "Communication Services": 9, "Industrials": 8,
        "Consumer Staples": 6, "Energy": 4, "Materials": 3,
        "Real Estate": 2, "Utilities": 3,
    }
    if _sector_data and "Rank B: 1 Day Performance" in _sector_data:
        try:
            from plotly import graph_objects as _go_s
        except Exception:
            _go_s = None
        _day_perf = _sector_data.get("Rank B: 1 Day Performance", {})
        _labels, _parents, _values, _colors, _texts = [], [], [], [], []
        _labels.append("S&P 500"); _parents.append(""); _values.append(0); _colors.append(0); _texts.append("")
        for _sec, _kr in _SECTOR_KR.items():
            _pct_str = _day_perf.get(_sec, "0%").replace("%", "").strip()
            try:
                _pct = float(_pct_str)
            except Exception:
                _pct = 0.0
            _sign = "+" if _pct >= 0 else ""
            _labels.append(_kr)
            _parents.append("S&P 500")
            _values.append(_SECTOR_WEIGHT.get(_sec, 5))
            _colors.append(_pct)
            _texts.append(f"{_kr}<br>{_sign}{_pct:.2f}%")
        if _go_s:
            _fig_sec = _go_s.Figure(_go_s.Treemap(
                labels=_labels, parents=_parents, values=_values,
                text=_texts, textinfo="text",
                customdata=_colors,
                marker=dict(
                    colors=_colors,
                    colorscale=[[0,"#C62828"],[0.5,"#37474F"],[1,"#1B5E20"]],
                    cmid=0, cmin=-3, cmax=3,
                    showscale=True,
                    colorbar=dict(title="%", thickness=12, len=0.8),
                ),
                hovertemplate="<b>%{label}</b><br>1일 등락: %{customdata:.2f}%<extra></extra>",
            ))
            _fig_sec.update_layout(
                height=320, margin=dict(l=0, r=0, t=0, b=0),
                template="plotly_dark",
            )
            st.plotly_chart(_fig_sec, width="stretch")
            # 섹터별 기간 수익률 테이블
            _period_map = [
                ("Rank B: 1 Day Performance",   "1일"),
                ("Rank C: 5 Day Performance",   "5일"),
                ("Rank D: 1 Month Performance", "1개월"),
                ("Rank E: 3 Month Performance", "3개월"),
                ("Rank F: Year-to-Date (YTD) Performance", "YTD"),
            ]
            with st.expander("📋 섹터별 기간 수익률 상세", expanded=False):
                _tbl_cols = ["섹터"] + [_lbl for _, _lbl in _period_map]
                _tbl_rows = []
                for _sec, _kr in _SECTOR_KR.items():
                    _row = [_kr]
                    for _rank_key, _ in _period_map:
                        _v = _sector_data.get(_rank_key, {}).get(_sec, "N/A")
                        _row.append(_v)
                    _tbl_rows.append(_row)
                import pandas as _pd_sec
                _df_sec = _pd_sec.DataFrame(_tbl_rows, columns=_tbl_cols)
                st.dataframe(_df_sec, use_container_width=True, hide_index=True)
        else:
            st.caption("plotly 라이브러리가 필요합니다.")
    else:
        st.caption("⏳ 섹터 데이터 로딩 중...")
    st.write("---")

    # ── 섹션 0: 관심종목 워치리스트 ───────────────────────────
    st.markdown("#### 👁️ 관심종목 워치리스트")

    def _make_sparkline(closes, width=90, height=32):
        if not closes or len(closes) < 2:
            return ""
        mn, mx = min(closes), max(closes)
        rng = mx - mn or 1
        pts = []
        for i, p in enumerate(closes):
            x = i / (len(closes) - 1) * width
            y = height - (p - mn) / rng * (height - 4) - 2
            pts.append(f"{x:.1f},{y:.1f}")
        color = "#ef5350" if closes[-1] >= closes[0] else "#26a69a"
        return (
            f'<svg width="{width}" height="{height}" style="vertical-align:middle;display:block;">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
            f'</svg>'
        )

    _wl = st.session_state.mcp_watchlist or []
    if _wl:
        _rows_html = ""
        for _r in _wl:
            _up   = _r["pct"] >= 0
            _clr  = "#ef5350" if _up else "#26a69a"
            _sign = "+" if _up else ""
            _spark_1mo = _make_sparkline(_r["closes"])
            _spark_1d  = _make_sparkline(_r.get("closes_1d", []), width=70)
            _last_str = f"{_r['last']:,.2f}"
            _chg_str  = f"{_sign}{_r['change']:,.2f}"
            _pct_str  = f"{_sign}{_r['pct']:.2f}%"
            _rows_html += f"""
            <tr>
                <td class="wl-sym">{_r['symbol']}</td>
                <td class="wl-name">{_r['name']}</td>
                <td class="wl-spark">{_spark_1mo}</td>
                <td class="wl-spark">{_spark_1d if _spark_1d else '<span style="color:#555;font-size:11px">장 마감</span>'}</td>
                <td class="wl-num">{_last_str}</td>
                <td class="wl-num" style="color:{_clr}">{_chg_str}</td>
                <td class="wl-num" style="color:{_clr}"><b>{_pct_str}</b></td>
            </tr>"""

        st.markdown(f"""
        <style>
            .wl-wrap {{ overflow-x:auto; }}
            .wl-tbl {{ width:100%; border-collapse:collapse; font-family:'Segoe UI',sans-serif; font-size:13px; }}
            .wl-tbl th {{ color:#888; font-weight:500; padding:7px 12px; border-bottom:1px solid #333;
                          text-align:left; white-space:nowrap; }}
            .wl-tbl th.wl-r {{ text-align:right; }}
            .wl-tbl td {{ padding:6px 12px; border-bottom:1px solid #1e1e1e; vertical-align:middle; }}
            .wl-sym  {{ font-weight:700; font-size:13px; white-space:nowrap; }}
            .wl-name {{ color:#999; font-size:12px; white-space:nowrap; }}
            .wl-spark {{ padding:4px 12px; }}
            .wl-num  {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; font-size:13px; }}
        </style>
        <div class="wl-wrap">
        <table class="wl-tbl">
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Name</th>
                    <th>1달 추이</th>
                    <th>1일 추이</th>
                    <th class="wl-r">Last</th>
                    <th class="wl-r">Change</th>
                    <th class="wl-r">% Change ↕</th>
                </tr>
            </thead>
            <tbody>{_rows_html}</tbody>
        </table>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.caption("워치리스트 데이터를 불러오는 중...")
    st.write("---")

    # ── 섹션 1: S&P 500 HIGHLIGHTS ────────────────────────────
    st.markdown("#### 🏆 S&P 500 HIGHLIGHTS")
    hl = st.session_state.mcp_gainers
    if not hl:
        st.caption("⏳ 데이터 로딩 중이거나 장 마감 상태입니다.")
    else:
        _names = st.session_state.get('mcp_ticker_names', {})

        def _hl_item(item, sec, metric="pct"):
            tk    = item["ticker"]
            nm    = _names.get(tk, "")
            name  = nm if nm and nm != tk else tk   # 종목명 없으면 티커로 대체
            pct   = item["pct"]
            clr   = "#ef5350" if pct >= 0 else "#26a69a"
            sign  = "+" if pct >= 0 else ""
            vol   = item["volume"]
            amt   = item["amount"]
            vol_s = f"{vol/1e6:.1f}M" if vol >= 1e6 else f"{vol/1e3:.0f}K"
            amt_s = f"${amt/1e9:.1f}B" if amt >= 1e9 else f"${amt/1e6:.0f}M"
            sub   = amt_s if metric == "amount" else vol_s
            sub_label = "거래대금" if metric == "amount" else "거래량"

            c_text, c_spark = st.columns([3, 1])
            with c_text:
                st.markdown(
                    f"**{name}** `{tk}`  \n"
                    f"${item['price']:,.2f} · **{sign}{pct:.2f}%**  \n"
                    f"*{sub_label}: {sub}*"
                )
            with c_spark:
                _sp = _make_sparkline(item.get("closes_1d", []), width=80, height=30)
                if _sp:
                    st.markdown(_sp, unsafe_allow_html=True)
            st.write("")

        col_g, col_l, col_a, col_v = st.columns(4)
        with col_g:
            st.markdown("**🚀 Top 5 Gainers**")
            for _it in hl.get("gainers", []):
                _hl_item(_it, sec="g")
        with col_l:
            st.markdown("**📉 Top 5 Losers**")
            for _it in hl.get("losers", []):
                _hl_item(_it, sec="l")
        with col_a:
            st.markdown("**💰 Most Active (거래대금)**")
            for _it in hl.get("by_amount", []):
                _hl_item(_it, sec="a", metric="amount")
        with col_v:
            st.markdown("**📊 Most Active (거래량)**")
            for _it in hl.get("by_volume", []):
                _hl_item(_it, sec="v", metric="volume")
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

    # 다음 발표일 정보 (고정 스케줄)
    _next_release = {
        "cpi":          "매월 10~15일경 (전월 기준) | BLS",
        "ppi":          "매월 11~16일경 (전월 기준) | BLS",
        "ffr":          "FOMC 회의 후 발표 (연 8회)",
        "unemployment": "매월 첫째 금요일 (NFP 동시 발표) | BLS",
        "nfp":          "매월 첫째 금요일 | BLS",
    }

    if macro:
        # ── CPI / PPI (전월비 · 전년비) ──
        col1, col2, col3, col4 = st.columns(4)
        cpi = macro.get("cpi", {})
        ppi = macro.get("ppi", {})
        with col1:
            l = _latest(cpi)
            st.metric("🏷️ CPI 전월비", _pct_chg(cpi, 1))
            st.caption(f"기준: {l.get('date','')}  \n다음 발표: {_next_release['cpi']}")
        with col2:
            st.metric("🏷️ CPI 전년비", _pct_chg(cpi, 12))
            st.caption(" ")
        with col3:
            l = _latest(ppi)
            st.metric("🏭 PPI 전월비", _pct_chg(ppi, 1))
            st.caption(f"기준: {l.get('date','')}  \n다음 발표: {_next_release['ppi']}")
        with col4:
            st.metric("🏭 PPI 전년비", _pct_chg(ppi, 12))
            st.caption(" ")

        st.write("")

        # ── 연방기금금리 / 실업률 / 비농업고용 ──
        col5, col6, col7 = st.columns(3)
        with col5:
            l = _latest(macro.get("ffr", {}))
            st.metric("🏦 연방기금금리", f"{l.get('value','N/A')}%",
                      delta=_abs_delta(macro.get("ffr", {})))
            st.caption(f"기준: {l.get('date','')}  \n다음 발표: {_next_release['ffr']}")
        with col6:
            l = _latest(macro.get("unemployment", {}))
            st.metric("👷 실업률", f"{l.get('value','N/A')}%",
                      delta=_abs_delta(macro.get("unemployment", {})))
            st.caption(f"기준: {l.get('date','')}  \n다음 발표: {_next_release['unemployment']}")
        with col7:
            l = _latest(macro.get("nfp", {}))
            try:
                val_k = f"{float(l.get('value', 0)) / 1000:.0f}K"
            except Exception:
                val_k = l.get("value", "N/A")
            st.metric("💼 비농업고용", val_k,
                      delta=_abs_delta(macro.get("nfp", {})))
            st.caption(f"기준: {l.get('date','')}  \n다음 발표: {_next_release['nfp']}")
    st.write("---")

    # ── 섹션 3: 원자재 시세판 ──────────────────────────────────
    st.markdown("#### 🛢️ 원자재 시세판")
    comm = st.session_state.mcp_commodities or {}

    def _yf_metric(label, key):
        d = comm.get(key, {})
        p, prev, wk = d.get("price"), d.get("prev_close"), d.get("week_ago")
        try:
            price_str = f"${float(p):,.2f}" if p is not None else "N/A"
        except Exception:
            price_str = "N/A"
        day_pct = week_pct = None
        try:
            if p is not None and prev is not None:
                day_pct = (float(p) - float(prev)) / float(prev) * 100
        except Exception:
            pass
        try:
            if p is not None and wk is not None:
                week_pct = (float(p) - float(wk)) / float(wk) * 100
        except Exception:
            pass
        day_str  = f"{day_pct:+.2f}%"  if day_pct  is not None else None
        week_str = f"{week_pct:+.2f}%" if week_pct is not None else "N/A"
        week_color = "green" if (week_pct or 0) >= 0 else "red"
        st.metric(label, price_str, delta=day_str, help="전일비 등락률")
        st.markdown(f"<small>주간 <span style='color:{week_color}'>{week_str}</span></small>", unsafe_allow_html=True)

    if comm:
        col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
        with col1:
            _yf_metric("🛢️ WTI ($/bbl)",        "wti")
        with col2:
            _yf_metric("🛢️ Brent ($/bbl)",      "brent")
        with col3:
            _yf_metric("🛢️ Dubai ($/bbl)",      "dubai")
        with col4:
            _yf_metric("🥇 금 ($/oz)",           "gold")
        with col5:
            _yf_metric("🥈 은 ($/oz)",           "silver")
        with col6:
            _yf_metric("🔧 구리 ($/lb)",         "copper")
        with col7:
            _yf_metric("💨 천연가스 ($/MMBtu)",  "ng")
    st.write("---")

    # ── 섹션 4: 실적 발표 캘린더 ──────────────────────────────
    st.markdown("#### 📅 실적 발표 캘린더 (S&P 500 · 2주)")
    raw_text = st.session_state.mcp_earnings_cal
    if raw_text:
        try:
            from collections import defaultdict as _dd
            from datetime import datetime as _dt2, timedelta as _td

            reader = csv.DictReader(io.StringIO(raw_text))
            _sp500_set = get_sp500_tickers()
            rows = [r for r in reader if r.get("symbol", "").strip() in _sp500_set]

            if not rows:
                st.info("S&P 500 기업의 예정된 실적 발표가 없습니다.")
            else:
                # 날짜별 그룹핑
                _cal = _dd(list)
                for _r in rows:
                    _cal[_r.get("reportDate", "")].append(_r)
                _today      = _dt2.now()
                _today_str  = _today.strftime("%Y-%m-%d")
                _day_names  = ["월", "화", "수", "목", "금"]
                # 오늘이 포함된 주(평일) 또는 다음 주(주말) 월요일 기준 2주 표시
                _wd  = _today.weekday()
                _mon = _today - _td(days=_wd) if _wd < 5 else _today + _td(days=7 - _wd)

                for _w in range(2):
                    _cur_mon = _mon + _td(weeks=_w)
                    _wdays   = [_cur_mon + _td(days=i) for i in range(5)]
                    _wstrs   = [d.strftime("%Y-%m-%d") for d in _wdays]
                    _wlabel  = f"{_wdays[0].strftime('%Y. %m/%d')} ~ {_wdays[4].strftime('%m/%d')}"
                    st.markdown(f"**📆 {_wlabel}**")
                    _dcols = st.columns(5)

                    for _ci, (_dcol, _ds, _do) in enumerate(zip(_dcols, _wstrs, _wdays)):
                        with _dcol:
                            _is_past = _ds < _today_str
                            _dlabel  = f"{'~~' if _is_past else ''}**{_do.strftime('%m/%d')} ({_day_names[_ci]})**{'~~' if _is_past else ''}"
                            st.markdown(_dlabel)
                            _cos = _cal.get(_ds, [])
                            for _co in _cos[:10]:
                                _tk    = _co.get("symbol", "").strip()
                                _nm    = _co.get("name", _tk)
                                _short = (_nm[:16] + "…") if len(_nm) > 16 else _nm
                                if st.button(_short, key=f"cal_{_tk}_{_ds}", use_container_width=True,
                                             help=f"{_tk} · EPS 예상: {_co.get('estimate','N/A')}"):
                                    st.session_state.mcp_cal_selected = {
                                        "ticker": _tk, "name": _nm,
                                        "estimate": _co.get("estimate", "N/A"),
                                        "fiscal":   _co.get("fiscalDateEnding", ""),
                                        "date":     _ds,
                                    }
                            if len(_cos) > 10:
                                st.caption(f"+{len(_cos)-10}개 더")

                    st.write("---")

                # ── 선택된 기업 상세 패널 ──────────────────────────
                _sel = st.session_state.mcp_cal_selected
                if _sel:
                    _tk = _sel["ticker"]
                    _nm = _sel["name"]

                    # 데이터 자동 로드 (캐시 없을 때만)
                    if _tk not in st.session_state.mcp_cal_data:
                        with st.spinner(f"📡 {_tk} 데이터 로딩 중..."):
                            _ov   = av_get({"function": "OVERVIEW",  "symbol": _tk})
                            _ear  = av_get({"function": "EARNINGS",  "symbol": _tk})
                            _news = av_get({"function": "NEWS_SENTIMENT", "tickers": _tk,
                                            "sort": "RELEVANCE", "limit": "10"})
                            # Gemini AI 브리핑 (OVERVIEW 데이터 활용)
                            _desc = _ov.get("Description", "")
                            _bp = f"""미국 상장 기업 {_tk} ({_nm}) 어닝 브리핑을 한국어로 작성해줘.

기업 정보:
- 섹터: {_ov.get('Sector','N/A')} / 업종: {_ov.get('Industry','N/A')}
- 시가총액: {_ov.get('MarketCapitalization','N/A')} / P/E: {_ov.get('PERatio','N/A')} / EPS: {_ov.get('EPS','N/A')}
- 52주 범위: {_ov.get('52WeekLow','N/A')} ~ {_ov.get('52WeekHigh','N/A')}
- 애널리스트 목표가: {_ov.get('AnalystTargetPrice','N/A')}
- 기업 설명: {_desc[:800] if _desc else 'N/A'}

다음 항목으로 작성해:
1. **핵심 비즈니스 요약** (2~3문장)
2. **이번 실적 주목 포인트** (EPS 달성 여부, 매출 성장률, 마진, 가이던스)
3. **시장이 주시하는 리스크** (2~3가지)
4. **밸류에이션 & 투자 성격** (성장주/가치주/배당주, 현재 PER 수준 평가)"""
                            try:
                                _br = client.models.generate_content(model="gemini-2.0-flash", contents=_bp)
                                _brief_text = _br.text
                            except Exception as _e:
                                _brief_text = f"생성 실패: {_e}"
                            st.session_state.mcp_cal_data[_tk] = {
                                "overview": _ov, "earnings": _ear,
                                "news": _news,   "brief": _brief_text,
                            }

                    _cd   = st.session_state.mcp_cal_data.get(_tk, {})
                    _ov   = _cd.get("overview", {})
                    _ear  = _cd.get("earnings", {})
                    _news = _cd.get("news", {})

                    # 헤더
                    _ch, _cx = st.columns([8, 1])
                    with _ch:
                        _sec = _ov.get("Sector", "")
                        _ind = _ov.get("Industry", "")
                        st.markdown(f"#### 🔍 {_nm} &nbsp; `{_tk}`")
                        if _sec:
                            st.caption(f"📌 {_sec}  ·  {_ind}")
                    with _cx:
                        if st.button("✕ 닫기", key="cal_close"):
                            st.session_state.mcp_cal_selected = None
                            st.rerun()
                    st.markdown(
                        f"📅 **발표일**: {_sel['date']} &nbsp;|&nbsp; "
                        f"📊 **회계기간**: {_sel['fiscal']} &nbsp;|&nbsp; "
                        f"💰 **이번 분기 EPS 예상**: **{_sel['estimate']}**"
                    )

                    _tab1, _tab2, _tab3, _tab4 = st.tabs(
                        ["🏢 기업 개요", "📊 분기 실적 이력", "🧠 AI 어닝 브리핑", "📰 관련 뉴스 (Relevance)"]
                    )

                    # ── Tab 1: 기업 개요 ──
                    with _tab1:
                        if _ov.get("Symbol"):
                            def _fmt_mc(v):
                                try:
                                    v = int(v)
                                    return f"${v/1e9:.1f}B" if v >= 1e9 else f"${v/1e6:.0f}M"
                                except Exception:
                                    return str(v)
                            _r1 = st.columns(4)
                            _r1[0].metric("시가총액",      _fmt_mc(_ov.get("MarketCapitalization","N/A")))
                            _r1[1].metric("P/E (TTM)",    _ov.get("PERatio","N/A"))
                            _r1[2].metric("EPS (TTM)",    f"${_ov.get('EPS','N/A')}")
                            try:
                                _dy = f"{float(_ov.get('DividendYield','0') or 0)*100:.2f}%"
                            except Exception:
                                _dy = "N/A"
                            _r1[3].metric("배당수익률",    _dy)
                            _r2 = st.columns(4)
                            _r2[0].metric("52주 최고",    f"${_ov.get('52WeekHigh','N/A')}")
                            _r2[1].metric("52주 최저",    f"${_ov.get('52WeekLow','N/A')}")
                            _r2[2].metric("목표주가",      f"${_ov.get('AnalystTargetPrice','N/A')}")
                            _r2[3].metric("베타",          _ov.get("Beta","N/A"))
                            _r3 = st.columns(4)
                            _r3[0].metric("전분기 EPS",   f"${_ov.get('EPS','N/A')}")
                            _r3[1].metric("매출(TTM)",    _fmt_mc(_ov.get("RevenueTTM","N/A")))
                            _r3[2].metric("영업이익률",   f"{_ov.get('OperatingMarginTTM','N/A')}")
                            _r3[3].metric("ROE",          f"{_ov.get('ReturnOnEquityTTM','N/A')}")
                            _desc = _ov.get("Description","")
                            if _desc:
                                st.markdown("---")
                                st.write(_desc)
                        else:
                            st.info("기업 개요 데이터를 불러올 수 없습니다.")

                    # ── Tab 2: 분기 실적 이력 ──
                    with _tab2:
                        _qtrs = _ear.get("quarterlyEarnings", [])[:8]
                        if _qtrs:
                            _erows = []
                            for _q in _qtrs:
                                _surp = _q.get("surprisePercentage","")
                                try:
                                    _surp = f"{float(_surp):+.2f}%"
                                except Exception:
                                    pass
                                _erows.append({
                                    "분기":      _q.get("fiscalDateEnding",""),
                                    "발표일":    _q.get("reportedDate",""),
                                    "예상 EPS":  _q.get("estimatedEPS","N/A"),
                                    "실제 EPS":  _q.get("reportedEPS","N/A"),
                                    "서프라이즈": _surp,
                                })
                            st.dataframe(_erows, use_container_width=True)
                            _annual = _ear.get("annualEarnings", [])[:4]
                            if _annual:
                                st.markdown("**연간 EPS 이력**")
                                _arows = [{"연도": a.get("fiscalDateEnding",""), "EPS": a.get("reportedEPS","N/A")} for a in _annual]
                                st.dataframe(_arows, use_container_width=True)
                        else:
                            st.info("실적 이력 데이터를 불러올 수 없습니다.")

                    # ── Tab 3: AI 어닝 브리핑 ──
                    with _tab3:
                        _brief = _cd.get("brief","")
                        if _brief:
                            st.write(_brief)
                        else:
                            st.info("AI 브리핑 데이터가 없습니다.")

                    # ── Tab 4: 관련 뉴스 ──
                    with _tab4:
                        _feed = _news.get("feed", [])
                        if not _feed:
                            st.info("관련 뉴스를 찾을 수 없습니다.")
                        else:
                            _sent_icon = {
                                "Bullish": "🟢", "Somewhat-Bullish": "🟡",
                                "Neutral": "⚪", "Somewhat-Bearish": "🟠", "Bearish": "🔴"
                            }

                            # 감성 아이콘 범례
                            st.markdown(
                                "🟢 **강세** &nbsp;·&nbsp; 🟡 **약강세** &nbsp;·&nbsp; "
                                "⚪ **중립** &nbsp;·&nbsp; 🟠 **약약세** &nbsp;·&nbsp; 🔴 **약세**  \n"
                                "<small>감성 스코어: -1.0(극약세) ~ 0(중립) ~ +1.0(극강세)</small>",
                                unsafe_allow_html=True
                            )

                            # 한국어 번역 버튼
                            _kr_key = f"news_kr_{_tk}"
                            _is_kr  = _kr_key in st.session_state.mcp_cal_data.get(_tk, {})
                            _btn_label = "🌐 영어로 보기" if _is_kr else "🇰🇷 한국어로 보기"
                            if st.button(_btn_label, key=f"cal_news_kr_{_tk}"):
                                if _is_kr:
                                    # 영어로 되돌리기
                                    st.session_state.mcp_cal_data[_tk].pop("news_kr", None)
                                    st.rerun()
                                else:
                                    _titles = [a.get("title","") for a in _feed[:10]]
                                    with st.spinner("헤드라인 번역 중..."):
                                        _tr_prompt = (
                                            "아래 영어 뉴스 헤드라인을 각각 자연스러운 한국어로 번역해줘.\n"
                                            "번호 순서 그대로, 한 줄에 하나씩만 출력하고 부가 설명은 쓰지 마.\n\n"
                                            + "\n".join(f"{i+1}. {t}" for i, t in enumerate(_titles))
                                        )
                                        try:
                                            _tr_resp = client.models.generate_content(
                                                model="gemini-2.0-flash", contents=_tr_prompt
                                            )
                                            _tr_lines = [
                                                l.strip().lstrip("0123456789.").strip()
                                                for l in _tr_resp.text.strip().splitlines()
                                                if l.strip()
                                            ]
                                            st.session_state.mcp_cal_data[_tk]["news_kr"] = _tr_lines
                                        except Exception as _te:
                                            st.error(f"번역 실패: {_te}")
                                    st.rerun()

                            st.write("")
                            _kr_titles = st.session_state.mcp_cal_data.get(_tk, {}).get("news_kr", [])

                            for _i, _art in enumerate(_feed[:10]):
                                _title = _kr_titles[_i] if _kr_titles and _i < len(_kr_titles) else _art.get("title","")
                                _url   = _art.get("url","")
                                _src   = _art.get("source","")
                                _tp    = _art.get("time_published","")[:8]
                                try:
                                    from datetime import datetime as _dt3
                                    _tp = _dt3.strptime(_tp, "%Y%m%d").strftime("%m/%d")
                                except Exception:
                                    pass
                                _sent  = _art.get("overall_sentiment_label","Neutral")
                                _icon  = _sent_icon.get(_sent, "⚪")
                                _score = _art.get("overall_sentiment_score","")
                                try:
                                    _score = f"{float(_score):+.3f}"
                                except Exception:
                                    _score = ""
                                st.markdown(
                                    f"{_icon} **[{_title}]({_url})**  \n"
                                    f"`{_src}` · {_tp} · {_sent} `{_score}`"
                                )
                                st.write("---")

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

    import json as _json, re as _re

    def _resolve_ticker(query):
        """기업명 또는 티커를 AI로 변환. 반환: {status, ticker, name} 또는 {status, candidates:[{ticker,name}]}"""
        prompt = f"""주식 투자 앱에서 사용자가 다음을 입력했습니다: "{query}"

미국 주식 티커를 찾아주세요. 반드시 아래 JSON 형식 중 하나로만 응답하세요 (마크다운·설명 없이 순수 JSON만):

명확한 경우:
{{"status":"found","ticker":"AAPL","name":"Apple Inc."}}

여러 후보가 있는 경우 (예: 구글→GOOGL/GOOG, 버크셔→BRK-A/BRK-B, 알파벳 주식 클래스 등):
{{"status":"ambiguous","candidates":[{{"ticker":"BRK-A","name":"버크셔 해서웨이 클래스 A"}},{{"ticker":"BRK-B","name":"버크셔 해서웨이 클래스 B"}}]}}

찾을 수 없는 경우:
{{"status":"not_found","message":"해당 기업을 찾을 수 없습니다"}}"""
        try:
            resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
            text = resp.text.strip()
            m = _re.search(r'\{.*\}', text, _re.DOTALL)
            if m:
                return _json.loads(m.group())
        except Exception:
            pass
        return {"status": "not_found", "message": "티커 변환에 실패했습니다."}

    def _detect_quarter(ticker):
        """최신 분기 정보만 탐지 (quarter, fiscal_date, reported_date)"""
        from datetime import datetime as _dt
        earnings_data = av_get({"function": "EARNINGS", "symbol": ticker})
        quarterly = earnings_data.get("quarterlyEarnings", [])
        if not quarterly:
            return None
        q0 = quarterly[0]
        date_str = q0.get("fiscalDateEnding", "")
        reported_date = q0.get("reportedDate", "")
        try:
            d = _dt.strptime(date_str, "%Y-%m-%d")
            latest_quarter = f"{d.year}Q{(d.month - 1) // 3 + 1}"
        except Exception:
            return None
        return {"quarter": latest_quarter, "fiscal_date": date_str, "reported_date": reported_date}

    def _fetch_transcript(ticker, quarter_info):
        """어닝콜 트랜스크립트 AI 요약 (분기 정보는 이미 탐지된 것 사용)"""
        latest_quarter = quarter_info["quarter"]
        result = av_get({"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker, "quarter": latest_quarter})
        cache_key = f"{ticker}_{latest_quarter}"
        if "Information" in result:
            st.warning(result["Information"]); return
        if "error" in result:
            st.error(result["error"]); return
        transcript_text = result.get("transcript", "")
        if not transcript_text:
            for v in result.values():
                if isinstance(v, str) and len(v) > 500:
                    transcript_text = v; break
        if not transcript_text:
            st.warning(f"{latest_quarter} 트랜스크립트 데이터를 찾을 수 없습니다."); return
        prompt = f"""다음은 {ticker}의 {latest_quarter} 어닝콜 트랜스크립트입니다.
한국어로 핵심 내용을 아래 형식으로 요약해 주세요. 전체 요약은 6~7문단으로 작성하세요.

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
            "ticker": ticker, "quarter": latest_quarter,
            "summary": summary, "raw_preview": transcript_text[:500],
            "full_text": transcript_text,
            "searches": {},
        }

    # ── 입력 UI ──
    col_t1, col_t2 = st.columns([4, 1])
    with col_t1:
        tr_query = st.text_input("종목 티커 또는 기업명", placeholder="예: AAPL · 애플 · 버크셔 해서웨이 · 구글",
                                 key="mcp_transcript_ticker", label_visibility="collapsed")
    with col_t2:
        tr_btn = st.button("🧠 AI 요약", key="mcp_transcript_btn", use_container_width=True)

    # ── 수동 트랜스크립트 입력 ──
    with st.expander("✏️ 트랜스크립트 직접 붙여넣기 (선택)", expanded=False):
        st.caption("인터넷에서 가져온 어닝콜 트랜스크립트를 직접 붙여넣으면 AI 요약에 사용됩니다. 티커와 분기를 위에서 입력하세요.")
        _manual_text = st.text_area(
            "트랜스크립트 원문",
            height=200,
            placeholder="트랜스크립트 전문을 여기에 붙여넣으세요...",
            key="mcp_manual_transcript",
            label_visibility="collapsed",
        )
        _manual_quarter = st.text_input(
            "분기 (예: 2025Q4)",
            placeholder="예: 2025Q4",
            key="mcp_manual_quarter",
        )
        _manual_btn = st.button("🧠 수동 트랜스크립트 AI 요약", key="mcp_manual_btn", use_container_width=True)

    if _manual_btn:
        if not tr_query.strip():
            st.warning("티커를 위 입력창에 먼저 입력해 주세요.")
        elif not _manual_text.strip():
            st.warning("트랜스크립트를 붙여넣어 주세요.")
        else:
            _man_quarter = _manual_quarter.strip() or "수동입력"
            _man_ticker  = tr_query.strip().upper()
            _cache_key   = f"{_man_ticker}_{_man_quarter}_manual"
            st.info(f"📅 **{_man_quarter} 어닝콜 (수동 입력)**  ·  티커: {_man_ticker}")
            with st.spinner(f"🧠 {_man_ticker} {_man_quarter} 트랜스크립트 AI 요약 중..."):
                _man_prompt = f"""다음은 {_man_ticker}의 {_man_quarter} 어닝콜 트랜스크립트입니다.
한국어로 핵심 내용을 아래 형식으로 요약해 주세요. 전체 요약은 6~7문단으로 작성하세요.

1. **실적 요약**: 매출, 순이익, EPS 주요 수치
2. **경영진 핵심 발언**: CEO/CFO의 중요 발언 3~5가지
3. **가이던스**: 다음 분기/연간 전망
4. **주요 리스크**: 경영진이 언급한 위험 요소
5. **투자 시사점**: 종합적인 투자 관점에서의 시사점

트랜스크립트:
{_manual_text[:15000]}"""
                try:
                    _man_resp = client.models.generate_content(model="gemini-2.0-flash", contents=_man_prompt)
                    _man_summary = _man_resp.text
                except Exception as _me:
                    _man_summary = f"요약 생성 실패: {_me}"
            st.session_state.mcp_transcript[_cache_key] = {
                "ticker": _man_ticker, "quarter": _man_quarter,
                "summary": _man_summary, "raw_preview": _manual_text[:500],
                "full_text": _manual_text, "searches": {},
            }

    if tr_btn:
        if tr_query:
            st.session_state.mcp_tr_candidates = None
            st.session_state.mcp_tr_query = tr_query.strip()
            with st.spinner("티커 확인 중..."):
                resolved = _resolve_ticker(tr_query.strip())
            if resolved["status"] == "found":
                _tk = resolved["ticker"]
                with st.spinner(f"{_tk} 최신 분기 확인 중..."):
                    _qinfo = _detect_quarter(_tk)
                if not _qinfo:
                    st.warning("최신 분기 정보를 가져올 수 없습니다. 티커를 확인해 주세요.")
                else:
                    st.info(
                        f"📅 **{_qinfo['quarter']} 어닝콜**  ·  "
                        f"회계연도 마감: {_qinfo['fiscal_date']}  ·  "
                        f"실적 발표일: {_qinfo['reported_date']}"
                    )
                    with st.spinner(f"🧠 {_tk} {_qinfo['quarter']} 트랜스크립트 AI 요약 중..."):
                        _fetch_transcript(_tk, _qinfo)
            elif resolved["status"] == "ambiguous":
                st.session_state.mcp_tr_candidates = resolved.get("candidates", [])
            else:
                st.warning(resolved.get("message", "종목을 찾을 수 없습니다."))
        else:
            st.warning("티커 또는 기업명을 입력해 주세요.")

    # ── 모호한 경우: 후보 선택 UI ──
    if st.session_state.mcp_tr_candidates:
        candidates = st.session_state.mcp_tr_candidates
        st.info(f"**'{st.session_state.mcp_tr_query}'** 에 해당하는 종목이 여러 개입니다. 어떤 종목을 찾으셨나요?")
        options = [f"{c['name']}  ({c['ticker']})" for c in candidates]
        sel_idx = st.radio("종목 선택", range(len(options)), format_func=lambda i: options[i], key="mcp_tr_radio")
        if st.button("✅ 이 종목으로 요약", key="mcp_tr_confirm", use_container_width=False):
            selected_ticker = candidates[sel_idx]["ticker"]
            st.session_state.mcp_tr_candidates = None
            with st.spinner(f"{selected_ticker} 최신 분기 확인 중..."):
                _qinfo2 = _detect_quarter(selected_ticker)
            if not _qinfo2:
                st.warning("최신 분기 정보를 가져올 수 없습니다.")
            else:
                st.info(
                    f"📅 **{_qinfo2['quarter']} 어닝콜**  ·  "
                    f"회계연도 마감: {_qinfo2['fiscal_date']}  ·  "
                    f"실적 발표일: {_qinfo2['reported_date']}"
                )
                with st.spinner(f"🧠 {selected_ticker} {_qinfo2['quarter']} 트랜스크립트 AI 요약 중..."):
                    _fetch_transcript(selected_ticker, _qinfo2)

    # ── 결과 표시 ──
    if st.session_state.mcp_transcript:
        for _tkey, _titem in st.session_state.mcp_transcript.items():
            st.success(f"🎯 **{_titem['ticker']} {_titem['quarter']} 어닝콜 요약 완료**")
            st.write(_titem["summary"])
            with st.expander("원문 미리보기 (첫 500자)", expanded=False):
                st.text(_titem.get("raw_preview", ""))

            # ── 트랜스크립트 내 검색 ──────────────────────────
            st.markdown("**🔍 트랜스크립트 내 검색**")
            _sq_col, _sb_col = st.columns([5, 1])
            with _sq_col:
                _search_q = st.text_input(
                    "검색어", placeholder="예: 마진 가이던스, AI 투자 계획, 중국 매출, buyback...",
                    key=f"tr_search_q_{_tkey}", label_visibility="collapsed"
                )
            with _sb_col:
                _search_btn = st.button("🔍 검색", key=f"tr_search_btn_{_tkey}", use_container_width=True)

            if _search_btn and _search_q:
                _full = _titem.get("full_text", "")
                if not _full:
                    st.warning("전문 데이터가 없습니다. 요약을 다시 실행해 주세요.")
                else:
                    with st.spinner(f"'{_search_q}' 검색 중..."):
                        _sp = f"""다음은 {_titem['ticker']}의 {_titem['quarter']} 어닝콜 트랜스크립트입니다.

트랜스크립트에서 아래 주제와 관련된 내용을 모두 찾아주세요: "{_search_q}"

결과를 아래 형식으로 작성해주세요. 반드시 트랜스크립트의 정확한 원문을 인용하세요:

**[관련 발언 N]**
> "원문 그대로 인용 (영어 그대로)"

**맥락 설명 (한국어):** 이 발언의 배경과 의미를 2~3문장으로 설명

---

관련 내용이 전혀 없으면 "해당 내용을 트랜스크립트에서 찾을 수 없습니다."라고만 답하세요.

트랜스크립트:
{_full[:20000]}"""
                        try:
                            _sr = client.models.generate_content(model="gemini-2.0-flash", contents=_sp)
                            _titem["searches"][_search_q] = _sr.text
                        except Exception as _se:
                            _titem["searches"][_search_q] = f"검색 실패: {_se}"

            # 검색 결과 표시
            for _sq, _sr in _titem.get("searches", {}).items():
                with st.expander(f"🔎 검색 결과: \"{_sq}\"", expanded=True):
                    st.write(_sr)

            st.write("---")
    else:
        st.caption("티커(AAPL) 또는 기업명(애플, 구글 등)을 입력하면 최신 어닝콜을 AI로 요약합니다.")


# ==========================================
# 📌 메인 앱 렌더링
# ==========================================
def main():
    st.set_page_config(page_title="News Prism V10.39", page_icon="💎", layout="wide")

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
                    <h1 style="margin: 0; padding: 0; line-height: 1.2;">가나디의 신문배달</h1>
                </div>
                """, unsafe_allow_html=True
            )
    else:
        st.title("💎 가나디의 신문배달")
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

    tab_mcp, tab_news, tab_alpha, tab_yt = st.tabs(["📊 MCP 마켓 대시보드", "📰 일반 뉴스 브리핑", "📈 Alpha Vantage 프리미엄", "📺 유튜브 인사이트"])

    with tab_mcp:
        render_tab_mcp_fragment()

    with tab_news:
        render_tab_news_fragment(target_keywords, user_interest, default_keywords)

    with tab_alpha:
        render_tab_alpha_fragment(target_keywords, user_interest, default_keywords)

    with tab_yt:
        render_tab_youtube_fragment()

if __name__ == "__main__":
    main()



