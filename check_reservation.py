#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポートアイランドドライビングスクール 教習予約の空き枠チェック → Discord通知

【方式】
  ログインは起動時に1回だけ行い、その後は同じセッション(COOKIEトークン)のまま
  予約表ページ(ReserveList.asp)への再POSTだけを繰り返して監視する。
  1回の巡回で、監視対象の車種(AT・セット・複数・高速・応急)を順番に確認する。

  - セッションは約5分でタイムアウトするため、巡回間隔はそれより短く設定し、
    ログアウトを検知した場合は自動で再ログインする。
  - 受付時間(9:00〜21:00 JST)の間だけ動作し、それ以外の時間帯は何もせず終了する。
  - GitHub Actions等、1ジョブに実行時間の上限があるプラットフォーム向けに、
    WATCH_DURATION_MINUTES で監視を打ち切る時間を指定できる。
  - 通知済みの空き枠は車種ごとに記録し、同じ枠を繰り返し通知しない。

必要な環境変数:
  NCORS_USERID          教習生番号 (例: F38700)
  NCORS_PASSWORD         パスワード
  DISCORD_WEBHOOK_URL    DiscordのWebhook URL

任意の環境変数:
  NCORS_CARTYPES          監視する車種コードをカンマ区切りで指定。
                          デフォルト: 002,017,401,027,091 (AT,セット,複数,高速,応急)
  WATCH_DURATION_MINUTES  監視を継続する最大時間(分)。デフォルト355分
  WATCH_INTERVAL_SECONDS  巡回の間隔(秒)。デフォルト240秒(4分、セッションタイムアウト対策)
  REQUEST_GAP_SECONDS     車種ごとのリクエスト間の待ち時間(秒、目安)。デフォルト2秒
  NCORS_STATE_FILE        通知済みの空き枠を記録するJSONファイルパス
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
CARTYPE_NAMES = {
    "002": "AT",
    "017": "セット",
    "401": "複数",
    "027": "高速",
    "091": "応急",
}
_cartypes_env = os.environ.get("NCORS_CARTYPES", "002,017,401,027,091")
CARTYPES = [c.strip() for c in _cartypes_env.split(",") if c.strip()]

USERID = os.environ.get("NCORS_USERID")
USERPASSWD = os.environ.get("NCORS_PASSWORD")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")

STATE_FILE = os.environ.get("NCORS_STATE_FILE", "notified_slots.json")

WATCH_DURATION_MINUTES = float(os.environ.get("WATCH_DURATION_MINUTES", "355"))
WATCH_INTERVAL_SECONDS = float(os.environ.get("WATCH_INTERVAL_SECONDS", "240"))
REQUEST_GAP_SECONDS = float(os.environ.get("REQUEST_GAP_SECONDS", "2"))
# ジョブのタイムアウトに引っかからないよう、終了予定時刻の少し手前で切り上げる
SAFETY_MARGIN_SECONDS = 120

JST = timezone(timedelta(hours=9))
BUSINESS_START = dtime(int(os.environ.get("BUSINESS_START_HOUR", "9")), 0)
BUSINESS_END = dtime(int(os.environ.get("BUSINESS_END_HOUR", "21")), 0)

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=Shift_JIS",
    "User-Agent": "Mozilla/5.0",
}

# セッションをまたいで引き継ぐhidden項目 (CARTYPEは車種ごとに指定するので含めない)
STATE_FIELDS = ["USERNAME", "USERSTEP", "INSTRUCTOR", "USERDATA", "COOKIE"]

# 旧バージョンの通知記録(車種名なし)はATのものとして引き継ぐ
LEGACY_CARTYPE_NAME = "AT"

DISCORD_MAX_CHARS = 1900


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
    return {name: get_hidden(soup, name) for name in STATE_FIELDS}


def looks_logged_out(soup: BeautifulSoup) -> bool:
    """ログインページ(USERID/USERPASSWD入力欄)に戻された = セッション切れ"""
    return soup.find("input", {"name": "USERID"}) is not None


def login(session: requests.Session) -> dict:
    """ログインを1回行い、以後の予約表取得に使う状態(hidden fields)を返す"""
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

    if looks_logged_out(soup2):
        raise RuntimeError(
            "ログインに失敗しました。教習生番号・パスワードを確認してください。"
        )

    state = extract_state(soup2)
    if not state.get("COOKIE"):
        raise RuntimeError(
            "ログインに失敗した可能性があります。教習生番号・パスワードを確認してください。"
        )
    return state


