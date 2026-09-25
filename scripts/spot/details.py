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
  2つの公開元が同じ寺社に当たったら、先の公開元（sources.json の順）の空の項目を後の公開元で埋め、出典にはどちらも書く。
- 公開元ごとに列の対応（columns）を書く。名前の列が見つからない公開元は飛ばす（列の一覧を表示する）。filter で種別の列の値の行だけにできる。
- 当たった件数と、寺社・城らしいのに当たらなかった名前を表示する。
- ダウンロードした CSV は一時フォルダに保存し、やり直しても送らない。
- 各公開元のライセンス（多くは CC BY 4.0）に従い、アプリでは出典を出す。
- 文化庁の国指定文化財等データベース（kind が culturalProperties の公開元）は、国宝・重要文化財の建造物の1棟ずつの行を寺社・城に当て、
  「国宝（2件）：本堂、…」の形にまとめて cultural_properties の項目に書く。検索結果の CSV は手で書き出す（2,000 件ずつ）ので files に並べる。
  建物は境内に散らばるので BUILDING_MATCH_DISTANCE 以内で、名称・所有者名が寺社の名前に合う（名称が寺社の名前で始まる形も含む）ものに当てる。
  正式な名前と呼び名が違う寺社（賀茂御祖神社／下鴨神社）は、公開元の aliases に書く。
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
# MATCH_DISTANCE 以内に当たらなかったとき、同じ都道府県で名前の合う寺社が1か所だけなら当てる距離（m）。
# 行政の一覧の座標が市役所や駅の近くに打たれていることがある（生國魂神社は 1.9km 離れていた）
UNIQUE_NAME_DISTANCE = 5_000.0
# 候補を DB から引くときの緯度・経度の幅（度）。MATCH_DISTANCE より少し広く取る
SEARCH_DEGREES = 0.005
# 文化財の建物の座標と DB の寺社の座標のずれを許す距離（m）。建物は1棟ずつ座標があり、広い境内の奥の堂や門は代表点から離れる
BUILDING_MATCH_DISTANCE = 500.0
# 建物の候補を DB から引くときの緯度・経度の幅（度）。BUILDING_MATCH_DISTANCE より少し広く取る
BUILDING_SEARCH_DEGREES = 0.006
# 国宝・重要文化財の種別ごとに名前を並べる建物の数（姫路城は重要文化財だけで 70 件を超える）。超えた分は「など」にする
MAX_LISTED_BUILDINGS = 6
# 文化財の種別を並べる順
PROPERTY_GRADES = ("国宝", "重要文化財")
# 摂社・末社など、境内の別の社の建物（本社の本殿より後に並べる）
SUB_SHRINE_PATTERN = re.compile(r"^(摂社|末社|境内社|旧)")
# 先に並べる建物（姫路城なら渡櫓より大天守、寺なら門より本堂）。前にあるものほど先
MAIN_BUILDINGS = ("大天守", "天守", "本殿", "本堂", "金堂", "五重塔", "三重塔", "多宝塔", "拝殿", "楼門", "三門", "山門", "仁王門")
EARTH_RADIUS = 6_371_000.0
# 片方がもう片方を含むとみなす名前の最短の長さ（「寺」1文字などで当たらないように）
MIN_CONTAINED_LENGTH = 3
# 名前の合い方の点（同じ名前の行を、含むだけの行より先に使う）
EXACT_SCORE = 2
PARTIAL_SCORE = 1
# 後ろに付いても同じ場所とみなす言葉
# （実相院門跡／実相院、大阪城天守閣／大阪城、今帰仁城跡／今帰仁城、首里城公園／首里城）
SAME_PLACE_SUFFIXES = ("門跡", "天守閣", "天守", "跡", "址", "公園")
# 寺社・城らしい名前（当たらなかったものを表示するときだけ使う）
SPOT_NAME_PATTERN = re.compile(r"(神社|大社|神宮|八幡|天満宮|稲荷|宮|寺|院|大師|不動|観音|城)$")
# 祭り・行事の行（「三井のお弓行事（友呂岐神社）」「白鳥神社だんじり祭」）。寺社の名前を含むが、説明は行事のことなので当てない
EVENT_PATTERN = re.compile(r"(祭|まつり|行事|ゆうべ|まいり|詣|ライトアップ|イベント)")
# ならすときに外す言葉
NAME_NOISE = ("宗教法人",)
# 旧字体と新字体・異体字をそろえる（OSM と行政の一覧で書き分けがある。例: 大宮賣神社／大宮売神社）
VARIANT_CHARACTERS = str.maketrans({
    "賣": "売", "觀": "観", "應": "応", "廣": "広", "國": "国", "澤": "沢", "濱": "浜", "邊": "辺", "邉": "辺",
    "齋": "斎", "齊": "斉", "櫻": "桜", "嶋": "島", "嶌": "島", "峯": "峰", "龜": "亀", "圓": "円", "寶": "宝",
    "佛": "仏", "彌": "弥", "眞": "真", "德": "徳", "瀧": "滝", "藏": "蔵", "會": "会", "萬": "万", "靈": "霊",
    "禪": "禅", "稻": "稲", "龍": "竜", "巖": "巌", "榮": "栄", "嶽": "岳", "鬪": "闘", "鷄": "鶏", "雞": "鶏",
    "總": "総", "祗": "祇", "ヶ": "ケ", "ヵ": "カ",
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


def read_rows(source: dict, refresh: bool) -> list[dict] | None:
    """公開元の行。名前の列がなければ None（公開元の列の形が変わった・対応を書き間違えた。列の一覧を表示して、この公開元を飛ばす）。
    ほかの項目の列がなければ、その項目だけを空にする。filter があれば、その列が values のどれかの行だけにする"""
    local_file = source.get("file")
    if local_file:
        data = (Path(local_file) if Path(local_file).is_absolute() else ROOT / local_file).read_bytes()
    else:
        data = download(source["url"], refresh)
    reader = csv.DictReader(io.StringIO(decode(data)))
    fieldnames = [name.strip() for name in reader.fieldnames or []]
    reader.fieldnames = fieldnames
    columns = source["columns"]
    if columns["name"] not in fieldnames:
        print(f"  × 名前の列「{columns['name']}」が見つからないので飛ばします（列: {'、'.join(fieldnames)}）", flush=True)
        return None
    missing = [key for key, column in columns.items() if column not in fieldnames]
    if missing:
        print(f"  △ 列が見つからない項目は空にします: {'、'.join(f'{key}（{columns[key]}）' for key in missing)}", flush=True)
        for key in missing:
            del columns[key]
    rows = list(reader)
    row_filter = source.get("filter")
    if row_filter:
        rows = [row for row in rows if (row.get(row_filter["column"]) or "").strip() in row_filter["values"]]
    return rows


# MARK: - 名前と距離

def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).translate(VARIANT_CHARACTERS)
    text = re.sub(r"[（(][^）)]*[）)]", "", text)  # 括弧書き（読み・別名）を外す
    text = re.sub(r"【[^】]*】", "", text)  # 【宇久島】のような地名の見出しを外す
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
          prefecture: str | None) -> tuple[str, int, float] | None:
    """行政の一覧の1行に当たる DB の寺社・城の id と、名前の合い方の点、距離（m。座標がなければ UNIQUE_NAME_DISTANCE）。
    当たらなければ None"""
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
        if candidates:
            best = min(candidates)
            return best[2], -best[0], best[1]
    if not prefecture:
        return None
    # 近くに当たらない・座標がない行は、同じ都道府県で名前の合う寺社が1か所だけなら当てる（同じ名前の寺社は多いので、2か所以上なら当てない）
    unique = unique_in_prefecture(connection, targets, prefecture)
    if unique is None:
        return None
    spot_id, score, spot_lat, spot_lon = unique
    meters = distance(lat, lon, spot_lat, spot_lon) if lat is not None and lon is not None else UNIQUE_NAME_DISTANCE
    if meters > UNIQUE_NAME_DISTANCE:
        return None
    return spot_id, score, meters


