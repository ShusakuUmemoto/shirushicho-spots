#!/usr/bin/env python3
"""全国の神社・寺院・城の名前と座標を OpenStreetMap から取り、アプリが読む SQLite を書き出す。

使い方（リポジトリのルートで）:
    python3 scripts/spot/fetch.py            # 47 都道府県すべて
    python3 scripts/spot/fetch.py --refresh  # 保存した結果を使わず取り直す

- Overpass API に都道府県（ISO3166-2 の JP-01〜JP-47）ごとに1件ずつ問い合わせる。
- 対象は、名前のある 神社（place_of_worship + religion=shinto）・寺院（religion=buddhist）・城（historic=castle）。
  大きな寺社は境内が宗教用地（landuse=religious + religion）としてだけ登録されていることがある
  （伏見稲荷大社・嚴島神社・成田山新勝寺など）ので、それも拾う。
  名前のない小さな祠と、「本殿」「拝殿」のような境内の建物の名前だけのものは除く。way・relation は代表点（center）を使う。
- 同じ名前で 50m 以内のもの（node と way の重複など）は1件にまとめる。境内と同じ名前の建物は 500m 以内ならまとめる。
- 都道府県ごとの件数が少なすぎる・座標が日本の外にあるときは書き出さず、原因を表示して終わる。
- 問い合わせの結果は一時フォルダに保存し、同じ問い合わせはやり直しても送らない。
- Overpass の窓口につながらない・混んでいるときは、ほかの公開の窓口（OVERPASS_APIS）に切り替える。
- データは ODbL 1.0（© OpenStreetMap contributors）。出力の DB も ODbL 1.0 のもとにある。
- 標準ライブラリだけで動く。
"""

import hashlib
import http.client
import json
import math
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Overpass の公開窓口。つながらないときは次の窓口に切り替える（どの窓口も同じ OpenStreetMap のデータを持つ）
OVERPASS_APIS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "ShirushichoSpotFetch/0.1 (https://github.com/ShusakuUmemoto/shirushicho-spots)"
ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = ROOT / "GoshuinApp" / "Resources" / "Spots" / "spots.sqlite"
CACHE_DIR = Path(tempfile.gettempdir()) / "goshuin-spot-cache"
# 問い合わせの間隔（Overpass の公開窓口の負荷を抑える）（秒）
REQUEST_INTERVAL = 5.0
# 断られたときにやり直す回数（窓口を切り替えながら、どの窓口も3回ずつ試す）と、Retry-After がないときの待ち時間（秒）
MAX_RETRIES = 3 * len(OVERPASS_APIS) + 1
DEFAULT_RETRY_WAIT = 60
# Overpass が1件の問い合わせにかけてよい時間（秒）と、受け取りを待つ時間（秒）
QUERY_TIMEOUT = 600
READ_TIMEOUT = 900
# 同じ名前をひとつにまとめる距離（m）
DUPLICATE_DISTANCE = 50.0
# 境内（宗教用地）と同じ名前の礼拝所をまとめる距離（m）。境内の代表点は建物から離れていることがある
PRECINCT_DUPLICATE_DISTANCE = 500.0
# 都道府県ごとにこれより少なければ取りこぼしとみなす
MIN_SPOTS_PER_PREFECTURE = 100
# 日本の範囲（座標の検証用）
JAPAN_LAT = (20.0, 46.0)
JAPAN_LON = (122.0, 154.0)
# 地球の半径（m）
EARTH_RADIUS = 6_371_000.0
LICENSE = "© OpenStreetMap contributors, ODbL 1.0 (https://opendatacommons.org/licenses/odbl/)"

# 都道府県コード（JP-01〜JP-47）の順
PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
    "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

# アプリの分類（SpotDatabase.Category の rawValue と合わせる）
CATEGORY_SHRINE = "shrine"
CATEGORY_TEMPLE = "temple"
CATEGORY_CASTLE = "castle"
RELIGION_CATEGORIES = {"shinto": CATEGORY_SHRINE, "buddhist": CATEGORY_TEMPLE}
# 境内の建物の名前（寺社そのものの名前ではないので地図に出さない）
BUILDING_PART_NAMES = {
    "本殿", "拝殿", "幣殿", "社殿", "内拝殿", "外拝殿", "神楽殿", "本堂", "鐘楼", "鐘楼堂", "山門", "社務所", "手水舎",
}

REFRESH = False
# いまつながっている窓口（OVERPASS_APIS の番号）
endpoint_index = 0


# MARK: - 問い合わせ

