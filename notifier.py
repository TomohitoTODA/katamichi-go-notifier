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
            )
        )
    return cars


# ---------------------------------------------------------------- 条件の判定

def load_routes() -> list[tuple[str, str]]:
    """監視するルートを読み込む。環境変数 ROUTES があれば config.json より優先。

    書き方: "関東>中部, 近畿>*, *>九州"
    """
    env = os.environ.get("ROUTES", "").strip()
    if env:
        pairs = [p.strip() for p in re.split(r"[,、\n]", env) if p.strip()]
        routes = []
        for p in pairs:
            a, _, b = p.replace("→", ">").partition(">")
            routes.append((a.strip(), b.strip() or "*"))
    else:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        routes = [(r.get("from", "*"), r.get("to", "*")) for r in cfg.get("routes", [])]
    norm = []
    for a, b in routes:
        a, b = AREA_ALIASES.get(a, a), AREA_ALIASES.get(b, b)
        for x in (a, b):
            if x != "*" and x not in AREA_CODES.values():
                raise SystemExit(f"エリア名が正しくありません: {x}（使えるのは {', '.join(AREA_CODES.values())}, *）")
        norm.append((a, b))
    return norm


def matches(car: Car, routes: list[tuple[str, str]]) -> bool:
    return any(
        (a == "*" or a == car.start_area) and (b == "*" or b == car.return_area)
        for a, b in routes
    )


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
              priority: int = 4, dry_run: bool = False) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload: dict = {"topic": topic, "title": title, "message": message,
                     "tags": tags or [], "priority": priority}
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


def car_message(c: Car) -> tuple[str, str]:
    title = f"🚗 {c.start_area}→{c.return_area}｜{c.car.split(' ')[0]}"
    lines = [
        f"出発：{c.start_shop}（{c.start_place}）" if c.start_place else f"出発：{c.start_shop}",
        f"返却：{c.return_shop}",
        f"期間：{c.period}",
        f"車種：{c.car}",
        f"条件：{c.condition}",
        f"予約：{c.reserve_shop} ☎ {c.tel}",
    ]
    return title, "\n".join(lines)


# ---------------------------------------------------------------- メイン

MAX_INDIVIDUAL = 8  # 1回の実行で個別通知する上限（多いときはまとめて1通に）


def run(args: argparse.Namespace) -> int:
    if args.test:
        send_ntfy("✅ 片道GO!通知のテスト",
                  "この通知が届けば設定は完了です。\n新しい車が出たらここに届きます。",
                  tags=["white_check_mark"], priority=3, dry_run=args.dry_run)
        print("テスト通知を送りました")
        return 0

    routes = load_routes()
    html = Path(args.html).read_text(encoding="utf-8") if args.html else fetch_html()
    cars = parse_cars(html)
    state = load_state()
    first_run = "seen" not in state

    if not cars:
        # ページの構造が変わった・一時的に空、などの可能性。1回だけ知らせる。
        print("車両が1件も見つかりませんでした")
        if not state.get("empty_alerted"):
            send_ntfy("⚠️ 片道GO!の一覧を読み取れません",
                      "掲載が0件か、ページの構造が変わった可能性があります。\n" + URL,
                      tags=["warning"], priority=3, dry_run=args.dry_run)
            state["empty_alerted"] = True
            if not args.dry_run:
                save_state(state)
        return 0
    state.pop("empty_alerted", None)

    seen: dict = state.get("seen", {})
    current = {c.key: c for c in cars}
    new_cars = [c for k, c in current.items() if k not in seen]
    targets = [c for c in new_cars if matches(c, routes)]
    route_label = "、".join(f"{a}→{b}".replace("*", "どこでも") for a, b in routes) or "なし"
    print(f"掲載 {len(cars)} 件 / 新着 {len(new_cars)} 件 / 条件に合う新着 {len(targets)} 件（{route_label}）")

    if first_run:
        hit = [c for c in cars if matches(c, routes)]
        body = f"監視ルート：{route_label}\n現在の掲載 {len(cars)} 台のうち、条件に合うのは {len(hit)} 台です。\n以後は新しく掲載された車だけを通知します。"
        send_ntfy("✅ 片道GO!の監視を開始しました", body, tags=["white_check_mark"], priority=3, dry_run=args.dry_run)
    elif targets:
        for c in targets[:MAX_INDIVIDUAL]:
            title, msg = car_message(c)
            send_ntfy(title, msg, tel=c.tel, tags=["red_car"], dry_run=args.dry_run)
        rest = targets[MAX_INDIVIDUAL:]
        if rest:
            lines = [f"・{c.start_shop}→{c.return_shop}｜{c.car}｜{c.period}｜☎{c.tel}" for c in rest]
            send_ntfy(f"🚗 ほかに {len(rest)} 台の新着", "\n".join(lines) + "\n\n" + URL,
                      tags=["red_car"], dry_run=args.dry_run)

    # 今掲載されている車だけを記録（消えた車は忘れる）
    state["seen"] = {k: seen.get(k) or {"first_seen": time.strftime("%Y-%m-%d %H:%M"), **asdict(c)}
                     for k, c in current.items()}
    if not args.dry_run:
        save_state(state)
    return 0


def _area(x: str) -> str:
    x = AREA_ALIASES.get(x.strip(), x.strip())
    if x != "*" and x not in AREA_CODES.values():
        raise SystemExit(f"エリア名が正しくありません: {x}")
    return x


def _route_label(routes: list[dict]) -> str:
    if not routes:
        return "（なし：通知は届きません）"
    return "\n".join(f"・{r['from']} → {r['to']}".replace("*", "どこでも") for r in routes)


def edit_routes(op: str, start: str, dest: str, dry_run: bool = False) -> int:
    """「ルート設定」ボタンから呼ばれる。config.json のルートを追加・削除する。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    routes: list[dict] = cfg.get("routes", [])
    r = {"from": _area(start), "to": _area(dest)}
    if op == "追加":
        if routes == [{"from": "*", "to": "*"}]:
            routes = []  # 初期設定（全ルート）は、最初の追加で置き換える
        if r not in routes:
            routes.append(r)
        head = f"ルートを追加しました：{r['from']} → {r['to']}"
    elif op == "削除":
        if r not in routes:
            head = f"そのルートは登録されていません：{r['from']} → {r['to']}"
        else:
            routes.remove(r)
            head = f"ルートを削除しました：{r['from']} → {r['to']}"
    elif op == "この1件だけにする":
        routes = [r]
        head = f"ルートを {r['from']} → {r['to']} の1件にしました"
    elif op == "確認だけ":
        head = "現在の監視ルート"
    else:
        raise SystemExit(f"不明な操作: {op}")
    head = head.replace("*", "どこでも")
    cfg["routes"] = routes
    if not dry_run:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    body = "監視中のルート：\n" + _route_label(routes)
    print(head + "\n" + body)
    send_ntfy("⚙️ " + head, body, tags=["gear"], priority=2, dry_run=dry_run)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", action="store_true", help="テスト通知を送る")
    p.add_argument("--dry-run", action="store_true", help="通知も保存もせず、表示だけする")
    p.add_argument("--html", help="ネットから取らず、このHTMLファイルを使う")
    p.add_argument("--route", nargs=3, metavar=("操作", "出発", "返却"),
                   help="ルートを編集（操作: 追加/削除/この1件だけにする/確認だけ）")
    a = p.parse_args()
    if a.route:
        return edit_routes(*a.route, dry_run=a.dry_run)
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
