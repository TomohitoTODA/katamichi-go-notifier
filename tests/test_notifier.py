import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifier  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture.html").read_text(encoding="utf-8")


def test_parse_fields():
    cars = notifier.parse_cars(FIXTURE)
    assert len(cars) == 3
    c = cars[1]
    assert (c.start_area, c.return_area) == ("関東", "中部")
    assert c.start_shop == "トヨタモビリティサービス 羽田空港（国内線）店"
    assert c.start_place == "東京都 大田区"
    assert c.return_shop == "トヨタレンタリース静岡"
    assert c.car == "ノアHEV 車両番号906"
    assert c.period == "2026年11月19日 ～ 11月21日"
    assert c.reserve_shop == "羽田空港（国内線）店"
    assert c.tel == "03-3744-0100"
    assert notifier.tel_uri(c.tel) == "tel:0337440100"


def test_routes(monkeypatch):
    cars = notifier.parse_cars(FIXTURE)
    monkeypatch.setenv("ROUTES", "関東>中部, 近畿→*")
    routes = notifier.load_routes()
    assert [c.start_area for c in cars if notifier.matches(c, routes)] == ["関東", "近畿"]
    monkeypatch.setenv("ROUTES", "*>東北")
    assert [c.start_shop for c in cars if notifier.matches(c, notifier.load_routes())] == ["トヨタレンタリース宮城 仙台空港店"]


def test_only_new_cars_notified(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notifier, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setenv("ROUTES", "関東>中部")
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append((title, msg, kw)))
    html_file = tmp_path / "page.html"

    # 1回目：開始通知だけ
    html_file.write_text(FIXTURE, encoding="utf-8")
    args = type("A", (), {"test": False, "dry_run": False, "html": str(html_file)})()
    notifier.run(args)
    assert len(sent) == 1 and "開始" in sent[0][0]

    # 2回目：変化なし → 通知なし
    sent.clear()
    notifier.run(args)
    assert sent == []

    # 3回目：関東→中部の新しい車が増えた → 1件通知（電話番号付き）
    new_li = FIXTURE.split('<li class="service-item" data-start-area="3"')[1].split("</li>")[0]
    new_li = new_li.replace("車両番号906", "車両番号999").replace("03-3744-0100", "03-0000-1111")
    html_file.write_text(FIXTURE.replace("</ul>", '<li class="service-item" data-start-area="3"' + new_li + "</li></ul>", 1), encoding="utf-8")
    notifier.run(args)
    assert len(sent) == 1
    title, msg, kw = sent[0]
    assert "関東→中部" in title and "車両番号999" in msg and kw["tel"] == "03-0000-1111"

    state = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert len(state["seen"]) == 4


def test_edit_routes(tmp_path, monkeypatch):
    html = tmp_path / "page.html"
    html.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setattr(notifier, "STATE_PATH", tmp_path / "seen.json")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"routes": [{"from": "*", "to": "*"}]}), encoding="utf-8")
    monkeypatch.setattr(notifier, "CONFIG_PATH", cfg)
    monkeypatch.delenv("ROUTES", raising=False)
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append(title))
    edit = lambda *a: notifier.edit_routes(*a, html_path=str(html))
    routes = lambda: json.loads(cfg.read_text(encoding="utf-8"))["routes"]

    edit("追加", "関東", "中部")          # 初期の全ルートは置き換わる
    assert routes() == [{"from": "関東", "to": "中部"}]
    edit("追加", "近畿", "どこでも")
    assert routes()[1] == {"from": "近畿", "to": "*"}
    edit("削除", "関東", "中部")
    assert routes() == [{"from": "近畿", "to": "*"}]
    edit("この1件だけにする", "どこでも", "九州")
    assert notifier.load_routes() == [notifier.Route("*", "九州")]
    edit("削除", "どこでも", "九州")
    assert notifier.load_routes() == []                 # 空なら何も通知しない
    # 追加・この1件だけにする では、そのルートの今の一覧も届く（設定通知＋一覧で2通）
    # 近畿発の車は受付終了なので一覧には出ない
    assert [t for t in sent if t.startswith("📋")] == ["📋 関東→中部：受付中 1台（🆕1）", "📋 近畿→どこでも：受付中 0台", "📋 どこでも→九州：受付中 0台"]
    assert len(sent) == 8