def overpass_query(code: str) -> str:
    return f"""[out:json][timeout:{QUERY_TIMEOUT}];
area["ISO3166-2"="{code}"]["admin_level"="4"]->.pref;
(
  nwr["amenity"="place_of_worship"]["religion"~"^(shinto|buddhist)$"]["name"](area.pref);
  nwr["landuse"="religious"]["religion"~"^(shinto|buddhist)$"]["name"](area.pref);
  nwr["historic"="castle"]["name"](area.pref);
);
out center tags;"""


def fetch(query: str) -> dict:
    cache_path = CACHE_DIR / (hashlib.sha256(query.encode()).hexdigest() + ".json")
    if cache_path.exists() and not REFRESH:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        # 前の版は空の結果も保存していたので、空なら取り直す
        if cached.get("elements"):
            return cached
    # 窓口によっては都道府県の範囲（area）を持たず、空の結果を返す。空なら次の窓口で取り直す
    for _ in OVERPASS_APIS:
        text = request_with_retry(query)
        payload = json.loads(text)
        remark = payload.get("remark", "")
        # 時間切れ・メモリ不足のときは途中までの結果に remark が付く。保存すると取りこぼしが残るので止める
        if "error" in remark.lower() or "timed out" in remark.lower():
            raise SystemExit(f"Overpass が途中で止まりました: {remark}")
        if payload.get("elements"):
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
            return payload
        print(f"  {OVERPASS_APIS[endpoint_index]} の結果が空でした", flush=True)
        switch_endpoint()
    # どの窓口でも空なら、そのまま返す（件数の確かめで止まる）
    return payload


def switch_endpoint() -> None:
    global endpoint_index
    endpoint_index = (endpoint_index + 1) % len(OVERPASS_APIS)
    print(f"  窓口を切り替えます: {OVERPASS_APIS[endpoint_index]}", flush=True)


