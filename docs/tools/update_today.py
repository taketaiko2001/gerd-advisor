"""
クラウド定期ルーティン(RemoteTrigger)から実行される、本日の出品データ自動更新スクリプト。

リバイバル倉庫BID(公開の出品一覧ページ)を取得し、自社の過去購入履歴
(docs/data/gerd_data.json)と突き合わせて、過去に買ったことのある薬品が
本日出品されていれば推奨ゲルト数を出す。結果は docs/data/today.json に
{"fetched_at": "YYYY/MM/DD HH:MM", "matches": [...]} の形式で書き出す
(このファイルをスマホ向けページ docs/index.html が毎回ライブ取得して表示する)。

- 出品一覧ページ(/reneed/bid.html)はログイン不要で閲覧できる公開ページ。
  表示されるのは商品名・メーカー・使用期限・価格・数量・割引率・"入札件数"のみで、
  他の入札者の入札額(ゲルト数)は表示されない。
- ここでは自社の過去実績データのみを使って推奨値を計算する(他社の入札情報は
  取得・使用しない)。
- 本スクリプトのロジックは check_today_bid.py(手動実行用・非公開)と同一。
  推奨ゲルト数・想定差益率の算出方法は .claude/skills/gerd-suitei/SKILL.md を参照。
"""
import datetime
import json
import os
import re
import statistics as st
import time
import urllib.request

