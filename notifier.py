#!/usr/bin/env python3
"""トヨタレンタカー「片道GO!」の新着車両を監視して ntfy でスマホに通知する。

使い方:
    NTFY_TOPIC=xxxx python notifier.py            # 通常実行（新着だけ通知）
    NTFY_TOPIC=xxxx python notifier.py --test     # テスト通知を1件送る
    python notifier.py --dry-run                  # 通知せず、結果を表示するだけ
    python notifier.py --dry-run --html file.html # 保存したHTMLで動作確認
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import NamedTuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

URL = "https://cp.toyota.jp/rentacar/"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"
USER_AGENT = "katamichi-go-notifier/1.0 (personal use; checks every 10 min)"

# サイトの data-start-area / data-return-area の番号とエリア名の対応
AREA_CODES = {
    "1": "北海道",
    "2": "東北",
    "3": "関東",
    "4": "中部",
    "5": "近畿",
    "6": "中国",
    "7": "四国",
    "8": "九州",
}
AREA_ALIASES = {"*": "*", "すべて": "*", "全国": "*", "どこでも": "*"}


@dataclass
class Car:
    start_area: str
    return_area: str
    start_shop: str
    start_place: str
    return_shop: str
    car: str
    condition: str
    period: str
    reserve_shop: str
    tel: str
    ended: bool = False  # サイトで「受付終了」の帯が付いている

    @property
    def key(self) -> str:
        raw = "|".join([self.start_shop, self.car, self.period, self.return_shop])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 取得と解析

def fetch_html(retries: int = 3) -> str:
    last_err: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"ページを取得できませんでした: {last_err}")


def _text(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _value_p(block) -> object:
    """ラベル用の <p class="label-sp"> を除いた、値の <p> を返す。"""
    if block is None:
        return None
    for p in block.find_all("p"):
        if "label-sp" not in (p.get("class") or []):
            return p
    return None


def parse_cars(html: str) -> list[Car]:
    soup = BeautifulSoup(html, "html.parser")
    # 「出発」順の一覧だけを使う（「到着」順の一覧は同じ車の並べ替え）
    ul = soup.select_one("#service-items-shop-type-start")
    if ul is None:
        return []
    cars: list[Car] = []
    for li in ul.select("li.service-item"):
        start_p = _value_p(li.select_one(".service-item__shop-start"))
        place = ""
        if start_p is not None and start_p.small is not None:
            place = _text(start_p.small).strip("（）() ")
            start_p.small.extract()
        return_p = _value_p(li.select_one(".service-item__shop-return"))
        if return_p is not None and return_p.small is not None:
            return_p.small.extract()
        tel = _text(li.select_one(".service-item__reserve-tel"))
        body = li.select_one(".service-item__body")
        # 受付終了の車は一覧に残ったまま、本体に show-entry-end クラスが付く（CSSで「受付終了」と表示）
        ended = body is not None and "show-entry-end" in (body.get("class") or [])
        cars.append(
            Car(
                start_area=AREA_CODES.get(li.get("data-start-area", ""), "不明"),
                return_area=AREA_CODES.get(li.get("data-return-area", ""), "不明"),
                start_shop=_text(start_p),
                start_place=place,
                return_shop=_text(return_p).replace(" 返却可能店舗", ""),
                car=_text(_value_p(li.select_one(".service-item__info__car-type"))).replace("　", " "),
                condition=_text(_value_p(li.select_one(".service-item__info__condition"))).replace("　", " "),
                period=_text(_value_p(li.select_one(".service-item__date"))),
                reserve_shop=_text(li.select_one(".service-item__reserve-shop")).replace("　", " "),
                tel=tel,
                ended=ended,
            )
        )
    return cars


# ---------------------------------------------------------------- 条件の判定

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県",
    "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
NO_PREF = {"", "指定なし", "*", "どこでも"}


def split_keywords(text: str) -> tuple[str, ...]:
    """'羽田, 品川　東京駅' → ('羽田', '品川', '東京駅')"""
    return tuple(k for k in re.split(r"[,、，\s/／]+", (text or "").strip()) if k)


class Route(NamedTuple):
    """監視ルート。出発エリア→返却エリア に、出発の都道府県・店舗/市区町村のキーワードで絞り込める。"""
    frm: str = "*"
    to: str = "*"
    pref: str = ""                 # 例: 東京都（空なら絞り込まない）
    kws: tuple[str, ...] = ()      # 例: ("羽田", "品川")：出発店舗名か市区町村にどれかを含む

    @property
    def label(self) -> str:
        start = "どこでも" if self.frm == "*" else self.frm
        detail = [x for x in [self.pref, "/".join(self.kws)] if x]
        if detail:
            start += "（" + "・".join(detail) + "）"
        return f"{start}→{'どこでも' if self.to == '*' else self.to}"

    def hit(self, car: "Car") -> bool:
        if self.frm != "*" and self.frm != car.start_area:
            return False
        if self.to != "*" and self.to != car.return_area:
            return False
        if self.pref and not car.start_place.startswith(self.pref):
            return False
        if self.kws:
            text = car.start_shop + " " + car.start_place
            if not any(k in text for k in self.kws):
                return False
        return True

    def to_dict(self) -> dict:
        d = {"from": self.frm, "to": self.to}
        if self.pref:
            d["pref"] = self.pref
        if self.kws:
            d["keywords"] = list(self.kws)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Route":
        kws = d.get("keywords") or ()
        if isinstance(kws, str):
            kws = split_keywords(kws)
        return make_route(d.get("from", "*"), d.get("to", "*"), d.get("pref", ""), tuple(kws))


def _area(x: str) -> str:
    x = AREA_ALIASES.get(x.strip(), x.strip())
    if x != "*" and x not in AREA_CODES.values():
        raise SystemExit(f"エリア名が正しくありません: {x}（使えるのは {', '.join(AREA_CODES.values())}, *）")
    return x


def make_route(frm: str, to: str, pref: str = "", kws: tuple[str, ...] = ()) -> Route:
    pref = "" if (pref or "").strip() in NO_PREF else pref.strip()
    if pref and pref not in PREFECTURES:
        raise SystemExit(f"都道府県名が正しくありません: {pref}")
    return Route(_area(frm), _area(to), pref, tuple(kws))


def load_routes() -> list[Route]:
    """監視するルートを読み込む。環境変数 ROUTES があれば config.json より優先（手元での確認用）。

    ROUTES の書き方: "関東>中部, 近畿>*, *>九州"
    """
    env = os.environ.get("ROUTES", "").strip()
    if env:
        routes = []
        for p in (p.strip() for p in re.split(r"[,、\n]", env) if p.strip()):
            a, _, b = p.replace("→", ">").partition(">")
            routes.append(make_route(a.strip(), b.strip() or "*"))
        return routes
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return [Route.from_dict(r) for r in cfg.get("routes", [])]


def matches(car: Car, routes: list[Route]) -> bool:
    return any(r.hit(car) for r in routes)


# ---------------------------------------------------------------- 状態の保存

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 通知

def tel_uri(tel: str) -> str:
    digits = re.sub(r"[^0-9+]", "", tel.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    return f"tel:{digits}"


def send_ntfy(title: str, message: str, *, tel: str = "", tags: list[str] | None = None,
              priority: int = 4, sequence_id: str = "", dry_run: bool = False) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload: dict = {"topic": topic, "title": title, "message": message,
                     "tags": tags or [], "priority": priority}
    if sequence_id:
        # 同じIDで送り直すと、対応アプリでは前の通知が置き換わる
        payload["sequence_id"] = sequence_id
    if tel:
        payload["click"] = tel_uri(tel)
        payload["actions"] = [{"action": "view", "label": f"電話する {tel}", "url": tel_uri(tel), "clear": False}]
    if dry_run:
        print("---- [dry-run] 通知 ----")
        print(json.dumps({k: v for k, v in payload.items() if k != "topic"}, ensure_ascii=False, indent=1))
        return
    if not topic:
        raise SystemExit("NTFY_TOPIC が設定されていません")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()


def car_message(c: Car, status: str = "") -> tuple[str, str]:
    """1台分の通知。status が「受付終了」などのときは、終了済みの表示にする。"""
    name = c.car.split(" ")[0]
    if status:
        title = f"❌ {status}｜{c.start_area}→{c.return_area}｜{name}"
    else:
        title = f"🚗 {c.start_area}→{c.return_area}｜{name}"
    lines = [
        f"出発：{c.start_shop}（{c.start_place}）" if c.start_place else f"出発：{c.start_shop}",
        f"返却：{c.return_shop}",
        f"期間：{c.period}",
        f"車種：{c.car}",
        f"条件：{c.condition}",
        f"予約：{c.reserve_shop} ☎ {c.tel}",
    ]
    if status:
        lines.insert(0, f"【{status}】{now_jst():%m/%d %H:%M} に確認しました")
    return title, "\n".join(lines)


def seq_id(key: str) -> str:
    return f"car-{key}"


# ---------------------------------------------------------------- 一覧

JST = timezone(timedelta(hours=9))
NEW_HOURS = 24          # この時間内に初めて見つかった車を「🆕」扱いにする
MAX_BYTES = 3500        # ntfy の1通あたりの上限(4096バイト)より少し小さく


def now_jst() -> datetime:
    return datetime.now(JST)


def _parse_seen_time(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # 古い形式はUTC


def is_new(c: Car, seen: dict, now: datetime) -> bool:
    rec = seen.get(c.key)
    if rec is None:
        return True
    t = _parse_seen_time(rec.get("first_seen", ""))
    return t is not None and now - t <= timedelta(hours=NEW_HOURS)


def short_period(p: str) -> str:
    """'2026年11月21日 ～ 11月24日' → '11/21〜11/24'"""
    md = re.findall(r"(\d{1,2})月(\d{1,2})日", p)
    if len(md) >= 2:
        return f"{int(md[0][0])}/{int(md[0][1])}〜{int(md[1][0])}/{int(md[1][1])}"
    return p


def _period_sort_key(c: Car):
    md = re.findall(r"(\d{1,2})月(\d{1,2})日", c.period)
    return (tuple(int(x) for x in md[0]) if md else (99, 99), c.start_shop)


def listing_line(c: Car, new: bool) -> str:
    shop = c.start_shop.split(" ", 1)[-1]          # 会社名を省いて店舗名だけ
    pref = c.start_place.split(" ")[0] if c.start_place else ""
    dest = re.sub(r"^トヨタレンタリース", "", c.return_shop)
    mark = "🆕" if new else "・"
    return (f"{mark}{short_period(c.period)} {shop}（{pref}）→ {dest}\n"
            f"　{c.car}｜{c.condition}｜☎{c.tel}")


def _chunks(lines: list[str]) -> list[str]:
    out, cur = [], ""
    for ln in lines:
        add = ln if not cur else "\n" + ln
        if cur and len((cur + add).encode("utf-8")) > MAX_BYTES:
            out.append(cur)
            cur = ln
        else:
            cur += add
    if cur:
        out.append(cur)
    return out


def send_listing(routes: list[Route], cars: list[Car], seen: dict,
                 dry_run: bool = False) -> int:
    """指定ルートの「今受付中の車」を、🆕（24時間以内の新着）を先頭にして通知する。受付終了は除く。"""
    now = now_jst()
    if not routes:
        send_ntfy("📋 一覧：ルートが登録されていません", "「ルート設定」で追加してください。", priority=2, dry_run=dry_run)
        return 0
    for route in routes:
        label = route.label
        route_cars = [c for c in cars if route.hit(c)]
        n_ended = sum(1 for c in route_cars if c.ended)
        hit = sorted((c for c in route_cars if not c.ended), key=_period_sort_key)
        new = [c for c in hit if is_new(c, seen, now)]
        old = [c for c in hit if not is_new(c, seen, now)]
        if not hit:
            extra = f"\n（受付終了の掲載 {n_ended}台は除いています）" if n_ended else ""
            send_ntfy(f"📋 {label}：受付中 0台", "このルートで今予約できる車はありません。\n新しく出たら通知します。" + extra,
                      priority=2, dry_run=dry_run)
            continue
        lines = []
        if new:
            lines.append(f"【🆕 新着 {len(new)}台（{NEW_HOURS}時間以内）】")
            lines += [listing_line(c, True) for c in new]
        if old:
            if new:
                lines.append("")
            lines.append(f"【受付中 {len(old)}台】")
            lines += [listing_line(c, False) for c in old]
        if n_ended:
            lines += ["", f"（受付終了の掲載 {n_ended}台は除いています）"]
        chunks = _chunks(lines)
        for i, body in enumerate(chunks, 1):
            page = f"（{i}/{len(chunks)}）" if len(chunks) > 1 else ""
            send_ntfy(f"📋 {label}：受付中 {len(hit)}台（🆕{len(new)}）{page}", body, priority=3, dry_run=dry_run)
        print(f"{label}: {len(hit)}台（新着 {len(new)}）を {len(chunks)} 通で送信")
    return 0


def _load_cars(html_path: str | None) -> list[Car]:
    html = Path(html_path).read_text(encoding="utf-8") if html_path else fetch_html()
    return parse_cars(html)


# ---------------------------------------------------------------- メイン

MAX_INDIVIDUAL = 8  # 1回の実行で個別通知する上限（多いときはまとめて1通に）


def run(args: argparse.Namespace) -> int:
    if args.test:
        send_ntfy("✅ 片道GO!通知のテスト",
                  "この通知が届けば設定は完了です。\n新しい車が出たらここに届きます。",
                  priority=3, dry_run=args.dry_run)
        print("テスト通知を送りました")
        return 0

    routes = load_routes()
    cars = _load_cars(args.html)
    state = load_state()
    first_run = "seen" not in state

    if not cars:
        # ページの構造が変わった・一時的に空、などの可能性。1回だけ知らせる。
        print("車両が1件も見つかりませんでした")
        if not state.get("empty_alerted"):
            send_ntfy("⚠️ 片道GO!の一覧を読み取れません",
                      "掲載が0件か、ページの構造が変わった可能性があります。\n" + URL,
                      priority=3, dry_run=args.dry_run)
            state["empty_alerted"] = True
            if not args.dry_run:
                save_state(state)
        return 0
    state.pop("empty_alerted", None)

    seen: dict = state.get("seen", {})
    current = {c.key: c for c in cars}
    available = [c for c in cars if not c.ended]

    # 通知する車：①初めて見る受付中の車 ②受付終了→受付中に戻った車
    def became_available(k: str, c: Car) -> bool:
        prev = seen.get(k)
        return not c.ended and (prev is None or prev.get("ended") is True)

    targets = [c for k, c in current.items() if became_available(k, c) and matches(c, routes)]
    reopened = {c.key for c in targets if c.key in seen}

    # 通知済みの車が「受付終了」になった／一覧から消えた → 前の通知を書き換える
    closed: list[tuple[str, dict, str]] = []
    for k, prev in seen.items():
        if not prev.get("notified") or prev.get("ended"):
            continue
        if k not in current:
            closed.append((k, prev, "掲載終了"))
        elif current[k].ended:
            closed.append((k, prev, "受付終了"))

    route_label = "、".join(r.label for r in routes) or "なし"
    print(f"掲載 {len(cars)} 件（受付中 {len(available)}）/ 通知する新着 {len(targets)} 件 / 終了に変わった通知済み {len(closed)} 件（{route_label}）")

    notified_now: set[str] = set()
    if first_run:
        hit = [c for c in available if matches(c, routes)]
        body = (f"監視ルート：{route_label}\n現在の掲載 {len(cars)} 台（うち受付中 {len(available)} 台）のうち、"
                f"条件に合う受付中の車は {len(hit)} 台です。\n以後は新しく受付が始まった車だけを通知します。")
        send_ntfy("✅ 片道GO!の監視を開始しました", body, priority=3, dry_run=args.dry_run)
    else:
        for c in targets[:MAX_INDIVIDUAL]:
            title, msg = car_message(c)
            if c.key in reopened:
                title = title.replace("🚗", "🔁", 1)
                msg = "【受付再開】\n" + msg
            send_ntfy(title, msg, tel=c.tel, sequence_id=seq_id(c.key), dry_run=args.dry_run)
            notified_now.add(c.key)
        rest = targets[MAX_INDIVIDUAL:]
        if rest:
            lines = [listing_line(c, True) for c in rest]
            for body in _chunks(lines):
                send_ntfy(f"🚗 ほかに {len(rest)} 台の新着", body, dry_run=args.dry_run)
        fields = Car.__dataclass_fields__.keys()
        for k, prev, status in closed:
            c = current.get(k) or Car(**{f: prev.get(f, "") for f in fields if f != "ended"})
            title, msg = car_message(c, status)
            send_ntfy(title, msg, priority=2, sequence_id=seq_id(k), dry_run=args.dry_run)

    # 今掲載されている車だけを記録（消えた車は忘れる）
    stamp = now_jst().isoformat(timespec="minutes")
    new_seen = {}
    for k, c in current.items():
        prev = seen.get(k, {})
        new_seen[k] = {
            **asdict(c),
            "first_seen": prev.get("first_seen", stamp),
            # 通知済みフラグ：終了になったら外す（受付再開したらまた通知されるように）
            "notified": (k in notified_now) or (bool(prev.get("notified")) and not c.ended),
        }
    state["seen"] = new_seen
    if not args.dry_run:
        save_state(state)
    return 0


def _route_label(routes: list[Route]) -> str:
    if not routes:
        return "（なし：通知は届きません）"
    return "\n".join(f"・{r.label}" for r in routes)


LIST_REGISTERED = "一覧を見る（登録中のルート）"
LIST_SELECTED = "一覧を見る（選んだエリア）"


def edit_routes(op: str, start: str, dest: str, pref: str = "", keywords: str = "",
                dry_run: bool = False, html_path: str | None = None) -> int:
    """「ルート設定・一覧」ボタンから呼ばれる。ルートの追加・削除と、一覧の通知。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    routes = [Route.from_dict(d) for d in cfg.get("routes", [])]
    r = make_route(start, dest, pref, split_keywords(keywords))
    seen = load_state().get("seen", {})

    if op in (LIST_REGISTERED, LIST_SELECTED):
        target = routes if op == LIST_REGISTERED else [r]
        return send_listing(target, _load_cars(html_path), seen, dry_run=dry_run)

    show_list = False
    if op == "追加":
        if routes == [Route()]:
            routes = []  # 初期設定（全ルート）は、最初の追加で置き換える
        if r not in routes:
            routes.append(r)
        head = f"ルートを追加しました：{r.label}"
        show_list = True
    elif op == "削除":
        if r not in routes:
            head = f"そのルートは登録されていません：{r.label}"
        else:
            routes.remove(r)
            head = f"ルートを削除しました：{r.label}"
    elif op == "この1件だけにする":
        routes = [r]
        head = f"ルートを {r.label} の1件にしました"
        show_list = True
    elif op == "確認だけ":
        head = "現在の監視ルート"
    else:
        raise SystemExit(f"不明な操作: {op}")
    cfg["routes"] = [x.to_dict() for x in routes]
    if not dry_run:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    body = "監視中のルート：\n" + _route_label(routes)
    print(head + "\n" + body)
    send_ntfy("⚙️ " + head, body, priority=2, dry_run=dry_run)
    if show_list:
        # 追加したルートで今受付中の車も一緒に知らせる
        send_listing([r], _load_cars(html_path), seen, dry_run=dry_run)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", action="store_true", help="テスト通知を送る")
    p.add_argument("--dry-run", action="store_true", help="通知も保存もせず、表示だけする")
    p.add_argument("--html", help="ネットから取らず、このHTMLファイルを使う")
    p.add_argument("--pref", default="", help="--route と一緒に：出発の都道府県（任意）")
    p.add_argument("--keywords", default="", help="--route と一緒に：出発店舗・市区町村のキーワード（任意、カンマ区切り）")
    p.add_argument("--route", nargs=3, metavar=("操作", "出発", "返却"),
                   help=f"ルート編集・一覧（操作: 追加/削除/この1件だけにする/確認だけ/{LIST_REGISTERED}/{LIST_SELECTED}）")
    a = p.parse_args()
    if a.route:
        return edit_routes(*a.route, pref=a.pref, keywords=a.keywords, dry_run=a.dry_run, html_path=a.html)
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