def request_with_retry(query: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(1, MAX_RETRIES + 1):
        wait = DEFAULT_RETRY_WAIT
        try:
            request = urllib.request.Request(OVERPASS_APIS[endpoint_index], data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
                text = response.read().decode("utf-8")
            time.sleep(REQUEST_INTERVAL)
            return text
        except urllib.error.HTTPError as error:
            if error.code not in (429, 503, 504) or attempt == MAX_RETRIES:
                raise
            retry_after = error.headers.get("Retry-After", "")
            wait = int(retry_after) if retry_after.isdigit() else DEFAULT_RETRY_WAIT
            print(f"  混んでいるため {wait} 秒待ってやり直します（{attempt}/{MAX_RETRIES - 1}）", flush=True)
            # 1つの窓口が混み続けることがあるので、待つあいだに次の窓口へ移しておく
            switch_endpoint()
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == MAX_RETRIES:
                raise
            # 窓口が止まっている・断られた・道がない（Network is unreachable）ときは、待たずに次の窓口へ
            if isinstance(error, urllib.error.URLError) and isinstance(error.reason, OSError):
                print(f"  窓口につながりませんでした（{attempt}/{MAX_RETRIES - 1}）: {error.reason!r}"[:200], flush=True)
                switch_endpoint()
                continue
            print(f"  受け取りが途中で切れたので {wait} 秒後にやり直します（{attempt}/{MAX_RETRIES - 1}）: {error!r}"[:200],
                  flush=True)
        time.sleep(wait)
    raise AssertionError("ここには来ない")


# MARK: - 読み取り

def to_hiragana(text: str) -> str:
    # カタカナ（ァ〜ヶ）をひらがなにそろえる（読みで探すときの揺れをなくす）
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def is_precinct(tags: dict) -> bool:
    # 境内（宗教用地）。礼拝所の印がなく、宗教用地としてだけ登録された寺社
    return tags.get("landuse") == "religious" and tags.get("amenity") != "place_of_worship"


def category(tags: dict) -> str | None:
    if tags.get("amenity") == "place_of_worship" or is_precinct(tags):
        return RELIGION_CATEGORIES.get(tags.get("religion", ""))
    if tags.get("historic") == "castle":
        return CATEGORY_CASTLE
    return None


def coordinate(element: dict) -> tuple[float, float] | None:
    if "lat" in element:
        return element["lat"], element["lon"]
    center = element.get("center")
    return (center["lat"], center["lon"]) if center else None


def parse(payload: dict, prefecture: str) -> list[dict]:
    spots = []
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        name = (tags.get("name:ja") or tags.get("name") or "").strip()
        kind = category(tags)
        point = coordinate(element)
        if not name or not kind or not point or name in BUILDING_PART_NAMES:
            continue
        kana = tags.get("name:ja-Hira") or tags.get("name:ja_kana") or tags.get("name:ja-Kana") or ""
        spots.append({
            "id": f"{element['type'][0]}{element['id']}",
            "name": name,
            "kana": to_hiragana(kana.strip()),
            "category": kind,
            "lat": round(point[0], 6),
            "lon": round(point[1], 6),
            "prefecture": prefecture,
            "wikidata": tags.get("wikidata", ""),
            # まとめるときだけ使う（DB には書かない）
            "precinct": is_precinct(tags),
        })
    return spots


# MARK: - まとめ

def distance(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    d_lat = lat2 - lat1
    d_lon = math.radians(b["lon"] - a["lon"])
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(h))


def richness(spot: dict) -> tuple[int, int, int]:
    # まとめるときに残す方: Wikidata・読みがあるもの、次に面（way・relation）より点
    return (bool(spot["wikidata"]), bool(spot["kana"]), spot["id"].startswith("n"))


def is_duplicate(spot: dict, other: dict) -> bool:
    limit = PRECINCT_DUPLICATE_DISTANCE if spot["precinct"] or other["precinct"] else DUPLICATE_DISTANCE
    return distance(spot, other) <= limit


def deduplicate(spots: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for spot in spots:
        by_id.setdefault(spot["id"], spot)  # 県境をまたぐ way は2つの県で出る
    by_name: dict[str, list[dict]] = {}
    for spot in sorted(by_id.values(), key=richness, reverse=True):
        kept = by_name.setdefault(spot["name"], [])
        if not any(is_duplicate(spot, other) for other in kept):
            kept.append(spot)
    return [spot for group in by_name.values() for spot in group]


# MARK: - 検証と書き出し

def validate(spots: list[dict]) -> list[str]:
    problems = []
    counts = {name: 0 for name in PREFECTURES}
    for spot in spots:
        counts[spot["prefecture"]] += 1
        if not (JAPAN_LAT[0] <= spot["lat"] <= JAPAN_LAT[1] and JAPAN_LON[0] <= spot["lon"] <= JAPAN_LON[1]):
            problems.append(f"{spot['name']}（{spot['id']}）の座標が日本の外です: {spot['lat']}, {spot['lon']}")
    for name, count in counts.items():
        if count < MIN_SPOTS_PER_PREFECTURE:
            problems.append(f"{name} が {count} 件しかありません（{MIN_SPOTS_PER_PREFECTURE} 件未満）")
    return problems


def write(spots: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    connection.executescript("""
        CREATE TABLE spots (
            id TEXT NOT NULL,
            name TEXT NOT NULL,
            kana TEXT NOT NULL,
            category TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            prefecture TEXT NOT NULL,
            wikidata TEXT NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    rows = sorted(spots, key=lambda s: (s["lat"], s["lon"]))
    connection.executemany(
        "INSERT INTO spots VALUES (:id, :name, :kana, :category, :lat, :lon, :prefecture, :wikidata)", rows)
    # 範囲で引くための索引（緯度で絞ってから経度で絞る）
    connection.execute("CREATE INDEX spots_lat_lon ON spots (lat, lon)")
    connection.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("builtAt", date.today().isoformat()),
        ("count", str(len(rows))),
        ("license", LICENSE),
    ])
    connection.commit()
    connection.execute("VACUUM")
    connection.close()
    temporary.replace(OUTPUT_PATH)


def main() -> None:
    global REFRESH
    REFRESH = "--refresh" in sys.argv[1:]
    spots: list[dict] = []
    for index, prefecture in enumerate(PREFECTURES, start=1):
        code = f"JP-{index:02d}"
        print(f"■ {prefecture}（{code}）", flush=True)
        found = parse(fetch(overpass_query(code)), prefecture)
        print(f"  {len(found)} 件", flush=True)
        spots.extend(found)

    spots = deduplicate(spots)
    problems = validate(spots)
    if problems:
        print(f"× {len(problems)} 件の問題があるため書き出しません:")
        for problem in problems[:50]:
            print(f"  - {problem}")
        sys.exit(1)

    write(spots)
    by_category = {kind: sum(1 for s in spots if s["category"] == kind)
                   for kind in (CATEGORY_SHRINE, CATEGORY_TEMPLE, CATEGORY_CASTLE)}
    size_mb = OUTPUT_PATH.stat().st_size / 1_000_000
    print(f"○ {len(spots)} か所を書き出しました: {OUTPUT_PATH.relative_to(ROOT)}（{size_mb:.1f} MB）")
    print(f"  神社 {by_category[CATEGORY_SHRINE]}・寺院 {by_category[CATEGORY_TEMPLE]}・城 {by_category[CATEGORY_CASTLE]}")


if __name__ == "__main__":
    main()
