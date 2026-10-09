#!/usr/bin/env python3
"""公開されているEventernoteの参加イベントをすべて取得し、events.json を生成する。

【確実に全件取る仕組み】
  1. 参加イベント一覧の1ページ目から読み始め、ページ下部の「1 2 3 … >」リンクを最後まで辿る。
  2. イベンターノートが画面に表示している「参加イベント一覧(N件)」の N と、集めた件数を突き合わせる。
  3. 件数が一致したときだけ events.json を書き換える。一致しなければ書き換えずにエラー終了する
     (GitHub Actions では赤い×になり、通知メールが届く)。今の events.json は壊れない。

  サイトの仕様変更などで、どうしても一致しないまま保存したいときだけ、環境変数 ALLOW_MISMATCH=1 を付ける。
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

USER = "ayu_11039"
SITE = "https://www.eventernote.com"
BASE = f"{SITE}/users/{USER}/events"
OUT = Path(__file__).with_name("events.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AyuOshiNote/1.1; public event archive)"}
MAX_REQUESTS = 80          # 1回の実行で読むページ数の上限(暴走防止)
DEFAULT_PER_PAGE = 30      # 1ページあたりの件数(イベンターノートの標準。実測値が取れればそちらを使う)

DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
TIME_RE = re.compile(r"開場\s*([^\s]+)\s*開演\s*([^\s]+)\s*終演\s*([^\s]+)")
TOTAL_RE = re.compile(r"参加イベント一覧\s*[\(（]\s*(\d+)")
PAGE_PARAM_RE = re.compile(r"[?&]page=\d+")


def text_of(node: object) -> str:
    return " ".join(node.stripped_strings) if hasattr(node, "stripped_strings") else ""


def parse_page(html: str) -> list[dict]:
    """一覧ページ1枚分のイベントを取り出す。イベント名は h4 のリンクとして表示される。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for heading in soup.find_all(["h4", "h3"]):
        link = heading.find("a", href=True)
        title = text_of(link or heading)
        href = urljoin(SITE, link["href"]) if link else ""
        if not title or not re.search(r"/events/\d+", href):
            continue
        # 見出しから親をたどり、日付・会場・時刻まで含む「1イベント分のブロック」を探す
        node, block_node, block = heading, None, ""
        for _ in range(6):
            if node.parent is None:
                break
            node = node.parent
            candidate = text_of(node)
            if DATE_RE.search(candidate) and ("会場" in candidate or "開演" in candidate):
                block_node, block = node, candidate
                break
        dm = DATE_RE.search(block)
        if not dm:
            continue
        venue_m = re.search(r"会場:\s*(.+?)(?=\s+開場|\s+開演|\s+出演者|$)", block)
        tm = TIME_RE.search(block)
        # 出演者は、ブロック内の /actors/ リンク(旧表記 /artists/ も許容)
        who: list[str] = []
        for a in block_node.find_all("a", href=True):
            label = text_of(a)
            href_a = a.get("href", "")
            if label and ("/actors/" in href_a or "/artists/" in href_a) and label not in who:
                who.append(label)
        out.append({
            "date": "-".join(dm.groups()), "title": title,
            "venue": venue_m.group(1).strip() if venue_m else "",
            "open": tm.group(1) if tm else "", "start": tm.group(2) if tm else "", "end": tm.group(3) if tm else "",
            "who": who, "url": href,
        })
    return out


def declared_total(html: str) -> int | None:
    """イベンターノートが画面に表示している「参加イベント一覧(N件)」の N。"""
    m = TOTAL_RE.search(text_of(BeautifulSoup(html, "html.parser")))
    return int(m.group(1)) if m else None


def page_links(html: str, base_url: str) -> list[str]:
    """ページ送りリンク(…events?page=2&user_id=… など)をそのままの形で拾う。"""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/users/{USER}/events" in href and PAGE_PARAM_RE.search(href):
            links.append(urljoin(base_url, href))
    return links


