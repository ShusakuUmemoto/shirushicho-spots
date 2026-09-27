#!/usr/bin/env python3
"""巡礼リストの場所のうち、全国のデータ（spots.sqlite）にないものを足す。通信しない。

使い方（リポジトリのルートで）:
    python3 scripts/spot/pilgrimage_places.py            # 今の spots.sqlite に足し、データの版を書き直す
    python3 scripts/spot/pilgrimage_places.py --db 別の.sqlite --version-file 別の.txt  # 試すとき

- fetch.py は OpenStreetMap の登録のされ方（城・礼拝所）で集めるので、公園や遺跡として登録された城跡（上田城・甲府城など）が抜ける。
  巡礼リストの JSON（Resources/Pilgrimages。Wikidata の CC0）は名前・座標・QID を持つので、そこから埋める。
- 巡礼リストの場所ごとに、次の順で決める。
  1. 同じ QID の場所が DB にあれば何もしない。
  2. 同じ分類で MATCH_DISTANCE 以内に同じ名前の場所があれば、いちばん近いものに QID を付ける（QID のない場所だけ）。
     名前は details.py と同じくならし（旧字体・括弧書き）、別名も試す。後ろに「跡」「公園」などが付いた形（上田城跡公園）も同じとみなす。
     OpenStreetMap の名前は空白で区切った1語ずつも試す（「西国33番 谷汲山 華厳寺」「都農神社 日向國一之宮」）。
     頭に言葉が付いた形（播州清水寺／清水寺）は、PREFIXED_MATCH_DISTANCE 以内だけ同じとみなす（同じ名前の寺社は各地にあるため）。
     いちばん近い同じ名前の場所が別の QID を持っていれば、足さずにそのままにする（どちらが正しいかをここでは決めない）。
  3. どれでもなければ、巡礼リストの名前・座標で新しく足す。id は「q」と QID の数字（q969909）で、作り直しても変わらない。
- fetch.py は DB を作り直すときにこの処理を通す。単独で流したときは、データの版（meta の dataVersion と spots-version.txt）も書き直す。
  市区町村の表（spot_area）がある DB に単独で流したら、足した場所に市区町村を付けるため municipality.py も流し直す。
- 標準ライブラリだけで動く。
"""

import argparse
import json
import re
import unicodedata
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import details  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PILGRIMAGE_DIR = ROOT / "GoshuinApp" / "Resources" / "Pilgrimages"
DEFAULT_DB = ROOT / "GoshuinApp" / "Resources" / "Spots" / "spots.sqlite"
# 巡礼リストの座標と DB の場所が同じとみなす距離（m）。城跡は面の代表点が本丸から離れることがある
MATCH_DISTANCE = 1000.0
# 頭に言葉が付いた名前（播州清水寺）を同じとみなす距離（m）と、そのときの名前の短さの下限
PREFIXED_MATCH_DISTANCE = 100.0
MIN_PREFIXED_KEY_LENGTH = 3
# 巡礼リストの分類（JSON の category）と DB の分類（SpotDatabase.Category の rawValue）
LIST_CATEGORIES = {"神社": "shrine", "寺院": "temple", "城郭": "castle"}
# 後ろに付いても同じ場所とみなす言葉（details.py のものに、城跡の公園の呼び方を足す）
SAME_PLACE_SUFFIXES = details.SAME_PLACE_SUFFIXES + ("跡公園", "址公園", "城址公園")
# 新しく足す場所の id の頭（OpenStreetMap の n・w・r と重ならない）
WIKIDATA_ID_PREFIX = "q"
HIRAGANA = ("ぁ", "ゖ")
# 閉じていない括弧書き（「青葉山 松尾寺（matsuno o dera)」の全角と半角の食い違い）から後ろ
UNCLOSED_PARENTHESIS = re.compile(r"[（(].*$")


# MARK: - 巡礼リスト

def load_places(directory: Path = PILGRIMAGE_DIR) -> list[dict]:
    places = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        category = LIST_CATEGORIES[payload["category"]]
        for spot in payload["spots"]:
            places.append({
                "qid": spot["id"],
                "name": spot["name"],
                "aliases": spot.get("aliases", []),
                "category": category,
                "lat": spot["latitude"],
                "lon": spot["longitude"],
                "prefecture": spot["prefecture"],
            })
    return places


def is_kana(text: str) -> bool:
    return bool(text) and all(HIRAGANA[0] <= c <= HIRAGANA[1] or c in "ーゝゞ" for c in text)


def reading(place: dict) -> str:
    # 別名の中のひらがなだけのものを読みにする（ねむろはんとうチャシあとぐん のようにカタカナが混ざるものも、ひらがなにそろえる）
    for alias in place["aliases"]:
        hiragana = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in alias)
        if is_kana(hiragana):
            return hiragana
    return ""


def name_keys(place: dict) -> set[str]:
    names = [place["name"], *(alias for alias in place["aliases"] if not is_kana(alias))]
    return {key for name in names for key in details.aliases(name)}


# MARK: - 照らし合わせ

