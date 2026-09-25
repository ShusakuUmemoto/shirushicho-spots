#!/usr/bin/env python3
"""行政のオープンデータ（自治体の観光施設一覧など）から寺社・城の詳しい情報を取り、spots.sqlite に書き足す。

使い方（リポジトリのルートで。fetch.py のあとに流す。fetch.py は DB を作り直すので、そのあとにもう一度流す）:
    python3 scripts/spot/details.py            # sources.json の公開元すべて
    python3 scripts/spot/details.py --refresh  # 保存した CSV を使わず取り直す
    python3 scripts/spot/details.py --sources 別の設定.json --db 別の.sqlite  # 試すとき

- 公開元は sources.json に1つずつ書く（取得先・ライセンス・列の対応）。デジタル庁の自治体標準オープンデータセット「観光施設一覧」の列を想定する。
- CSV の行を、DB の寺社・城に名前と距離で当てる。名前は空白・括弧書き・「宗教法人」・旧字体をならし、括弧の中の別名と山号を外した名前も試す。
  同じ名前か、一覧の名前の頭に言葉が付いた形（元伊勢籠神社／籠神社）、DB の名前が一覧の名前を含む形なら同じとみなす。
  後ろに言葉が付いた形（高台寺掌美術館・二条城駐車場）は、境内や門前の別の施設なので当てない。
  座標があれば MATCH_DISTANCE 以内の候補のうち名前のよく合うもの（同じならいちばん近いもの）、座標がなければ同じ都道府県で名前が1件だけ合うものに当てる。
- 当たった寺社だけを spot_details の表に書く（出典の名前・ライセンス・情報更新日も一緒に）。spots の表には手を付けない。
- 当たった件数と、寺社・城らしいのに当たらなかった名前を表示する。
- ダウンロードした CSV は一時フォルダに保存し、やり直しても送らない。
- 各公開元のライセンス（多くは CC BY 4.0）に従い、アプリでは出典を出す。
- 標準ライブラリだけで動く。
"""

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = Path(__file__).resolve().parent / "sources.json"
DEFAULT_DB = ROOT / "GoshuinApp" / "Resources" / "Spots" / "spots.sqlite"
CACHE_DIR = Path(tempfile.gettempdir()) / "goshuin-spot-cache"
USER_AGENT = "ShirushichoSpotDetails/0.1 (https://github.com/ShusakuUmemoto/shirushicho-spots)"
# 取得の待ち時間（秒）とやり直す回数
READ_TIMEOUT = 120
MAX_RETRIES = 4
RETRY_WAIT = 30
# 行政の一覧の座標と DB の寺社の座標のずれを許す距離（m）。境内の代表点と入口の点がずれることがある
MATCH_DISTANCE = 300.0
# 候補を DB から引くときの緯度・経度の幅（度）。MATCH_DISTANCE より少し広く取る
SEARCH_DEGREES = 0.005
EARTH_RADIUS = 6_371_000.0
# 片方がもう片方を含むとみなす名前の最短の長さ（「寺」1文字などで当たらないように）
MIN_CONTAINED_LENGTH = 3
# 名前の合い方の点（同じ名前の行を、含むだけの行より先に使う）
EXACT_SCORE = 2
PARTIAL_SCORE = 1
# 後ろに付いても同じ場所とみなす言葉（実相院門跡／実相院）
SAME_PLACE_SUFFIXES = ("門跡",)
# 寺社・城らしい名前（当たらなかったものを表示するときだけ使う）
SPOT_NAME_PATTERN = re.compile(r"(神社|大社|神宮|八幡|天満宮|稲荷|宮|寺|院|大師|不動|観音|城)$")
# ならすときに外す言葉
NAME_NOISE = ("宗教法人",)
# 旧字体と新字体・異体字をそろえる（OSM と行政の一覧で書き分けがある。例: 大宮賣神社／大宮売神社）
VARIANT_CHARACTERS = str.maketrans({
    "賣": "売", "觀": "観", "應": "応", "廣": "広", "國": "国", "澤": "沢", "濱": "浜", "邊": "辺", "邉": "辺",
    "齋": "斎", "齊": "斉", "櫻": "桜", "嶋": "島", "嶌": "島", "峯": "峰", "龜": "亀", "圓": "円", "寶": "宝",
    "佛": "仏", "彌": "弥", "眞": "真", "德": "徳", "瀧": "滝", "藏": "蔵", "會": "会", "萬": "万", "靈": "霊",
    "禪": "禅", "稻": "稲", "龍": "竜", "ヶ": "ケ", "ヵ": "カ",
})
# 山号（「天祥山長橋寺」の「天祥山」）。寺の名前の頭に付くことがあり、OSM では付いていないことが多い
MOUNTAIN_PREFIX = re.compile(r"^[^山]{1,4}山(?=.{2,}(寺|院)$)")
# 文字コードの候補（自治体の CSV は Shift_JIS のことがある）
ENCODINGS = ("utf-8-sig", "cp932")


