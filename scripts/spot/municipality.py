#!/usr/bin/env python3
"""寺社・城の市区町村を国土数値情報「行政区域データ（N03）」から決め、DB に表 spot_area を書き足す。

使い方（リポジトリのルートで。fetch.py・details.py・wikipedia.py のあとに流す。通信は要らない）:
    python3 scripts/spot/municipality.py ~/Downloads/N03-20240101_GML/N03-20240101.geojson

- 元データは https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-2024.html の全国版の GeoJSON（市区町村ごとの多角形）。
  都道府県の境界（scripts/prefecture/build.py）が読む *_prefecture.geojson ではなく、合わせる前のもの。
- 点が入る多角形の市区町村名（N03_004）に政令市の区名（N03_005）をつなぐ（「京都市東山区」「天理市」「斑鳩町」。郡名は付けない）。
- どの多角形にも入らない点（海際・埋め立て地など）は、境界の外周の点がいちばん近い市区町村にする（MAX_NEAREST_DEGREES より遠ければ付けない）。
- 同じ場所の名前が同じ寺社を見分けるために一覧に出す（前の版の DB にはない表なので、アプリは表があるかを確かめてから読む）。
- 書いたあと meta の dataVersion と spots-version.txt を作り直す（アプリは版が違えば手元の DB を落とし直す）。
- 標準ライブラリだけで動く。出典の表記は設定のクレジットにある国土数値情報と同じ。
"""

import argparse
import datetime
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from details import DEFAULT_DB, DEFAULT_VERSION_FILE, data_version, write_version_file  # noqa: E402

# 多角形を引く升目の大きさ（度）。点ごとに調べる多角形を、同じ升目にかかるものだけに絞る
GRID_DEGREES = 0.05
# どの多角形にも入らない点を、いちばん近い市区町村にする距離の上限（度。約1km）
MAX_NEAREST_DEGREES = 0.01


def municipality_name(properties: dict) -> str:
    """市区町村名と政令市の区名（郡名は付けない）"""
    return (properties.get("N03_004") or "") + (properties.get("N03_005") or "")


def load_polygons(path: Path) -> list[tuple[str, list, tuple]]:
    """(市区町村名, 輪の並び, 外接矩形) の並び。MultiPolygon は多角形ごとに分ける"""
    print(f"■ 境界を読んでいます: {path}", flush=True)
    with path.open(encoding="utf-8") as file:
        collection = json.load(file)
    polygons = []
    for feature in collection["features"]:
        name = municipality_name(feature["properties"])
        geometry = feature.get("geometry")
        if not name or not geometry:
            continue
        parts = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        for rings in parts:
            outer = rings[0]
            lons = [point[0] for point in outer]
            lats = [point[1] for point in outer]
            polygons.append((name, rings, (min(lons), min(lats), max(lons), max(lats))))
    print(f"  {len(polygons)} の多角形", flush=True)
    return polygons