def spot_name_keys(spot_name: str) -> set[str]:
    # 名前全体と、空白で区切った1語ずつ（番号・山号・読みの付いた名前から寺社の名前を取り出す）
    text = unicodedata.normalize("NFKC", spot_name)
    words = UNCLOSED_PARENTHESIS.sub("", re.sub(r"[（(][^）)]*[）)]", "", text)).split()
    return {key for key in {details.normalize(text), *(details.normalize(word) for word in words)} if key}


def same_name(keys: set[str], spot_name: str, allow_prefix: bool = False) -> bool:
    for spot_key in spot_name_keys(spot_name):
        for key in keys:
            if spot_key == key or any(spot_key == key + suffix for suffix in SAME_PLACE_SUFFIXES):
                return True
            if allow_prefix and len(key) >= MIN_PREFIXED_KEY_LENGTH and spot_key.endswith(key):
                return True
    return False


def nearest_same_place(place: dict, spots: list[dict]) -> dict | None:
    keys = name_keys(place)
    candidates = []
    for spot in spots:
        if spot["category"] != place["category"]:
            continue
        meters = details.distance(place["lat"], place["lon"], spot["lat"], spot["lon"])
        if meters > MATCH_DISTANCE:
            continue
        if same_name(keys, spot["name"], allow_prefix=meters <= PREFIXED_MATCH_DISTANCE):
            candidates.append((meters, spot))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def new_spot(place: dict) -> dict:
    return {
        "id": WIKIDATA_ID_PREFIX + place["qid"].removeprefix("Q"),
        "name": place["name"],
        "kana": reading(place),
        "category": place["category"],
        "lat": round(place["lat"], 6),
        "lon": round(place["lon"], 6),
        "prefecture": place["prefecture"],
        "wikidata": place["qid"],
    }


def merge(spots: list[dict], places: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]], list[dict], list[tuple[dict, dict]]]:
    """spots（fetch.py の形の辞書）に巡礼リストの場所を足す。QID を付けた場所は書き換える。
    返すのは（足したあとの spots、QID を付けた（巡礼リストの場所、DB の場所）、新しく足した場所、
    別の QID を持つ同じ名前の場所があってそのままにした（巡礼リストの場所、DB の場所））"""
    known_qids = {spot["wikidata"] for spot in spots if spot["wikidata"]}
    tagged: list[tuple[dict, dict]] = []
    added: list[dict] = []
    conflicted: list[tuple[dict, dict]] = []
    for place in places:
        if place["qid"] in known_qids:
            continue
        spot = nearest_same_place(place, spots)
        if spot is not None and spot["wikidata"]:
            conflicted.append((place, spot))
            continue
        if spot is not None:
            spot["wikidata"] = place["qid"]
            tagged.append((place, spot))
        else:
            spot = new_spot(place)
            spots.append(spot)
            added.append(spot)
        known_qids.add(place["qid"])
    return spots, tagged, added, conflicted


def print_report(tagged: list[tuple[dict, dict]], added: list[dict], conflicted: list[tuple[dict, dict]]) -> None:
    print(f"○ QID を付けた場所: {len(tagged)} か所")
    for place, spot in tagged:
        print(f"  - {place['name']} → {spot['name']}（{spot['id']}）")
    print(f"○ 新しく足した場所: {len(added)} か所")
    for spot in added:
        print(f"  - {spot['name']}（{spot['prefecture']}・{spot['wikidata']}）")
    if conflicted:
        print(f"△ 同じ名前の場所が別の QID を持つのでそのままにした: {len(conflicted)} か所")
        for place, spot in conflicted:
            print(f"  - {place['name']}（{place['qid']}）→ {spot['name']}（{spot['id']}・{spot['wikidata']}）")


# MARK: - 単独で流すとき

SPOT_COLUMNS = ("id", "name", "kana", "category", "lat", "lon", "prefecture", "wikidata")


def read_spots(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(f"SELECT {', '.join(SPOT_COLUMNS)} FROM spots")
    return [dict(zip(SPOT_COLUMNS, row)) for row in rows]


def rewrite_spots(connection: sqlite3.Connection, spots: list[dict]) -> None:
    # 表の形（列と索引）は変えない。前の版の DB を落とした人のアプリも同じ SELECT で読める
    rows = sorted(spots, key=lambda s: (s["lat"], s["lon"]))
    connection.execute("DELETE FROM spots")
    connection.executemany(
        "INSERT INTO spots VALUES (:id, :name, :kana, :category, :lat, :lon, :prefecture, :wikidata)", rows)
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('count', ?)", (str(len(rows)),))
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('dataVersion', ?)", (details.data_version(connection),))
    connection.commit()
    connection.execute("VACUUM")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--version-file", type=Path, default=details.DEFAULT_VERSION_FILE)
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}（先に fetch.py を流す）")

    connection = sqlite3.connect(args.db)
    spots, tagged, added, conflicted = merge(read_spots(connection), load_places())
    print_report(tagged, added, conflicted)
    if not tagged and not added:
        print("足すものはありませんでした（DB は書き換えません）")
        connection.close()
        return
    rewrite_spots(connection, spots)
    details.write_version_file(connection, args.version_file)
    connection.close()


if __name__ == "__main__":
    main()