# MARK: - 取得

def download(url: str, refresh: bool) -> bytes:
    cache_path = CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".csv")
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
                data = response.read()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
            return data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == MAX_RETRIES:
                raise SystemExit(f"取得できませんでした: {url}\n  {error!r}")
            print(f"  取得できなかったので {RETRY_WAIT} 秒後にやり直します（{attempt}/{MAX_RETRIES - 1}）: {error!r}"[:200],
                  flush=True)
            time.sleep(RETRY_WAIT)
    raise AssertionError("ここには来ない")


def decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"文字コードが分かりませんでした（{', '.join(ENCODINGS)} のどれでも読めません）")


def read_rows(source: dict, refresh: bool) -> list[dict]:
    local_file = source.get("file")
    if local_file:
        data = (Path(local_file) if Path(local_file).is_absolute() else ROOT / local_file).read_bytes()
    else:
        data = download(source["url"], refresh)
    reader = csv.DictReader(io.StringIO(decode(data)))
    columns = source["columns"]
    missing = [column for column in (columns["name"],) if column not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"{source['name']}: 列が見つかりません: {missing}（列: {reader.fieldnames}）")
    return list(reader)


# MARK: - 名前と距離

def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).translate(VARIANT_CHARACTERS)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)  # 括弧書き（読み・別名）を外す
    for noise in NAME_NOISE:
        text = text.replace(noise, "")
    return re.sub(r"[\s・･]", "", text)


def aliases(name: str) -> set[str]:
    """行政の一覧の名前から、当てるときに試す名前。括弧の中（別名）と、山号を外した名前も試す
    （「神谷神社（神谷太刀宮）」→ 神谷神社・神谷太刀宮、「天祥山長橋寺」→ 天祥山長橋寺・長橋寺）"""
    text = unicodedata.normalize("NFKC", name)
    names = {normalize(text)} | {normalize(inner) for inner in re.findall(r"[（(]([^）)]*)[）)]", text)}
    names |= {MOUNTAIN_PREFIX.sub("", candidate) for candidate in names}
    return {candidate for candidate in names if candidate}


def name_score(listed: str, spot: str) -> int:
    """行政の一覧の名前（listed）と DB の名前（spot）がどれだけ合うか。0 は別の場所、大きいほどよく合う。
    一覧の名前が長いときは、頭に言葉が付いた形（元伊勢籠神社・大本山東福寺・丹波亀山城跡）か、門跡が付いた形だけを同じとみなす。
    後ろに言葉が付いた形（高台寺掌美術館・南禅寺順正・二条城駐車場）は境内や門前の別の施設なので当てない"""
    if listed == spot:
        return EXACT_SCORE
    if len(listed) > len(spot) >= MIN_CONTAINED_LENGTH:
        if listed.endswith(spot) or any(listed == spot + suffix for suffix in SAME_PLACE_SUFFIXES):
            return PARTIAL_SCORE
        return 0
    if len(spot) > len(listed) >= MIN_CONTAINED_LENGTH and listed in spot:
        return PARTIAL_SCORE
    return 0


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = p2 - p1
    d_lon = math.radians(lon2 - lon1)
    h = math.sin(d_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(h))


def number(text: str | None) -> float | None:
    try:
        return float((text or "").strip())
    except ValueError:
        return None


