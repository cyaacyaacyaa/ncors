#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポートアイランドドライビングスクール 教習予約の空き枠チェック → Discord通知

必要な環境変数:
  NCORS_USERID        教習生番号 (例: F38700)
  NCORS_PASSWORD       パスワード
  DISCORD_WEBHOOK_URL  DiscordのWebhook URL

使い方:
  python3 check_reservation.py

定期実行するには cron や GitHub Actions などで一定間隔ごとに実行してください。
"""

import os
import json
import sys
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://dk.ncors.com/pic/ncors/"
LOGIN_URL = BASE_URL + "login.asp"
CARTYPE_ACTION = BASE_URL + "cartype.asp"
RESERVE_ACTION = BASE_URL + "ReserveList.asp"

# 車種コード: 002=AT, 017=セット, 401=複数, 027=高速, 091=応急
CARTYPE = os.environ.get("NCORS_CARTYPE", "002")

USERID = os.environ.get("NCORS_USERID")
USERPASSWD = os.environ.get("NCORS_PASSWORD")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

STATE_FILE = os.environ.get("NCORS_STATE_FILE", "notified_slots.json")

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=Shift_JIS",
    "User-Agent": "Mozilla/5.0",
}


def encode_cp932(data: dict) -> bytes:
    """フォームデータをShift_JIS(cp932)でURLエンコードしてbytesにする"""
    parts = []
    for k, v in data.items():
        parts.append(f"{quote(str(k))}={quote(str(v), encoding='cp932')}")
    return "&".join(parts).encode("ascii")


def get_html(resp: requests.Response) -> str:
    resp.encoding = "cp932"
    return resp.text


def get_hidden(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("input", {"name": name})
    return tag["value"] if tag and tag.has_attr("value") else ""


def login_and_get_reservelist(session: requests.Session) -> str:
    # 1. ログインページを開いて初期COOKIE値を取得
    r = session.get(LOGIN_URL)
    soup = BeautifulSoup(get_html(r), "html.parser")
    initial_cookie = get_hidden(soup, "COOKIE")

    # 2. ログインPOST -> cartype.asp (メニュー選択ページ)
    login_data = {
        "USERID": USERID,
        "USERPASSWD": USERPASSWD,
        "COOKIE": initial_cookie,
    }
    r2 = session.post(CARTYPE_ACTION, data=encode_cp932(login_data), headers=HEADERS)
    soup2 = BeautifulSoup(get_html(r2), "html.parser")

    username = get_hidden(soup2, "USERNAME")
    userstep = get_hidden(soup2, "USERSTEP")
    userdata = get_hidden(soup2, "USERDATA")
    cookie2 = get_hidden(soup2, "COOKIE")

    if not cookie2:
        raise RuntimeError(
            "ログインに失敗した可能性があります。教習生番号・パスワードを確認してください。"
        )

    # 3. 車種選択POST -> ReserveList.asp (空き状況ページ)
    reserve_data = {
        "USERNAME": username,
        "USERSTEP": userstep,
        "USERDATA": userdata,
        "COOKIE": cookie2,
        "CARTYPE": CARTYPE,
    }
    r3 = session.post(RESERVE_ACTION, data=encode_cp932(reserve_data), headers=HEADERS)
    return get_html(r3)


def parse_free_slots(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "taReserve01"})
    if not table:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = rows[0].find_all("th")
    times = [th.get_text(strip=True) for th in header_cells[1:]]

    free_slots = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if not cells:
            continue
        date_label = cells[0].get_text(strip=True)
        for i, cell in enumerate(cells[1:]):
            classes = cell.get("class") or []
            if "Free" in classes:
                slot_id = cell.get("id", "")
                time_label = times[i] if i < len(times) else "?"
                free_slots.append(f"{date_label} {time_label} (id={slot_id})")
    return free_slots


def load_notified():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_notified(slots):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(slots), f, ensure_ascii=False)


def notify_discord(new_slots):
    content = "🚗 教習予約に空きが出ました！\n" + "\n".join(new_slots)
    r = requests.post(DISCORD_WEBHOOK, json={"content": content})
    r.raise_for_status()


def main():
    missing = [
        name
        for name, val in [
            ("NCORS_USERID", USERID),
            ("NCORS_PASSWORD", USERPASSWD),
            ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK),
        ]
        if not val
    ]
    if missing:
        print(f"環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    html = login_and_get_reservelist(session)
    free_slots = parse_free_slots(html)

    notified = load_notified()
    current = set(free_slots)
    new_slots = current - notified

    if new_slots:
        print(f"新しい空き枠 {len(new_slots)} 件を通知します。")
        notify_discord(sorted(new_slots))
    else:
        print("新しい空き枠なし。")

    save_notified(current)


if __name__ == "__main__":
    main()