def unique_in_prefecture(connection: sqlite3.Connection, targets: set[str],
                         prefecture: str) -> tuple[str, int, float, float] | None:
    """都道府県の中で、名前が同じ寺社（括弧の中の別名・山号を外した名前を含む）が1か所だけならその id・点・座標。
    頭に言葉が付いた形までは広げない（「七夕のゆうべ in 四天王寺」のような行事の行が寺に当たる）"""
    same = [(spot_id, spot_lat, spot_lon) for spot_id, spot_name, spot_lat, spot_lon in connection.execute(
            "SELECT id, name, lat, lon FROM spots WHERE prefecture = ?", (prefecture,))
            if normalize(spot_name) in targets]
    if len(same) != 1:
        return None
    spot_id, spot_lat, spot_lon = same[0]
    return spot_id, EXACT_SCORE, spot_lat, spot_lon


# MARK: - 文化財の建物

def read_building_rows(source: dict, refresh: bool) -> list[dict] | None:
    """文化庁の検索結果の CSV（files に並べたもの）の行。書き出しを分けたときに重なった行は1つにする"""
    rows: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for file in source["files"]:
        file_rows = read_rows({**source, "file": file}, refresh)
        if file_rows is None:
            return None
        for row in file_rows:
            key = tuple((row.get(column) or "").strip() for column in source["columns"].values())
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def owner_names(row: dict, columns: dict) -> list[str]:
    """所有者名（改行で複数書かれることがある。「月山神社\n出羽神社\n湯殿山神社」）"""
    return [name for name in value(row, columns, "owner").splitlines() if name.strip()]


