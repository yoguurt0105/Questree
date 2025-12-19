import requests
import json
import datetime
import time
import yaml
from bs4 import BeautifulSoup

with open('config.yaml', encoding='UTF-8') as f:
    _cfg = yaml.load(f, Loader=yaml.FullLoader)

PAPER_TRADING = _cfg.get('PAPER_TRADING', False)
PAPER_STARTING_CASH = int(_cfg.get('PAPER_STARTING_CASH', 1_000_000))

APP_KEY = _cfg.get('APP_KEY')
APP_SECRET = _cfg.get('APP_SECRET')
ACCESS_TOKEN = ""
CANO = _cfg.get('CANO')
ACNT_PRDT_CD = _cfg.get('ACNT_PRDT_CD')
DISCORD_WEBHOOK_URL = _cfg.get('DISCORD_WEBHOOK_URL')
URL_BASE = _cfg.get('URL_BASE')

if not PAPER_TRADING:
    required_realtime_fields = [APP_KEY, APP_SECRET, URL_BASE, CANO, ACNT_PRDT_CD]
    if any(value in (None, "") for value in required_realtime_fields):
        print("[INFO] 필수 계좌 정보가 없어 모의투자(PAPER_TRADING)를 자동 활성화합니다.")
        PAPER_TRADING = True

paper_portfolio = {
    "cash": PAPER_STARTING_CASH,
    "positions": {},
    "trade_log": []
}


def _to_yahoo_symbol(code):
    return code if "." in code else f"{code}.KS"

def send_message(msg):
    """디스코드 메세지 전송"""
    now = datetime.datetime.now()
    message = {"content": f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {str(msg)}"}
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, data=message, timeout=5)
        except requests.RequestException as exc:
            print(f"[Webhook Error] {exc}")
    print(message)

def get_access_token():
    """토큰 발급"""
    if PAPER_TRADING:
        return ""
    if not (APP_KEY and APP_SECRET and URL_BASE):
        raise ValueError("실거래를 위해서는 APP_KEY, APP_SECRET, URL_BASE가 필요합니다.")
    headers = {"content-type":"application/json"}
    body = {"grant_type":"client_credentials",
    "appkey":APP_KEY, 
    "appsecret":APP_SECRET}
    PATH = "oauth2/tokenP"
    URL = f"{URL_BASE}/{PATH}"
    res = requests.post(URL, headers=headers, data=json.dumps(body))
    ACCESS_TOKEN = res.json()["access_token"]
    return ACCESS_TOKEN
    
def hashkey(datas):
    """암호화"""
    if PAPER_TRADING:
        return ""
    PATH = "uapi/hashkey"
    URL = f"{URL_BASE}/{PATH}"
    headers = {
    'content-Type' : 'application/json',
    'appKey' : APP_KEY,
    'appSecret' : APP_SECRET,
    }
    res = requests.post(URL, headers=headers, data=json.dumps(datas))
    hashkey = res.json()["HASH"]
    return hashkey

def get_current_price(code="005930"):
    """현재가 조회"""
    if PAPER_TRADING:
        ticker = _to_yahoo_symbol(code)
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}"
        res = requests.get(url, timeout=5)
        result = res.json().get("quoteResponse", {}).get("result", [])
        if not result:
            raise ValueError(f"야후 시세 조회 실패: {code}")
        price = result[0].get("regularMarketPrice")
        if price is None:
            raise ValueError(f"현재가 데이터 없음: {code}")
        return int(float(price))
    PATH = "uapi/domestic-stock/v1/quotations/inquire-price"
    URL = f"{URL_BASE}/{PATH}"
    headers = {"Content-Type":"application/json", 
            "authorization": f"Bearer {ACCESS_TOKEN}",
            "appKey":APP_KEY,
            "appSecret":APP_SECRET,
            "tr_id":"FHKST01010100"}
    params = {
    "fid_cond_mrkt_div_code":"J",
    "fid_input_iscd":code,
    }
    res = requests.get(URL, headers=headers, params=params)
    return int(res.json()['output']['stck_prpr'])

def get_target_price(code="005930"):
    """변동성 돌파 전략으로 매수 목표가 조회"""
    if PAPER_TRADING:
        ticker = _to_yahoo_symbol(code)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=1d"
        res = requests.get(url, timeout=5)
        result = res.json().get("chart", {}).get("result", [])
        if not result:
            raise ValueError(f"야후 일별 데이터 조회 실패: {code}")
        quote = result[0]["indicators"]["quote"][0]
        candles = [
            (o, h, l)
            for o, h, l in zip(quote["open"], quote["high"], quote["low"])
            if None not in (o, h, l)
        ]
        if len(candles) < 2:
            raise ValueError("목표가 계산을 위한 캔들 데이터가 부족합니다.")
        today_open = candles[-1][0]
        prev_high = candles[-2][1]
        prev_low = candles[-2][2]
        target_price = today_open + (prev_high - prev_low) * 0.5
        return int(target_price)
    PATH = "uapi/domestic-stock/v1/quotations/inquire-daily-price"
    URL = f"{URL_BASE}/{PATH}"
    headers = {"Content-Type":"application/json", 
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"FHKST01010400"}
    params = {
    "fid_cond_mrkt_div_code":"J",
    "fid_input_iscd":code,
    "fid_org_adj_prc":"1",
    "fid_period_div_code":"D"
    }
    res = requests.get(URL, headers=headers, params=params)
    stck_oprc = int(res.json()['output'][0]['stck_oprc']) #오늘 시가
    stck_hgpr = int(res.json()['output'][1]['stck_hgpr']) #전일 고가
    stck_lwpr = int(res.json()['output'][1]['stck_lwpr']) #전일 저가
    target_price = stck_oprc + (stck_hgpr - stck_lwpr) * 0.5
    return target_price