def grid_cells(bbox: tuple) -> list[tuple[int, int]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return [(x, y)
            for x in range(math.floor(min_lon / GRID_DEGREES), math.floor(max_lon / GRID_DEGREES) + 1)
            for y in range(math.floor(min_lat / GRID_DEGREES), math.floor(max_lat / GRID_DEGREES) + 1)]


def cell_of(lon: float, lat: float) -> tuple[int, int]:
    return math.floor(lon / GRID_DEGREES), math.floor(lat / GRID_DEGREES)


def build_grid(polygons: list) -> dict:
    grid = defaultdict(list)
    for index, (_, _, bbox) in enumerate(polygons):
        for cell in grid_cells(bbox):
            grid[cell].append(index)
    return grid


def inside_ring(lon: float, lat: float, ring: list) -> bool:
    """偶奇規則（点から右へ伸ばした線が辺と交わる回数）"""
    inside = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index - 1][0], ring[index - 1][1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def inside_polygon(lon: float, lat: float, rings: list) -> bool:
    """外周の中で、穴（2つ目からの輪）の外"""
    return inside_ring(lon, lat, rings[0]) and not any(inside_ring(lon, lat, hole) for hole in rings[1:])


def in_bbox(lon: float, lat: float, bbox: tuple, margin: float = 0) -> bool:
    return bbox[0] - margin <= lon <= bbox[2] + margin and bbox[1] - margin <= lat <= bbox[3] + margin


def nearest_municipality(lon: float, lat: float, polygons: list, grid: dict) -> str | None:
    """外周の点がいちばん近い市区町村（MAX_NEAREST_DEGREES の中だけ）"""
    near = set()
    for cell in grid_cells((lon - MAX_NEAREST_DEGREES, lat - MAX_NEAREST_DEGREES,
                            lon + MAX_NEAREST_DEGREES, lat + MAX_NEAREST_DEGREES)):
        near.update(grid.get(cell, []))
    best_name, best_distance = None, MAX_NEAREST_DEGREES ** 2
    lat_scale = math.cos(math.radians(lat)) ** 2
    for index in near:
        name, rings, bbox = polygons[index]
        if not in_bbox(lon, lat, bbox, MAX_NEAREST_DEGREES):
            continue
        for point in rings[0]:
            distance = (point[1] - lat) ** 2 + (point[0] - lon) ** 2 * lat_scale
            if distance < best_distance:
                best_name, best_distance = name, distance
    return best_name


def municipality_of(lon: float, lat: float, polygons: list, grid: dict) -> tuple[str | None, bool]:
    """(市区町村名, 多角形の中に入ったか)"""
    for index in grid.get(cell_of(lon, lat), []):
        name, rings, bbox = polygons[index]
        if in_bbox(lon, lat, bbox) and inside_polygon(lon, lat, rings):
            return name, True
    return nearest_municipality(lon, lat, polygons, grid), False


def write(connection: sqlite3.Connection, rows: dict[str, str]) -> None:
    connection.execute("DROP TABLE IF EXISTS spot_area")
    connection.execute("CREATE TABLE spot_area (id TEXT PRIMARY KEY, municipality TEXT NOT NULL) WITHOUT ROWID")
    connection.executemany("INSERT INTO spot_area VALUES (?, ?)", sorted(rows.items()))
    connection.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
        ("areaCount", str(len(rows))),
        ("dataVersion", data_version(connection)),
        ("updatedAt", datetime.date.today().isoformat()),
    ])
    connection.commit()
    connection.execute("VACUUM")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("boundaries", type=Path, help="N03 の全国版の GeoJSON（市区町村ごと）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}（先に fetch.py を流す）")
    if not args.boundaries.is_file():
        raise SystemExit(f"境界の GeoJSON がありません: {args.boundaries}")
    polygons = load_polygons(args.boundaries)
    grid = build_grid(polygons)

    connection = sqlite3.connect(args.db)
    rows: dict[str, str] = {}
    nearest_count = 0
    missing = []
    for spot_id, name, lat, lon in connection.execute("SELECT id, name, lat, lon FROM spots"):
        municipality, inside = municipality_of(lon, lat, polygons, grid)
        if municipality is None:
            missing.append(f"{name}（{spot_id}）")
            continue
        rows[spot_id] = municipality
        nearest_count += 0 if inside else 1

    write(connection, rows)
    print(f"○ {len(rows)} か所の市区町村を書きました（境界の外で近くの市区町村にした {nearest_count} か所）: {args.db}")
    if missing:
        print(f"  付けられなかった {len(missing)} か所: {'、'.join(missing[:60])}")
    # アプリに入れる DB を作ったときだけ、本体の版のファイルも書き換える（試しの DB で本体の版を変えない）
    if args.db.resolve() == DEFAULT_DB.resolve():
        write_version_file(connection, DEFAULT_VERSION_FILE)
    connection.close()


if __name__ == "__main__":
    main()
