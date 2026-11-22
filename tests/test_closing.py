"""受付終了・受付再開・掲載終了の扱いのテスト。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifier  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture.html").read_text(encoding="utf-8")


def _args(html):
    return type("A", (), {"test": False, "dry_run": False, "html": str(html)})()


def _run(html, content):
    html.write_text(content, encoding="utf-8")
    notifier.run(_args(html))


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setenv("ROUTES", "*>*")
    sent = []
    monkeypatch.setattr(notifier, "send_ntfy", lambda title, msg, **kw: sent.append((title, msg, kw)))
    return sent, tmp_path / "page.html"


def test_parse_ended():
    assert [c.ended for c in notifier.parse_cars(FIXTURE)] == [False, False, True]


def test_closed_and_reopened(tmp_path, monkeypatch):
    """新着 → 受付終了（同じIDで書き換え）→ 受付再開 → 掲載終了 の流れ。"""
    sent, html = _setup(tmp_path, monkeypatch)
    haneda = FIXTURE.split('<li class="service-item" data-start-area="3"')[1].split("</li>")[0]
    new_li = '<li class="service-item" data-start-area="3"' + haneda.replace("車両番号906", "車両番号777") + "</li>"
    with_new = FIXTURE.replace("</ul>", new_li + "</ul>", 1)
    ended = with_new.replace(new_li, new_li.replace('service-item__body "', 'service-item__body  show-entry-end"'))
    assert ended != with_new

    _run(html, FIXTURE)                       # 初回：開始通知だけ
    assert len(sent) == 1
    sent.clear()

    _run(html, with_new)                      # 受付中の新着 → 通知（IDつき）
    assert len(sent) == 1
    title, msg, kw = sent[0]
    assert title.startswith("🚗 関東→中部") and "車両番号777" in msg
    sid = kw["sequence_id"]
    sent.clear()

    _run(html, ended)                         # 受付終了 → 同じIDで書き換え（静かな通知）
    assert len(sent) == 1
    title, msg, kw = sent[0]
    assert title.startswith("❌ 受付終了｜関東→中部") and kw["sequence_id"] == sid and kw["priority"] == 2
    assert msg.startswith("【受付終了】")
    sent.clear()

    _run(html, ended)                         # 変化なし → 何も送らない
    assert sent == []

    _run(html, with_new)                      # 受付再開 → もう一度通知
    assert len(sent) == 1 and sent[0][0].startswith("🔁") and "【受付再開】" in sent[0][1]
    sent.clear()

    _run(html, FIXTURE)                       # 一覧から消えた → 掲載終了に書き換え
    assert len(sent) == 1 and sent[0][0].startswith("❌ 掲載終了") and sent[0][2]["sequence_id"] == sid


def test_new_but_already_closed_is_silent(tmp_path, monkeypatch):
    sent, html = _setup(tmp_path, monkeypatch)
    _run(html, FIXTURE)
    sent.clear()
    osaka = FIXTURE.split('<li class="service-item" data-start-area="5"')[1].split("</li>")[0]
    li = '<li class="service-item" data-start-area="5"' + osaka.replace("車両番号8255", "車両番号1234") + "</li>"
    _run(html, FIXTURE.replace("</ul>", li + "</ul>", 1))   # 最初から受付終了の新顔
    assert sent == []


def test_old_state_without_flags_does_not_flood(tmp_path, monkeypatch):
    """以前の版の既読リスト（ended/notified なし）から更新しても、通知が殺到しない。"""
    sent, html = _setup(tmp_path, monkeypatch)
    cars = notifier.parse_cars(FIXTURE)
    old = {"seen": {c.key: {"first_seen": "2026-11-21 15:05", "car": c.car} for c in cars}}
    (tmp_path / "seen.json").write_text(json.dumps(old), encoding="utf-8")
    _run(html, FIXTURE)
    assert sent == []
    saved = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))["seen"]
    assert all("ended" in v and "notified" in v for v in saved.values())