def get_stock_balance():
    """주식 잔고조회"""
    if PAPER_TRADING:
        stock_dict = {}
        send_message("====모의투자 보유현황====")
        total_value = 0
        for code, qty in paper_portfolio["positions"].items():
            if qty <= 0:
                continue
            stock_dict[code] = str(qty)
            send_message(f"{code}: {qty}주")
            time.sleep(0.1)
            try:
                total_value += get_current_price(code) * qty
            except Exception as exc:
                send_message(f"[모의 시세 조회 실패] {code}: {exc}")
        send_message(f"주식 평가 금액: {total_value}원")
        send_message(f"현금 잔고: {paper_portfolio['cash']}원")
        send_message(f"총 자산: {total_value + paper_portfolio['cash']}원")
        send_message(f"=================")
        return stock_dict
    PATH = "uapi/domestic-stock/v1/trading/inquire-balance"
    URL = f"{URL_BASE}/{PATH}"
    headers = {"Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC8434R",
        "custtype":"P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }
    res = requests.get(URL, headers=headers, params=params)
    stock_list = res.json()['output1']
    evaluation = res.json()['output2']
    stock_dict = {}
    send_message(f"====주식 보유잔고====")
    for stock in stock_list:
        if int(stock['hldg_qty']) > 0:
            stock_dict[stock['pdno']] = stock['hldg_qty']
            send_message(f"{stock['prdt_name']}({stock['pdno']}): {stock['hldg_qty']}주")
            time.sleep(0.1)
    send_message(f"주식 평가 금액: {evaluation[0]['scts_evlu_amt']}원")
    time.sleep(0.1)
    send_message(f"평가 손익 합계: {evaluation[0]['evlu_pfls_smtl_amt']}원")
    time.sleep(0.1)
    send_message(f"총 평가 금액: {evaluation[0]['tot_evlu_amt']}원")
    time.sleep(0.1)
    send_message(f"=================")
    return stock_dict

def get_balance():
    """현금 잔고조회"""
    if PAPER_TRADING:
        send_message(f"모의투자 현금 잔고: {paper_portfolio['cash']}원")
        return int(paper_portfolio["cash"])
    PATH = "uapi/domestic-stock/v1/trading/inquire-psbl-order"
    URL = f"{URL_BASE}/{PATH}"
    headers = {"Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC8908R",
        "custtype":"P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": "005930",
        "ORD_UNPR": "65500",
        "ORD_DVSN": "01",
        "CMA_EVLU_AMT_ICLD_YN": "Y",
        "OVRS_ICLD_YN": "Y"
    }
    res = requests.get(URL, headers=headers, params=params)
    cash = res.json()['output']['ord_psbl_cash']
    send_message(f"주문 가능 현금 잔고: {cash}원")
    return int(cash)

def buy(code="005930", qty="1"):
    """주식 시장가 매수"""  
    if PAPER_TRADING:
        qty = int(qty)
        if qty <= 0:
            send_message("[모의투자 매수 실패] 수량이 0입니다.")
            return False
        price = get_current_price(code)
        cost = price * qty
        if cost > paper_portfolio["cash"]:
            send_message(f"[모의투자 매수 실패]{code}: 필요자금 {cost}원, 보유현금 {paper_portfolio['cash']}원")
            return False
        paper_portfolio["cash"] -= cost
        paper_portfolio["positions"][code] = paper_portfolio["positions"].get(code, 0) + qty
        paper_portfolio["trade_log"].append(
            {"side": "BUY", "code": code, "qty": qty, "price": price, "ts": datetime.datetime.now().isoformat()}
        )
        send_message(f"[모의투자 매수 체결]{code} {qty}주 @ {price}원 (잔고 {paper_portfolio['cash']}원)")
        return True
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": "0",
    }
    headers = {"Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC0802U",
        "custtype":"P",
        "hashkey" : hashkey(data)
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    if res.json()['rt_cd'] == '0':
        send_message(f"[매수 성공]{str(res.json())}")
        return True
    else:
        send_message(f"[매수 실패]{str(res.json())}")
        return False

def sell(code="005930", qty="1"):
    """주식 시장가 매도"""
    if PAPER_TRADING:
        qty = int(qty)
        held = paper_portfolio["positions"].get(code, 0)
        if qty <= 0 or held < qty:
            send_message(f"[모의투자 매도 실패]{code}: 보유 {held}주, 요청 {qty}주")
            return False
        price = get_current_price(code)
        proceeds = price * qty
        paper_portfolio["positions"][code] = held - qty
        paper_portfolio["cash"] += proceeds
        paper_portfolio["trade_log"].append(
            {"side": "SELL", "code": code, "qty": qty, "price": price, "ts": datetime.datetime.now().isoformat()}
        )
        send_message(f"[모의투자 매도 체결]{code} {qty}주 @ {price}원 (잔고 {paper_portfolio['cash']}원)")
        return True
    PATH = "uapi/domestic-stock/v1/trading/order-cash"
    URL = f"{URL_BASE}/{PATH}"
    data = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": qty,
        "ORD_UNPR": "0",
    }
    headers = {"Content-Type":"application/json", 
        "authorization":f"Bearer {ACCESS_TOKEN}",
        "appKey":APP_KEY,
        "appSecret":APP_SECRET,
        "tr_id":"TTTC0801U",
        "custtype":"P",
        "hashkey" : hashkey(data)
    }
    res = requests.post(URL, headers=headers, data=json.dumps(data))
    if res.json()['rt_cd'] == '0':
        send_message(f"[매도 성공]{str(res.json())}")
        return True
    else:
        send_message(f"[매도 실패]{str(res.json())}")
        return False

