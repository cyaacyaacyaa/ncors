#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポートアイランドドライビングスクール 教習予約の空き枠チェック → Discord通知

【方式】
  ログインは起動時に1回だけ行い、その後は同じセッション(COOKIEトークン)のまま
  「最新の内容に更新する」相当のページ再取得(reservelist.aspへの再POST)だけを
  繰り返して監視する。

  - セッションは約5分でタイムアウトするため、更新間隔はそれより短く設定し、
    ログアウトを検知した場合は自動で再ログインする。
  - 受付時間(9:00〜21:00 JST)の間だけ動作し、それ以外の時間帯は何もせず終了する。
  - GitHub Actions等、1ジョブに実行時間の上限があるプラットフォーム向けに、
    WATCH_DURATION_MINUTES で監視を打ち切る時間を指定できる。

必要な環境変数:
  NCORS_USERID          教習生番号 (例: F38700)
  NCORS_PASSWORD         パスワード
  DISCORD_WEBHOOK_URL    DiscordのWebhook URL

任意の環境変数:
  NCORS_CARTYPE           車種コード (デフォルト: 002=AT)
  WATCH_DURATION_MINUTES  監視を継続する最大時間(分)。デフォルト355分
  WATCH_INTERVAL_SECONDS  ページ更新の間隔(秒)。デフォルト240秒(4分、セッションタイムアウト対策)
  NCORS_STATE_FILE        既知の空き枠を記録するJSONファイルパス
  BUSINESS_START_HOUR     受付開始時刻(時)。デフォルト9
  BUSINESS_END_HOUR       受付終了時刻(時)。デフォルト21

使い方:
  python3 check_reservation.py
"""

import os
import json
import sys
import time
import random
from datetime import datetime, timedelta, timezone, time as dtime
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

WATCH_DURATION_MINUTES = float(os.environ.get("WATCH_DURATION_MINUTES", "355"))
WATCH_INTERVAL_SECONDS = float(os.environ.get("WATCH_INTERVAL_SECONDS", "240"))
# ジョブのタイムアウトに引っかからないよう、終了予定時刻の少し手前で切り上げる
SAFETY_MARGIN_SECONDS = 120

JST = timezone(timedelta(hours=9))
BUSINESS_START = dtime(int(os.environ.get("BUSINESS_START_HOUR", "9")), 0)
BUSINESS_END = dtime(int(os.environ.get("BUSINESS_END_HOUR", "21")), 0)

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=Shift_JIS",
    "User-Agent": "Mozilla/5.0",
}

HIDDEN_FIELDS = ["USERNAME", "USERSTEP", "CARTYPE", "INSTRUCTOR", "USERDATA", "COOKIE"]


class SessionExpired(Exception):
    """セッションタイムアウト等でログアウトされたことを示す"""
    pass


def now_jst() -> datetime:
    return datetime.now(JST)


def in_business_hours(dt: datetime = None) -> bool:
    dt = dt or now_jst()
    return BUSINESS_START <= dt.time() < BUSINESS_END


def seconds_until_business_end(dt: datetime = None) -> float:
    dt = dt or now_jst()
    end_dt = dt.replace(hour=BUSINESS_END.hour, minute=BUSINESS_END.minute, second=0, microsecond=0)
    return max(0.0, (end_dt - dt).total_seconds())


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


def extract_state(soup: BeautifulSoup) -> dict:
    return {name: get_hidden(soup, name) for name in HIDDEN_FIELDS}


def looks_logged_out(soup: BeautifulSoup) -> bool:
    """ログインページ(USERID/USERPASSWD入力欄)に戻された = セッション切れ"""
    return soup.find("input", {"name": "USERID"}) is not None


def login(session: requests.Session) -> dict:
    """ログインを1回行い、以後のページ更新に使う状態(hidden fields)を返す"""
    r = session.get(LOGIN_URL)
    soup = BeautifulSoup(get_html(r), "html.parser")
    initial_cookie = get_hidden(soup, "COOKIE")

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

    reserve_data = {
        "USERNAME": username,
        "USERSTEP": userstep,
        "USERDATA": userdata,
        "COOKIE": cookie2,
        "CARTYPE": CARTYPE,
    }
    r3 = session.post(RESERVE_ACTION, data=encode_cp932(reserve_data), headers=HEADERS)
    soup3 = BeautifulSoup(get_html(r3), "html.parser")
    if looks_logged_out(soup3):
        raise RuntimeError("ログイン直後にログイン画面へ戻されました。ID・パスワードを確認してください。")
    return extract_state(soup3)


def reload_reservelist(session: requests.Session, state: dict) -> tuple[str, dict]:
    """ログインし直さずに、同じセッションのままページだけ再取得する。
    セッション切れを検知した場合は SessionExpired を送出する。"""
    payload = {name: state.get(name, "") for name in HIDDEN_FIELDS}
    r = session.post(RESERVE_ACTION, data=encode_cp932(payload), headers=HEADERS)
    html = get_html(r)
    soup = BeautifulSoup(html, "html.parser")

    if looks_logged_out(soup):
        raise SessionExpired("セッションがタイムアウトしました。")

    new_state = extract_state(soup)
    for name in HIDDEN_FIELDS:
        if not new_state.get(name):
            new_state[name] = state.get(name, "")
    return html, new_state


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

    if not in_business_hours():
        print(
            f"現在({now_jst().strftime('%H:%M')} JST)は受付時間外"
            f"({BUSINESS_START.strftime('%H:%M')}〜{BUSINESS_END.strftime('%H:%M')})のため、"
            "何もせず終了します。"
        )
        return

    session = requests.Session()

    print("ログイン中...")
    state = login(session)
    print("ログイン成功。監視を開始します。")

    notified = load_notified()

    start = time.monotonic()
    duration_deadline = start + WATCH_DURATION_MINUTES * 60 - SAFETY_MARGIN_SECONDS

    first_loop = True
    while True:
        if not first_loop:
            jitter = random.uniform(-0.1, 0.1) * WATCH_INTERVAL_SECONDS
            time.sleep(max(1.0, WATCH_INTERVAL_SECONDS + jitter))

        if time.monotonic() > duration_deadline:
            print("監視時間の上限に達したため終了します。")
            break

        if not in_business_hours():
            print("受付時間を過ぎたため監視を終了します。")
            break

        try:
            html, state = reload_reservelist(session, state)
        except SessionExpired:
            print("セッション切れを検知しました。再ログインします。")
            try:
                state = login(session)
            except RuntimeError as e:
                print(f"再ログイン失敗: {e}", file=sys.stderr)
                break
            first_loop = False
            continue
        except requests.RequestException as e:
            print(f"通信エラー: {e}", file=sys.stderr)
            first_loop = False
            continue

        free_slots = parse_free_slots(html)
        current = set(free_slots)
        new_slots = current - notified

        if new_slots:
            print(f"新しい空き枠 {len(new_slots)} 件を通知します。")
            try:
                notify_discord(sorted(new_slots))
            except requests.RequestException as e:
                print(f"Discord通知エラー: {e}", file=sys.stderr)
        else:
            print("新しい空き枠なし。")

        notified = current
        save_notified(notified)
        first_loop = False

    print("監視を終了しました。")


if __name__ == "__main__":
    main()
