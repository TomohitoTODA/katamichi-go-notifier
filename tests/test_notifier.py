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
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"routes": [{"from": "*", "to": "*"}]}), encoding="utf-8")
    monkeypatch.setattr(notifier, "CONFIG_PATH", cfg)
    monkeypatch.delenv("ROUTES", raising=False)
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append(title))
    routes = lambda: json.loads(cfg.read_text(encoding="utf-8"))["routes"]

    notifier.edit_routes("追加", "関東", "中部")          # 初期の全ルートは置き換わる
    assert routes() == [{"from": "関東", "to": "中部"}]
    notifier.edit_routes("追加", "近畿", "どこでも")
    assert routes()[1] == {"from": "近畿", "to": "*"}
    notifier.edit_routes("削除", "関東", "中部")
    assert routes() == [{"from": "近畿", "to": "*"}]
    notifier.edit_routes("この1件だけにする", "どこでも", "九州")
    assert notifier.load_routes() == [("*", "九州")]
    notifier.edit_routes("削除", "どこでも", "九州")
    assert notifier.load_routes() == []                 # 空なら何も通知しない
    assert len(sent) == 5