def building_aliases(row: dict, columns: dict, renames: dict[str, str]) -> set[str]:
    """建物の行から、当てるときに試す寺社の名前（名称・所有者名。正式な名前は aliases の呼び名にも替える）"""
    names = aliases(value(row, columns, "name"))
    for owner in owner_names(row, columns):
        names |= aliases(owner)
    for official, common in renames.items():
        names |= {name.replace(normalize(official), normalize(common)) for name in names if normalize(official) in name}
    return names


def match_building(connection: sqlite3.Connection, row: dict, columns: dict,
                   renames: dict[str, str]) -> tuple[str, str] | None:
    """文化財の建物の1行に当たる DB の寺社・城の id と名前。当たらなければ None"""
    lat, lon = number(row.get(columns["latitude"])), number(row.get(columns["longitude"]))
    targets = building_aliases(row, columns, renames)
    if not targets:
        return None
    if lat is not None and lon is not None:
        full_names = {normalize(value(row, columns, "name"))} | {name for name in targets if name}
        rows = connection.execute(
            "SELECT id, name, lat, lon FROM spots WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - BUILDING_SEARCH_DEGREES, lat + BUILDING_SEARCH_DEGREES,
             lon - BUILDING_SEARCH_DEGREES, lon + BUILDING_SEARCH_DEGREES))
        candidates = []
        for spot_id, spot_name, spot_lat, spot_lon in rows:
            meters = distance(lat, lon, spot_lat, spot_lon)
            if meters > BUILDING_MATCH_DISTANCE:
                continue
            spot = normalize(spot_name)
            score = max(name_score(target, spot) for target in targets)
            # 名称が寺社の名前で始まる建物（清水寺本堂・延暦寺根本中堂）はその寺社のもの
            if not score and len(spot) >= MIN_CONTAINED_LENGTH and any(name.startswith(spot) for name in full_names):
                score = PARTIAL_SCORE
            if score:
                candidates.append((-score, meters, spot_id, spot_name))
        if candidates:
            _, _, spot_id, spot_name = min(candidates)
            return spot_id, spot_name
    unique = unique_in_prefecture(connection, targets, value(row, columns, "prefecture"))
    if unique is None:
        return None
    spot_id, _, spot_lat, spot_lon = unique
    if lat is not None and lon is not None and distance(lat, lon, spot_lat, spot_lon) > UNIQUE_NAME_DISTANCE:
        return None
    spot_name = connection.execute("SELECT name FROM spots WHERE id = ?", (spot_id,)).fetchone()[0]
    return spot_id, spot_name


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def building_label(row: dict, columns: dict, spot_name: str) -> str:
    """画面に並べる建物の名前。棟名があれば棟名、なければ名称から寺社の名前を外したもの（清水寺本堂 → 本堂）。
    1件の中の「、」は「・」にする（「本社本殿、幣殿、拝殿」で1件。件どうしを「、」で区切るため）。
    括弧だけの棟名（姫路城の「(ニの渡櫓)」）は括弧を外す"""
    label = compact(value(row, columns, "building"))
    if not label:
        label = compact(value(row, columns, "name"))
        # 旧字体をそろえて比べる（1文字ずつ替えるので長さは変わらない。廣八幡神社／広八幡神社拝殿）
        variant = label.translate(VARIANT_CHARACTERS)
        for prefix in [spot_name, *owner_names(row, columns)]:
            prefix = compact(prefix).translate(VARIANT_CHARACTERS)
            if prefix and variant.startswith(prefix) and len(variant) > len(prefix):
                label = label[len(prefix):]
                break
    label = re.sub(r"^\((.+)\)$", r"\1", label)
    return label.replace("、", "・")