BASE = "https://www.revivaldrug.co.jp/reneed/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PAGE_SIZE = 120

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.dirname(SCRIPT_DIR)
HISTORY_PATH = os.path.join(DOCS_DIR, "data", "gerd_data.json")
OUTPUT_PATH = os.path.join(DOCS_DIR, "data", "today.json")


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_listing_html(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.select("#lqdboxs > li"):
        name_a = li.select_one(".name a")
        if not name_a:
            continue
        name = name_a.get_text(strip=True)

        def dd_after(label):
            dt = li.find("dt", string=lambda s: s and label in s)
            if not dt:
                return None
            dd = dt.find_next_sibling("dd", class_=lambda c: c and "dd02" in c)
            return dd.get_text(strip=True) if dd else None

        maker = dd_after("メーカー")
        expiry = dd_after("使用期限")
        price_raw = dd_after("販売価格")
        qty_raw = dd_after("総数量")
        bid_span = li.select_one('[id^="bid_cnt"]')
        bid_cnt = bid_span.get_text(strip=True) if bid_span else None
        disc_dd = li.select_one(".d-rate.dd02")
        disc = disc_dd.get_text(strip=True) if disc_dd else None

        price = None
        if price_raw:
            m = re.search(r"[\d,]+", price_raw)
            if m:
                price = float(m.group().replace(",", ""))
        qty = None
        if qty_raw:
            m = re.search(r"[\d,]+", qty_raw)
            if m:
                qty = float(m.group().replace(",", ""))
        disc_val = None
        if disc:
            m = re.search(r"\d+", disc)
            if m:
                disc_val = float(m.group())
        bid_val = None
        if bid_cnt:
            m = re.search(r"\d+", bid_cnt)
            if m:
                bid_val = int(m.group())

        items.append({
            "name": name, "maker": maker, "expiry": expiry,
            "price": price, "qty": qty, "disc": disc_val, "bid_cnt": bid_val,
        })
    return items


def fetch_all_pages(max_pages=30, delay=0.6):
    all_items = []
    seen_count = None
    for page in range(1, max_pages + 1):
        url = f"{BASE}bid.html?num={PAGE_SIZE}" if page == 1 else f"{BASE}bid{page}.html?num={PAGE_SIZE}"
        html = fetch(url)
        items = parse_listing_html(html)
        if not items:
            break
        all_items.extend(items)
        m = re.search(r"検索結果\s*([\d,]+)品", html)
        if m:
            seen_count = int(m.group(1).replace(",", ""))
        if seen_count is not None and len(all_items) >= seen_count:
            break
        time.sleep(delay)
    return all_items, seen_count


def load_history():
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def quantile(sorted_vals, p):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = p * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _date_key(d):
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", d) if d else None
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _parse_date(d):
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", d) if d else None
    return datetime.date(*(int(x) for x in m.groups())) if m else None


RECENT_DAYS = 365
MIN_RECENT_SAMPLE = 5


def select_with_recency(rows):
    """推奨ゲルトの算定は直近1年の実績を優先する。直近1年のサンプルが
    少ない場合のみ、日付が新しい順に過去実績を補って最低限のサンプル数を
    確保する(古いデータは"補助"であり、主役はあくまで直近1年)。
    """
    today = datetime.date.today()
    dated = [(r, _parse_date(r.get("d"))) for r in rows]
    recent = [r for r, dt in dated if dt and (today - dt).days <= RECENT_DAYS]
    if len(recent) >= MIN_RECENT_SAMPLE:
        return recent
    recent_ids = {id(r) for r in recent}
    older_sorted = sorted(
        (r for r, dt in dated if dt and id(r) not in recent_ids),
        key=lambda r: _parse_date(r.get("d")),
        reverse=True,
    )
    supplemented = recent + older_sorted[: max(0, MIN_RECENT_SAMPLE - len(recent))]
    return supplemented if supplemented else rows


def recommend(history, name, tag):
    """薬品名(+割引率タグ)ごとに、総数量1あたりのゲルト使用量の分布から
    経済/標準/堅実の3段階の推奨ゲルト量を算出する(総数量ベース)。
    あわせて、この商品固有の薬価換算レート(直近実績の 薬価換算÷総数量)と
    手数料率(直近実績の値、商品ごとにおおむね一定)も返す。これらは割引率
    タグに依らない商品固有の値のため、タグ絞り込み前の全実績から算出する。
    """
    rows = [r for r in history if r["n"] == name]
    target = [r for r in rows if r.get("disc") == tag] if tag is not None else rows
    fallback = False
    if tag is not None and not target:
        target = rows
        fallback = True

    target = select_with_recency(target)

    per_qty = sorted(r["g"] / r["tq"] for r in target if r.get("tq"))

    yakka_rate = None
    fee_rate = None
    priced_rows = [r for r in rows if r.get("tq") and r.get("y") and r.get("fr") is not None]
    if priced_rows:
        latest = max(priced_rows, key=lambda r: _date_key(r.get("d")))
        yakka_rate = latest["y"] / latest["tq"]
        fee_rate = latest["fr"] / 100

    return {
        "n_all": len(rows),
        "n_target": len(target),
        "fallback": fallback,
        "eco": (quantile(per_qty, 0.25), None),
        "std": (quantile(per_qty, 0.50), None),
        "safe": (quantile(per_qty, 0.75), None),
        "yakka_rate": yakka_rate,
        "fee_rate": fee_rate,
    }


def build_today_matches(listing, history):
    """本日の出品一覧を自社購入履歴と突き合わせ、推奨ゲルト数と想定差益率を算出する。

    推奨ゲルト数(総数量1あたりの必要ゲルト)は、割引率タグに実績が無ければ
    同一商品の他タグ実績にフォールバックして推定する(過去実績が薄いための
    近似であり、fallback フラグで示す)。

    一方、想定差益率は「割引率タグが異なると販売価格の水準がまるで違う」ため、
    他タグの実績差益率をそのまま借用すると大きく外れる。そこで差益率は必ず
    「本日実際にスクレイピングした販売価格・総数量」と、その商品固有の薬価
    換算レート(直近実績の 薬価換算÷総数量)・手数料率(直近実績の手数料率、
    商品ごとにおおむね一定)から、真の購入価格の式
    (販売価格 + 総数量×薬価換算レート×手数料率 + ゲルト×0.44)を使って
    その場で計算する。
    """
    hist_names = {r["n"] for r in history}
    matches = []
    for item in listing:
        if item["name"] not in hist_names:
            continue
        rec = recommend(history, item["name"], item["disc"])
        eco_gq, _ = rec["eco"]
        std_gq, _ = rec["std"]
        safe_gq, _ = rec["safe"]
        today_qty = item["qty"]
        today_price = item["price"]
        yakka_rate = rec["yakka_rate"]
        fee_rate = rec["fee_rate"]

        def true_profit(gq):
            if None in (gq, today_qty, today_price, yakka_rate, fee_rate):
                return None, None
            gerd = gq * today_qty
            yakka_today = yakka_rate * today_qty
            true_price = today_price + yakka_today * fee_rate + gerd * 0.44
            rate = (yakka_today - true_price) / yakka_today if yakka_today else None
            return gerd, rate

        eco_gerd, _ = true_profit(eco_gq)
        std_gerd, std_rate = true_profit(std_gq)
        safe_gerd, _ = true_profit(safe_gq)

        matches.append({
            "name": item["name"],
            "maker": item["maker"],
            "expiry": item["expiry"],
            "price": item["price"],
            "qty": item["qty"],
            "disc": item["disc"],
            "bid_cnt": item["bid_cnt"],
            "n_target": rec["n_target"],
            "fallback": rec["fallback"],
            "eco": eco_gerd,
            "std": std_gerd,
            "safe": safe_gerd,
            "std_rate": std_rate,
        })
    return matches


def main():
    print("出品一覧を取得中(公開ページ・ログイン不要)...")
    listing, total = fetch_all_pages()
    print(f"取得件数: {len(listing)} / 表示件数: {total}")

    history = load_history()
    matches = build_today_matches(listing, history)
    print(f"過去購入実績のある出品: {len(matches)}件 / 本日出品総数: {len(listing)}件")

    fetched_at = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": fetched_at, "matches": matches}, f, ensure_ascii=False)
    print(f"{OUTPUT_PATH} を更新しました({fetched_at})。")


if __name__ == "__main__":
    main()