def match(connection: sqlite3.Connection, name: str, lat: float | None, lon: float | None,
          prefecture: str | None) -> tuple[str, int] | None:
    """行政の一覧の1行に当たる DB の寺社・城の id と、名前の合い方の点。当たらなければ None"""
    targets = aliases(name)
    if not targets:
        return None
    if lat is not None and lon is not None:
        rows = connection.execute(
            "SELECT id, name, lat, lon FROM spots WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - SEARCH_DEGREES, lat + SEARCH_DEGREES, lon - SEARCH_DEGREES, lon + SEARCH_DEGREES))
        candidates = []
        for spot_id, spot_name, spot_lat, spot_lon in rows:
            score = max(name_score(target, normalize(spot_name)) for target in targets)
            meters = distance(lat, lon, spot_lat, spot_lon)
            if score and meters <= MATCH_DISTANCE:
                candidates.append((-score, meters, spot_id))
        if not candidates:
            return None
        best = min(candidates)
        return best[2], -best[0]
    if not prefecture:
        return None
    # 座標がない行は、同じ都道府県で名前が1件だけ合うときに当てる（同じ名前の寺社は多いので、2件以上なら当てない）
    rows = connection.execute("SELECT id, name FROM spots WHERE prefecture = ?", (prefecture,))
    ids = [row[0] for row in rows if normalize(row[1]) in targets]
    return (ids[0], EXACT_SCORE) if len(ids) == 1 else None


# MARK: - 詳しい情報

def value(row: dict, columns: dict, key: str) -> str:
    column = columns.get(key)
    return (row.get(column) or "").strip() if column else ""


def joined(*parts: str, separator: str = "　") -> str:
    return separator.join(part for part in parts if part)


def details(row: dict, source: dict) -> dict:
    columns = source["columns"]
    open_time, close_time = value(row, columns, "openTime"), value(row, columns, "closeTime")
    hours = f"{open_time}〜{close_time}" if open_time and close_time else joined(open_time, close_time)
    return {
        "description": value(row, columns, "description"),
        "days": value(row, columns, "days"),
        "hours": joined(hours, value(row, columns, "hoursNote")),
        "fee": joined(value(row, columns, "fee"), value(row, columns, "feeDetail")),
        "address": value(row, columns, "address"),
        "phone": value(row, columns, "phone"),
        "url": value(row, columns, "url"),
        "access": value(row, columns, "access"),
        "parking": value(row, columns, "parking"),
        "updated": value(row, columns, "updated"),
        "source": source["name"],
        "license": source["license"],
    }


DETAIL_COLUMNS = ("description", "days", "hours", "fee", "address", "phone", "url", "access", "parking",
                  "updated", "source", "license")


def has_content(detail: dict) -> bool:
    return any(detail[key] for key in DETAIL_COLUMNS if key not in ("updated", "source", "license"))


def write(connection: sqlite3.Connection, found: dict[str, dict], sources: list[dict]) -> None:
    connection.execute("DROP TABLE IF EXISTS spot_details")
    column_definitions = ", ".join(f"{column} TEXT NOT NULL" for column in DETAIL_COLUMNS)
    connection.execute(f"CREATE TABLE spot_details (id TEXT PRIMARY KEY, {column_definitions})")
    placeholders = ", ".join(f":{column}" for column in ("id",) + DETAIL_COLUMNS)
    connection.executemany(f"INSERT INTO spot_details VALUES ({placeholders})",
                           [{"id": spot_id, **detail} for spot_id, detail in found.items()])
    connection.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
        ("detailsCount", str(len(found))),
        ("detailsSources", " / ".join(f"{source['name']}（{source['license']}）" for source in sources)),
    ])
    connection.commit()
    connection.execute("VACUUM")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}（先に fetch.py を流す）")
    sources = json.loads(args.sources.read_text(encoding="utf-8"))["sources"]
    connection = sqlite3.connect(args.db)
    found: dict[str, dict] = {}
    for source in sources:
        print(f"■ {source['name']}（{source['license']}）", flush=True)
        rows = read_rows(source, args.refresh)
        columns = source["columns"]
        # 同じ寺社に2行当たったら、名前のよく合う方を残す（同じ点なら先の行）。前の公開元で当たった寺社は上書きしない
        best: dict[str, tuple[int, dict]] = {}
        unmatched = []
        for row in rows:
            name = value(row, columns, "name")
            result = match(connection, name, number(row.get(columns.get("latitude", ""))),
                           number(row.get(columns.get("longitude", ""))), source.get("prefecture"))
            if result is None:
                if SPOT_NAME_PATTERN.search(normalize(name)):
                    unmatched.append(name)
                continue
            spot_id, score = result
            detail = details(row, source)
            if spot_id in found or not has_content(detail):
                continue
            if spot_id not in best or score > best[spot_id][0]:
                best[spot_id] = (score, detail)
        found.update({spot_id: detail for spot_id, (_, detail) in best.items()})
        print(f"  {len(rows)} 行のうち {len(best)} か所に当てました", flush=True)
        if unmatched:
            print(f"  寺社・城らしいのに当たらなかった {len(unmatched)} 件: {'、'.join(unmatched[:60])}", flush=True)

    write(connection, found, sources)
    connection.close()
    print(f"○ {len(found)} か所の詳しい情報を書き足しました: {args.db}")


if __name__ == "__main__":
    main()