def test_listing_marks_new(tmp_path, monkeypatch):
    from datetime import timedelta
    cars = notifier.parse_cars(FIXTURE)
    now = notifier.now_jst()
    seen = {
        cars[0].key: {"first_seen": (now - timedelta(hours=48)).isoformat()},   # 古い
        cars[1].key: {"first_seen": (now - timedelta(hours=2)).isoformat()},    # 新しい
        # cars[2] は未登録 → 新しい
    }
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append((title, msg)))
    notifier.send_listing([notifier.Route()], cars, seen)
    title, body = sent[0]
    assert title == "📋 どこでも→どこでも：受付中 2台（🆕1）"
    assert body.index("【🆕 新着 1台") < body.index("【受付中 1台】")
    assert "🆕11/19〜11/21 羽田空港（国内線）店（東京都）→ 静岡" in body
    assert "・11/18〜11/21 仙台空港店（宮城県）→ 福島" in body
    assert "心斎橋" not in body and "受付終了の掲載 1台は除いています" in body
    # 古い形式（UTC・タイムゾーンなし）の記録も読める
    assert notifier._parse_seen_time("2026-11-21 15:05").utcoffset().total_seconds() == 0


def test_pref_and_keyword_filter(tmp_path, monkeypatch):
    cars = notifier.parse_cars(FIXTURE)
    R = notifier.make_route
    assert [c.start_shop for c in cars if R("関東", "*", "東京都").hit(c)] == ["トヨタモビリティサービス 羽田空港（国内線）店"]
    assert [c for c in cars if R("関東", "*", "千葉県").hit(c)] == []
    assert len([c for c in cars if R("*", "*", "", notifier.split_keywords("羽田、仙台空港")).hit(c)]) == 2
    assert len([c for c in cars if R("*", "*", "", ("大田区",)).hit(c)]) == 1      # 市区町村でもOK
    assert notifier.split_keywords(" 羽田, 品川　東京駅 ") == ("羽田", "品川", "東京駅")
    assert R("関東", "近畿", "東京都", ("羽田", "品川")).label == "関東（東京都・羽田/品川）→近畿"
    assert R("関東", "近畿", "指定なし").label == "関東→近畿"

    # ボタン操作：追加 → config.json に保存され、読み戻せる
    html = tmp_path / "page.html"
    html.write_text(FIXTURE, encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"routes": [{"from": "関東", "to": "近畿"}]}), encoding="utf-8")
    monkeypatch.setattr(notifier, "CONFIG_PATH", cfg)
    monkeypatch.setattr(notifier, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.delenv("ROUTES", raising=False)
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append(title))
    notifier.edit_routes("追加", "関東", "中部", pref="東京都", keywords="羽田,品川", html_path=str(html))
    saved = json.loads(cfg.read_text(encoding="utf-8"))["routes"]
    assert saved[1] == {"from": "関東", "to": "中部", "pref": "東京都", "keywords": ["羽田", "品川"]}
    assert notifier.load_routes()[1] == R("関東", "中部", "東京都", ("羽田", "品川"))
    assert sent == ["⚙️ ルートを追加しました：関東（東京都・羽田/品川）→中部", "📋 関東（東京都・羽田/品川）→中部：受付中 1台（🆕1）"]
    notifier.edit_routes("削除", "関東", "中部", pref="東京都", keywords="羽田, 品川", html_path=str(html))
    assert len(json.loads(cfg.read_text(encoding="utf-8"))["routes"]) == 1