def get_crawler():
    url = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=삼성전자"  # 예시
    res = requests.get(url)          # 1) 웹페이지 HTML 받아오기
    html = res.text                  # 2) HTML 전체가 하나의 긴 문자열

    soup = BeautifulSoup(html, "html.parser")  # 3) HTML 파싱

    # 4) 제목 후보들을 찾아서 텍스트만 뽑기 (사이트 구조마다 다름)
    titles = []
    for tag in soup.select("a"):   # 단순 예시: 모든 링크 중에서
        txt = tag.get_text(strip=True)
        if len(txt) > 20:          # 너무 짧은 건 걸러냄
            titles.append(txt)

    return titles


# 자동매매 시작
try:
    ACCESS_TOKEN = get_access_token()

    symbol_list = ["005930","035720","000660","069500"] # 매수 희망 종목 리스트
    bought_list = [] # 매수 완료된 종목 리스트
    total_cash = get_balance() # 보유 현금 조회
    stock_dict = get_stock_balance() # 보유 주식 조회
    for sym in stock_dict.keys():
        bought_list.append(sym)
    target_buy_count = 3 # 매수할 종목 수
    buy_percent = 0.33 # 종목당 매수 금액 비율
    buy_amount = total_cash * buy_percent  # 종목별 주문 금액 계산
    soldout = False

    send_message("===국내 주식 자동매매 프로그램을 시작합니다===")
    while True:
        t_now = datetime.datetime.now()
        t_9 = t_now.replace(hour=9, minute=0, second=0, microsecond=0)
        t_start = t_now.replace(hour=9, minute=5, second=0, microsecond=0)
        t_sell = t_now.replace(hour=15, minute=15, second=0, microsecond=0)
        t_exit = t_now.replace(hour=15, minute=20, second=0,microsecond=0)
        today = datetime.datetime.today().weekday()
        if today == 5 or today == 6:  # 토요일이나 일요일이면 자동 종료
            send_message("주말이므로 프로그램을 종료합니다.")
            break
        if t_9 < t_now < t_start and soldout == False: # 잔여 수량 매도
            for sym, qty in stock_dict.items():
                sell(sym, qty)
            soldout == True
            bought_list = []
            stock_dict = get_stock_balance()
        if t_start < t_now < t_sell :  # AM 09:05 ~ PM 03:15 : 매수
            for sym in symbol_list:
                if len(bought_list) < target_buy_count:
                    if sym in bought_list:
                        continue
                    target_price = get_target_price(sym)
                    current_price = get_current_price(sym)
                    if target_price < current_price:
                        buy_qty = 0  # 매수할 수량 초기화
                        buy_qty = int(buy_amount // current_price)
                        if buy_qty > 0:
                            send_message(f"{sym} 목표가 달성({target_price} < {current_price}) 매수를 시도합니다.")
                            result = buy(sym, buy_qty)
                            if result:
                                soldout = False
                                bought_list.append(sym)
                                get_stock_balance()
                    time.sleep(1)
            time.sleep(1)
            if t_now.minute == 30 and t_now.second <= 5: 
                get_stock_balance()
                time.sleep(5)
        if t_sell < t_now < t_exit:  # PM 03:15 ~ PM 03:20 : 일괄 매도
            if soldout == False:
                stock_dict = get_stock_balance()
                for sym, qty in stock_dict.items():
                    sell(sym, qty)
                soldout = True
                bought_list = []
                time.sleep(1)
        if t_exit < t_now:  # PM 03:20 ~ :프로그램 종료
            send_message("프로그램을 종료합니다.")
            break
except Exception as e:
    send_message(f"[오류 발생]{e}")
    time.sleep(1)
