#!/usr/bin/env python3
"""OpenStreetMap で QID のない寺社・城に、日本語版 Wikipedia の同じ名前の記事から QID を付ける。

使い方（リポジトリのルートで）:
    python3 scripts/spot/wikidata_links.py --find   # 通信あり。候補を探して wikidata_links.json に書く（DB は変えない）
    python3 scripts/spot/wikidata_links.py          # 通信なし。wikidata_links.json の QID を DB に当て、データの版を書き直す
    python3 scripts/spot/wikidata_links.py --db 別の.sqlite --version-file 別の.txt  # 試すとき

- 閲覧数の順位（popular.py）は QID のある場所しか数えないので、QID のない有名な所（日吉大社・立石寺など）が抜ける。それを埋める。
- 探し方: QID のない場所の名前を、そのまま記事の名前として引く（転送もたどる。旧字体の名前 → 新字体の記事など）。
  次のすべてを満たすときだけ候補にする。
  - 記事があり、曖昧さ回避のページでなく、座標と QID がある。
  - その QID が DB のどの場所にもまだない（1つの QID を2か所に付けない）。
  - 記事の座標から LINK_DISTANCE 以内に、その名前の QID のない場所がある。複数あれば、いちばん近い1か所だけ。
  - 記事の名前と DB の名前が重なる（popular.names_overlap。転送で別の記事に行った所を防ぐ。「山寺」→ 立石寺のような呼び名の転送も付けない）。
  「八幡神社」のように各地にある名前は、記事が一般の説明か曖昧さ回避で座標がないので、候補にならない。
- 確かめて誤りと分かったものは、wikidata_links.json の exclude に場所の id と理由を書く（--find で探し直しても残す）。
- 当て方: 場所の id が同じで、まだ QID のない場所にだけ付ける。OpenStreetMap の側で QID が付いた所は、OpenStreetMap の値を残す。
  同じ QID がすでに DB にあれば付けない。fetch.py も、巡礼リストの場所を足したあとにこれを通す（DB を作り直しても残る）。
- spot_area は場所の id で引くので、単独で流しても作り直さなくてよい。
- 答えは一時フォルダ（wikipedia.py と同じ）に保存し、やり直しても送らない。送るたびに REQUEST_INTERVAL 秒あける。
- 標準ライブラリだけで動く。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import details  # noqa: E402
import pilgrimage_places  # noqa: E402
from popular import names_overlap  # noqa: E402
from wikipedia import WIKIPEDIA_API, batches, request  # noqa: E402

LINKS_FILE = Path(__file__).with_name("wikidata_links.json")
# 記事の座標と DB の場所が同じとみなす距離（m）。広い境内・城跡は、記事の座標と OpenStreetMap の代表点が離れることがある
LINK_DISTANCE = 1000.0
# 1回に引く記事の数（titles は50件まで）
TITLE_BATCH = 50
# 記事の名前に使えない文字（入っている名前は引かない。「|」は問い合わせの区切りでもある）
INVALID_TITLE_CHARACTERS = set("#<>[]|{}")
# 記事の名前の長さの上限（バイト）
MAX_TITLE_BYTES = 255


# MARK: - 探す

def is_valid_title(name: str) -> bool:
    return bool(name.strip()) and not (set(name) & INVALID_TITLE_CHARACTERS) and len(name.encode()) <= MAX_TITLE_BYTES


def resolve_pages(data: dict) -> dict[str, dict]:
    """問い合わせた名前 → 記事（名前・QID・座標・曖昧さ回避か）。表記の直し（normalized）と転送（redirects）をたどる"""
    query = data.get("query", {})
    renamed = {item["from"]: item["to"] for item in query.get("normalized", [])}
    redirected = {item["from"]: item["to"] for item in query.get("redirects", [])}
    pages = {page["title"]: page for page in query.get("pages", []) if not page.get("missing") and not page.get("invalid")}
    resolved = {}
    names = set(renamed) | set(redirected) | set(pages)
    for name in names:
        title = renamed.get(name, name)
        title = redirected.get(title, title)
        page = pages.get(title)
        if page is None:
            continue
        coordinates = (page.get("coordinates") or [{}])[0]
        properties = page.get("pageprops", {})
        resolved[name] = {"title": title, "qid": properties.get("wikibase_item"),
                          "disambiguation": "disambiguation" in properties,
                          "lat": coordinates.get("lat"), "lon": coordinates.get("lon")}
    return resolved


def look_up(names: list[str], refresh: bool) -> dict[str, dict]:
    pages = {}
    for index, batch in enumerate(batches(names, TITLE_BATCH), start=1):
        data = request(WIKIPEDIA_API, {"action": "query", "titles": "|".join(batch), "redirects": "1",
                                       "prop": "coordinates|pageprops", "ppprop": "wikibase_item|disambiguation",
                                       "colimit": "max"}, refresh)
        pages.update({name: page for name, page in resolve_pages(data).items() if name in batch})
        if index % 50 == 0:
            print(f"  {index * TITLE_BATCH}/{len(names)}", flush=True)
    return pages


def find_links(spots: list[dict], pages: dict[str, dict], excluded: set[str]) -> dict[str, dict]:
    """場所の id → 付ける QID と記事。QID ごとに、いちばん近い1か所だけ"""
    used = {spot["wikidata"] for spot in spots if spot["wikidata"]}
    unlinked: dict[str, list[dict]] = {}
    for spot in spots:
        if not spot["wikidata"] and spot["id"] not in excluded:
            unlinked.setdefault(spot["name"], []).append(spot)
    best: dict[str, tuple[float, dict, dict]] = {}
    for name, page in pages.items():
        qid = page["qid"]
        if not qid or page["disambiguation"] or page["lat"] is None or qid in used:
            continue
        for spot in unlinked.get(name, []):
            meters = details.distance(page["lat"], page["lon"], spot["lat"], spot["lon"])
            if meters > LINK_DISTANCE or not names_overlap(page["title"], [spot["name"]]):
                continue
            if qid not in best or meters < best[qid][0]:
                best[qid] = (meters, spot, page)
    links = {spot["id"]: {"qid": qid, "title": page["title"], "name": spot["name"], "distance": round(meters)}
             for qid, (meters, spot, page) in best.items()}
    return dict(sorted(links.items()))


# MARK: - 当てる

def load(path: Path = LINKS_FILE) -> tuple[dict[str, dict], dict[str, str]]:
    """（場所の id → 付ける QID と記事、外す場所の id → 理由）。ファイルがなければ空"""
    if not path.exists():
        return {}, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    excluded = data.get("exclude", {})
    for spot_id, reason in excluded.items():
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"wikidata_links.json の exclude の {spot_id} に理由がありません")
    return data.get("links", {}), excluded


def apply_links(spots: list[dict], links: dict[str, dict], excluded: dict[str, str]) -> list[dict]:
    """QID のない場所に付ける。返すのは付けた場所"""
    used = {spot["wikidata"] for spot in spots if spot["wikidata"]}
    applied = []
    for spot in spots:
        link = links.get(spot["id"])
        if link is None or spot["id"] in excluded or spot["wikidata"] or link["qid"] in used:
            continue
        spot["wikidata"] = link["qid"]
        used.add(link["qid"])
        applied.append(spot)
    return applied


def write_links(path: Path, links: dict[str, dict], excluded: dict[str, str]) -> None:
    """1か所を1行に書く（差分を見やすくする）"""
    def block(items: dict) -> str:
        lines = [f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"
                 for key, value in items.items()]
        return "{\n" + ",\n".join(lines) + "\n  }" if lines else "{}"
    path.write_text(f'{{\n  "count": {len(links)},\n  "links": {block(links)},\n  "exclude": {block(excluded)}\n}}\n',
                    encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=details.DEFAULT_DB)
    parser.add_argument("--version-file", type=Path, default=details.DEFAULT_VERSION_FILE)
    parser.add_argument("--find", action="store_true", help="通信して候補を探し、wikidata_links.json に書く（DB は変えない）")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}（先に fetch.py を流す）")

    try:
        links, excluded = load()
        if args.find:
            connection = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
            try:
                spots = pilgrimage_places.read_spots(connection)
            finally:
                connection.close()
            names = sorted({spot["name"] for spot in spots if not spot["wikidata"] and is_valid_title(spot["name"])})
            print(f"■ QID のない場所の名前: {len(names)} 種類（{TITLE_BATCH} 件ずつ引く）", flush=True)
            found = find_links(spots, look_up(names, args.refresh), set(excluded))
            write_links(LINKS_FILE, found, excluded)
            print(f"○ QID を付ける候補: {len(found)} か所: {LINKS_FILE}")
            return
        connection = sqlite3.connect(args.db)
        try:
            spots = pilgrimage_places.read_spots(connection)
            applied = apply_links(spots, links, excluded)
            if applied:
                pilgrimage_places.rewrite_spots(connection, spots)
                details.write_version_file(connection, args.version_file)
        finally:
            connection.close()
        print(f"○ QID を付けた場所: {len(applied)} か所（候補 {len(links)} か所）: {args.db}")
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        raise SystemExit(f"QID を付けられませんでした: {error}") from error


if __name__ == "__main__":
    main()