def row_key(row: dict) -> tuple:
    # 同じイベントページ(URL)の重複だけを排除する。昼夜公演などは別ページなので残る。
    return (row["date"], row["title"], row["venue"], row["start"], row["url"])


def fetch(session: requests.Session, url: str) -> str:
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:   # 一時的な通信エラーは少し待って再試行
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"取得に失敗しました: {url} ({last})")


def crawl(session: requests.Session) -> tuple[dict[tuple, dict], int | None]:
    """全ページを集める。戻り値は (イベント辞書, イベンターノート表示の総件数)。"""
    seen: dict[tuple, dict] = {}
    visited: set[str] = set()
    queue = [BASE]
    total: int | None = None
    per_page = DEFAULT_PER_PAGE
    user_id = ""

    def visit(url: str) -> None:
        nonlocal total, per_page, user_id
        visited.add(url)
        html = fetch(session, url)
        rows = parse_page(html)
        for row in rows:
            seen.setdefault(row_key(row), row)
        if total is None:
            total = declared_total(html)
        if len(visited) == 1 and rows:
            per_page = max(len(rows), 1)
        for link in page_links(html, url):
            if not user_id:
                user_id = (parse_qs(urlparse(link).query).get("user_id") or [""])[0]
            if link not in visited and link not in queue:
                queue.append(link)
        print(f"{url} -> {len(rows)}件 (累計{len(seen)}件 / 表示上の総数 {total if total is not None else '不明'})")
        time.sleep(0.7)

    # 1) ページ送りリンクを辿る
    while queue and len(visited) < MAX_REQUESTS:
        url = queue.pop(0)
        if url not in visited:
            visit(url)

    # 2) まだ足りなければ、page=1,2,3… を順番に直接読む(念のための保険)
    if total is not None and len(seen) < total:
        last_page = math.ceil(total / per_page) + 1
        for n in range(1, last_page + 1):
            if len(seen) >= total or len(visited) >= MAX_REQUESTS:
                break
            url = f"{BASE}?page={n}" + (f"&user_id={user_id}" if user_id else "")
            if url in visited:
                continue
            try:
                visit(url)
            except RuntimeError as e:   # 最終ページより先は存在しないことがある。そこで打ち切る
                print(f"{url} は読み込めませんでした({e})。ここで打ち切ります。")
                break
    return seen, total


def run(session: requests.Session) -> None:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    seen, total = crawl(session)
    events = sorted(seen.values(), key=lambda x: (x["date"], x["title"]), reverse=True)

    by_year: dict[str, int] = {}
    for e in events:
        by_year[e["date"][:4]] = by_year.get(e["date"][:4], 0) + 1
    for y in sorted(by_year):
        print(f"{y}: {by_year[y]}件")
    print(f"集めた件数: {len(events)}件 / イベンターノートの表示: {total if total is not None else '読み取れず'}件")

    if not events:
        sys.exit("エラー: イベントを取得できませんでした。サイト構造の変更の可能性があるため、events.json は書き換えません。")

    force = os.environ.get("ALLOW_MISMATCH") == "1"
    verified = total is not None and len(events) == total
    if not verified and not force:
        reason = "件数を読み取れませんでした" if total is None else f"件数が一致しません(集めた{len(events)}件 / 表示{total}件)"
        sys.exit(f"エラー: {reason}。取りこぼしの可能性があるため、events.json は書き換えません。"
                 "確認のうえ保存したい場合は ALLOW_MISMATCH=1 を付けて実行してください。")

    data: dict = {"updatedAt": now.strftime("%Y-%m-%d %H:%M JST"), "source": BASE}
    if total is not None:
        data["eventernoteTotal"] = total
    data["verified"] = verified
    data["events"] = events
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"保存完了: {len(events)}件 -> {OUT}" + ("(件数一致 ✓)" if verified else "(件数不一致のまま強制保存)"))


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)
    run(session)


if __name__ == "__main__":
    main()
