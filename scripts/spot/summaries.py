#!/usr/bin/env python3
"""spots.sqlite の Wikipedia の冒頭の文（spot_wiki）から1文目だけを抜き出し、アプリ本体に入れる JSON に書く。

使い方（リポジトリのルートで。通信しない。wikipedia.py も最後にこれを呼ぶ）:
    python3 scripts/spot/summaries.py
    python3 scripts/spot/summaries.py --db 別の.sqlite --out 別の.json  # 試すとき

- 全国の寺社のデータ（spots.sqlite）は竹プランの人だけが落とすので、梅プランの人の印帖の「〇〇について」には1文目だけをこの JSON から出す。
- 場所ごとに QID・記事の名前・1文目と、当て込みに使う名前と座標（spots の同じ QID の行。札所だけの場所は空で、QID で当てる）を持つ。
- 1文目は、括弧（「」・（）など）の外の最初の「。」まで。言い換えない。
- 標準ライブラリだけで動く。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from details import DEFAULT_DB, ROOT  # noqa: E402

DEFAULT_OUT = ROOT / "GoshuinApp" / "Resources" / "spot-summaries.json"
OPENING_BRACKETS = "「『（(［[【〔"
CLOSING_BRACKETS = "」』）)］]】〕"
SENTENCE_END = "。"
# 座標の小数点以下の桁（約 1m。ファイルを小さくする）
COORDINATE_DIGITS = 5


def first_sentence(text: str) -> str:
    """括弧の外の最初の「。」までを返す（なければ全体）"""
    depth = 0
    for index, character in enumerate(text):
        if character in OPENING_BRACKETS:
            depth += 1
        elif character in CLOSING_BRACKETS:
            depth = max(depth - 1, 0)
        elif character == SENTENCE_END and depth == 0:
            return text[:index + 1]
    return text


def summaries(connection: sqlite3.Connection) -> list[dict]:
    places = []
    for qid, title, summary in connection.execute(
            "SELECT qid, title, summary FROM spot_wiki WHERE summary != '' ORDER BY qid"):
        spots = [
            {"name": name, "lat": round(lat, COORDINATE_DIGITS), "lon": round(lon, COORDINATE_DIGITS)}
            for name, lat, lon in connection.execute(
                "SELECT name, lat, lon FROM spots WHERE wikidata = ? ORDER BY id", (qid,))
        ]
        places.append({"qid": qid, "title": title, "sentence": first_sentence(summary), "spots": spots})
    return places


def write_summaries(connection: sqlite3.Connection, out: Path) -> int:
    places = summaries(connection)
    out.write_text(json.dumps({"places": places}, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return len(places)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}")
    connection = sqlite3.connect(args.db)
    count = write_summaries(connection, args.out)
    connection.close()
    print(f"○ {count} か所の1文目を書きました: {args.out}")


if __name__ == "__main__":
    main()