def building_order(label: str) -> tuple[bool, int, int]:
    """並べ替えの鍵。本社の建物を摂社・末社より先に、MAIN_BUILDINGS を含む建物ほど先に、同じなら名前の短い方を先に
    （「楼門東西廻廊(東)」より「楼門」）"""
    main = next((index for index, word in enumerate(MAIN_BUILDINGS) if word in label), len(MAIN_BUILDINGS))
    return bool(SUB_SHRINE_PATTERN.match(label)), main, len(label)


def properties_text(buildings: list[tuple[str, str]]) -> str:
    """種別と建物の名前の組を「国宝（2件）：本堂、三重塔」の行にまとめる（種別ごとに1行。多ければ「など」）"""
    lines = []
    for grade in PROPERTY_GRADES:
        labels = sorted(dict.fromkeys(label for building_grade, label in buildings if building_grade == grade),
                        key=building_order)
        if not labels:
            continue
        listed = "、".join(labels[:MAX_LISTED_BUILDINGS])
        more = " など" if len(labels) > MAX_LISTED_BUILDINGS else ""
        lines.append(f"{grade}（{len(labels)}件）：{listed}{more}")
    return "\n".join(lines)


def collect_cultural_properties(connection: sqlite3.Connection, source: dict,
                                rows: list[dict]) -> dict[str, dict]:
    """文化財の建物の行を寺社・城ごとにまとめ、cultural_properties だけを埋めた詳しい情報にする"""
    columns = source["columns"]
    renames = source.get("aliases", {})
    buildings: dict[str, list[tuple[str, str]]] = {}
    unmatched = []
    for row in rows:
        result = match_building(connection, row, columns, renames)
        if result is None:
            unmatched.append(value(row, columns, "name"))
            continue
        spot_id, spot_name = result
        buildings.setdefault(spot_id, []).append((value(row, columns, "grade"),
                                                  building_label(row, columns, spot_name)))
    found = {}
    for spot_id, pairs in buildings.items():
        detail = {key: "" for key in DETAIL_COLUMNS}
        detail.update(cultural_properties=properties_text(pairs), updated=source.get("updated", ""),
                      source=credit(source), license=source["license"])
        if has_content(detail):
            found[spot_id] = detail
    print(f"  {len(rows)} 棟のうち {len(rows) - len(unmatched)} 棟を {len(found)} か所に当てました", flush=True)
    if unmatched:
        names = list(dict.fromkeys(unmatched))
        print(f"  当たらなかった {len(names)} 件: {'、'.join(names[:60])}", flush=True)
    return found


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
        "cultural_properties": "",
        "updated": date_text(value(row, columns, "updated")) or source.get("updated", ""),
        "source": credit(source),
        "license": source["license"],
    }