def fetch_reservelist(session: requests.Session, state: dict, cartype: str) -> str:
    """ログインし直さずに、指定した車種の予約表ページを取得する。
    stateはその場で更新される(COOKIEトークンが更新されるため)。
    セッション切れを検知した場合は SessionExpired を送出する。"""
    payload = {name: state[name] for name in STATE_FIELDS if state.get(name)}
    payload["CARTYPE"] = cartype

    r = session.post(RESERVE_ACTION, data=encode_cp932(payload), headers=HEADERS)
    html = get_html(r)
    soup = BeautifulSoup(html, "html.parser")

    if looks_logged_out(soup):
        raise SessionExpired("セッションがタイムアウトしました。")

    new_state = extract_state(soup)
    for name in STATE_FIELDS:
        if new_state.get(name):
            state[name] = new_state[name]
    return html


def parse_reservelist(html: str):
    """予約表から空き枠を取り出す。
    戻り値: (空き枠のリスト, 表のキャプション)。予約表が見つからなければ (None, "")。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "taReserve01"})
    if not table:
        return None, ""

    caption_tag = table.find("caption")
    caption = caption_tag.get_text(strip=True) if caption_tag else ""

    rows = table.find_all("tr")
    if not rows:
        return [], caption

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
    return free_slots, caption


def load_notified():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return set()

    result = set()
    for item in raw:
        s = str(item)
        # 旧形式(車種名なし)はATとして扱う
        result.add(s if "|" in s else f"{LEGACY_CARTYPE_NAME}|{s}")
    return result


def save_notified(slots):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(slots), f, ensure_ascii=False)


def display_slot(slot: str) -> str:
    """通知用に、内部管理用の (id=...) を取り除く"""
    return slot.split(" (id=")[0]


def build_discord_messages(new_by_type: dict) -> list:
    lines = ["🚗 教習予約に空きが出ました！"]
    for name, slots in new_by_type.items():
        lines.append(f"【{name}】")
        lines.extend(display_slot(s) for s in slots)

    messages = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > DISCORD_MAX_CHARS and current:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def notify_discord(new_by_type: dict):
    for content in build_discord_messages(new_by_type):
        r = requests.post(DISCORD_WEBHOOK, json={"content": content})
        r.raise_for_status()


def check_all_cartypes(session: requests.Session, state: dict, notified: set):
    """監視対象の全車種を順に確認する。
    notified はその場で更新される。
    戻り値: (車種名 -> 新しく空いた枠のリスト, セッション切れを検知したか)"""
    new_by_type = {}
    expired = False

    for idx, cartype in enumerate(CARTYPES):
        name = CARTYPE_NAMES.get(cartype, cartype)

        if idx > 0:
            time.sleep(max(0.5, random.uniform(0.5, 1.5) * REQUEST_GAP_SECONDS))

        try:
            html = fetch_reservelist(session, state, cartype)
        except SessionExpired:
            expired = True
            break
        except requests.RequestException as e:
            print(f"[{name}] 通信エラー: {e}", file=sys.stderr)
            continue

        free_slots, caption = parse_reservelist(html)
        if free_slots is None:
            print(
                f"[{name}] 予約表が見つかりません"
                "(この車種は現在選べない可能性があります)。スキップします。"
            )
            continue

        print(f"[{name}] 表示:「{caption or '-'}」 空き {len(free_slots)} 件")

        prefix = f"{name}|"
        current = {prefix + s for s in free_slots}
        previous = {k for k in notified if k.startswith(prefix)}
        new_keys = current - previous

        if new_keys:
            new_by_type[name] = sorted(k[len(prefix):] for k in new_keys)

        notified.difference_update(previous)
        notified.update(current)

    return new_by_type, expired


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

    if not CARTYPES:
        print("監視する車種(NCORS_CARTYPES)が空です。", file=sys.stderr)
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
    names = "・".join(CARTYPE_NAMES.get(c, c) for c in CARTYPES)
    print(f"ログイン成功。監視を開始します。(対象車種: {names})")

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

        new_by_type, expired = check_all_cartypes(session, state, notified)

        if new_by_type:
            total = sum(len(v) for v in new_by_type.values())
            print(f"新しい空き枠 {total} 件を通知します。")
            try:
                notify_discord(new_by_type)
            except requests.RequestException as e:
                print(f"Discord通知エラー: {e}", file=sys.stderr)
                # 通知できなかった枠は「通知済み」にせず、次回の巡回で再度通知を試みる
                for name, slots in new_by_type.items():
                    for s in slots:
                        notified.discard(f"{name}|{s}")
        else:
            print("新しい空き枠なし。")

        save_notified(notified)

        if expired:
            print("セッション切れを検知しました。再ログインします。")
            try:
                state = login(session)
            except RuntimeError as e:
                print(f"再ログイン失敗: {e}", file=sys.stderr)
                break

        first_loop = False

    print("監視を終了しました。")


if __name__ == "__main__":
    main()