def date_text(text: str) -> str:
    """「20140222」のような8桁の日付を「2014-02-22」にそろえる（ほかの書き方は公開元のまま）"""
    return f"{text[:4]}-{text[4:6]}-{text[6:]}" if re.fullmatch(r"\d{8}", text) else text


def credit(source: dict) -> str:
    """画面と meta に出す出典（公開元の名前とライセンス）"""
    return f"{source['name']}（{source['license']}）"


def merge(earlier: dict, later: dict) -> dict:
    """同じ寺社に2つの公開元が当たったとき、先の公開元の空の項目を後の公開元で埋め、使った公開元をどちらも出典に書く"""
    merged = dict(earlier)
    used_later = False
    for key in DETAIL_COLUMNS:
        if key in ("updated", "source", "license"):
            continue
        if not merged[key] and later[key]:
            merged[key] = later[key]
            used_later = True
    if used_later:
        merged["source"] = f"{earlier['source']}、{later['source']}"
        merged["license"] = f"{earlier['license']}、{later['license']}"
    return merged


DETAIL_COLUMNS = ("description", "days", "hours", "fee", "address", "phone", "url", "access", "parking",
                  "cultural_properties", "updated", "source", "license")


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
        ("detailsSources", "、".join(credit(source) for source in sources)),
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
    used_sources: list[dict] = []
    for source in sources:
        print(f"■ {credit(source)}", flush=True)
        if source.get("kind") == "culturalProperties":
            rows = read_building_rows(source, args.refresh)
            if rows is None:
                continue
            used_sources.append(source)
            for spot_id, detail in collect_cultural_properties(connection, source, rows).items():
                found[spot_id] = merge(found[spot_id], detail) if spot_id in found else detail
            continue
        rows = read_rows(source, args.refresh)
        if rows is None:
            continue
        used_sources.append(source)
        columns = source["columns"]
        # 同じ寺社に2行当たったら、名前のよく合う方、同じ点なら近い方、それも同じなら名前の短い方を残す
        # （大阪城に「大阪城天守閣」と「海洋堂フィギュアミュージアム ミライザ大阪城」が当たったら天守閣）。前の公開元で当たった寺社は、空の項目だけ埋める
        best: dict[str, tuple[tuple[int, float, int], dict]] = {}
        unmatched = []
        for row in rows:
            name = value(row, columns, "name")
            if EVENT_PATTERN.search(normalize(name)):
                continue
            result = match(connection, name, number(row.get(columns.get("latitude", ""))),
                           number(row.get(columns.get("longitude", ""))), source.get("prefecture"))
            if result is None:
                if SPOT_NAME_PATTERN.search(normalize(name)):
                    unmatched.append(name)
                continue
            spot_id, score, meters = result
            detail = details(row, source)
            if not has_content(detail):
                continue
            rank = (score, -meters, -len(normalize(name)))
            if spot_id not in best or rank > best[spot_id][0]:
                best[spot_id] = (rank, detail)
        for spot_id, (_, detail) in best.items():
            found[spot_id] = merge(found[spot_id], detail) if spot_id in found else detail
        print(f"  {len(rows)} 行のうち {len(best)} か所に当てました", flush=True)
        if unmatched:
            print(f"  寺社・城らしいのに当たらなかった {len(unmatched)} 件: {'、'.join(unmatched[:60])}", flush=True)

    write(connection, found, used_sources)
    connection.close()
    print(f"○ {len(found)} か所の詳しい情報を書き足しました: {args.db}")


if __name__ == "__main__":
    main()
