#!/usr/bin/env python3
"""spots.sqlite の寺社・城を、日本語版 Wikipedia の記事の閲覧数で並べ、popular_places.json に書く（wikipedia.py が上から足す）。

使い方（リポジトリのルートで。DB は書き換えない）:
    python3 scripts/spot/popular.py --dry-run   # 通信せず、対象の数と問い合わせの回数の見込みを出す
    python3 scripts/spot/popular.py             # 記事の名前と閲覧数を問い合わせて一覧を書く
    python3 scripts/spot/popular.py --end 2026-09  # 数える12か月の終わりの月（既定は DEFAULT_END）
    python3 scripts/spot/popular.py --refresh   # 保存した答えを使わず取り直す

- 対象は Wikidata の QID がある場所すべて。QID から日本語版の記事の名前を引き（wikipedia.article_titles）、
  記事ごとに Wikimedia の Pageviews API で、人の閲覧（agent=user。ボットを除く）の12か月の合計を数える。
  12か月にするのは、初詣・紅葉などの季節と、一時のニュースでの偏りをならすため。閲覧の記録がない記事（404）は 0 回。
- OpenStreetMap の QID が、寺社でなく山・町・人物の記事を指していることがある（山の閲覧数で上位に来てしまう）。
  記事の名前と DB の名前が重ならないもの（片方がもう片方を含み、短い方が「寺・神社・城」などで終わる形でもない）は
  needsReview を付け、wikipedia.py は足さない。2つ以上の都道府県の場所に同じ QID が付いている所も同じ。確かめたら popular_overrides.json の accept（正しい）か exclude（誤り）に理由と一緒に書く。
- 答えは一時フォルダ（wikipedia.py と同じ）に保存し、やり直しても送らない。送るたびに REQUEST_INTERVAL 秒（閲覧数は PAGEVIEWS_INTERVAL 秒）あける。
- 標準ライブラリだけで動く。
"""

import argparse
import calendar
import json
import math
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from details import DEFAULT_DB, MOUNTAIN_PREFIX, SPOT_NAME_PATTERN, aliases, normalize  # noqa: E402
from wikipedia import (ENTITY_BATCH, POPULAR_OVERRIDES_FILE, POPULAR_PLACES_FILE, REQUEST_INTERVAL,  # noqa: E402
                       article_titles, fetch_json)

PAGEVIEWS_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/ja.wikipedia/all-access/user/"
# 数える長さ（か月）と、既定の終わりの月（終わった月まで。今月の途中は入れない）
WINDOW_MONTHS = 12
DEFAULT_END = "2026-09"
# 片方がもう片方を含むとき、短い方に要る長さ（「寺」「城」の1字だけで重なったとみなさない）
MIN_CONTAINED_LENGTH = 2
SECONDS_PER_MINUTE = 60
# 閲覧数の窓口（Wikimedia の REST API）は1秒に100回まで受ける決まりなので、MediaWiki の窓口より短くあける
PAGEVIEWS_INTERVAL = 0.2


# MARK: - 数える期間

def window(end: str) -> tuple[str, str, str]:
    """終わりの月（YYYY-MM）から、問い合わせの始まり・終わり（YYYYMMDD）と、表示の期間（2025-10〜2026-09）"""
    end_year, end_month = (int(part) for part in end.split("-"))
    start_index = end_year * 12 + (end_month - 1) - (WINDOW_MONTHS - 1)
    start_year, start_month = divmod(start_index, 12)
    start_month += 1
    last_day = calendar.monthrange(end_year, end_month)[1]
    label = f"{start_year:04d}-{start_month:02d}〜{end_year:04d}-{end_month:02d}"
    return f"{start_year:04d}{start_month:02d}01", f"{end_year:04d}{end_month:02d}{last_day:02d}", label


# MARK: - 取得

def pageviews_url(title: str, start: str, end: str) -> str:
    path = urllib.parse.quote(title.replace(" ", "_"), safe="")
    return f"{PAGEVIEWS_API}{path}/monthly/{start}/{end}"


def total_views(data: dict | None) -> int:
    """Pageviews API の答えの合計。記録がない（None）なら 0"""
    if data is None:
        return 0
    return sum(int(item.get("views", 0)) for item in data.get("items", []))


def article_views(titles: list[str], start: str, end: str, refresh: bool) -> dict[str, int]:
    views = {}
    for index, title in enumerate(titles, start=1):
        views[title] = total_views(fetch_json(pageviews_url(title, start, end), refresh, not_found_ok=True,
                                                  interval=PAGEVIEWS_INTERVAL))
        if index % 500 == 0:
            print(f"  閲覧数 {index}/{len(titles)}", flush=True)
    return views


# MARK: - 場所と名前

def spot_groups(connection: sqlite3.Connection) -> dict[str, dict]:
    """QID → その QID の場所の名前・都道府県・分類（同じ QID の行はまとめる）"""
    groups: dict[str, dict] = {}
    for qid, name, prefecture, category in connection.execute(
            "SELECT wikidata, name, prefecture, category FROM spots WHERE wikidata != '' ORDER BY id"):
        group = groups.setdefault(qid, {"names": [], "prefectures": [], "categories": []})
        for key, value in (("names", name), ("prefectures", prefecture), ("categories", category)):
            if value not in group[key]:
                group[key].append(value)
    return groups


def names_overlap(title: str, names: list[str]) -> bool:
    """記事の名前と DB の名前が同じ場所を指していそうか。
    ならした形（括弧書き・旧字体・山号を外す）が同じか、片方がもう片方を含み、短い方が寺社・城の名前の形で終わるとき
    （大阪城／大阪城天守閣は重なる。富士山／富士山本宮浅間大社は、短い方が山の名前なので重ならない）"""
    # 記事の名前の括弧は曖昧さ回避（長谷寺 (鎌倉市)）なので、中を別名として試さない
    title_keys = {normalize(title), MOUNTAIN_PREFIX.sub("", normalize(title))}
    for title_key in title_keys:
        for name in names:
            for name_key in aliases(name):
                if title_key == name_key:
                    return True
                shorter, longer = sorted((title_key, name_key), key=len)
                if len(shorter) >= MIN_CONTAINED_LENGTH and shorter in longer and SPOT_NAME_PATTERN.search(shorter):
                    return True
    return False


def ranked_places(groups: dict[str, dict], titles: dict[str, str], views: dict[str, int],
                  overrides: dict) -> list[dict]:
    """閲覧数の多い順（同じなら QID の順）の一覧。記事のない QID は入れない"""
    accepted, excluded = overrides.get("accept", {}), overrides.get("exclude", {})
    unknown = (set(accepted) | set(excluded)) - set(titles)
    if unknown:
        raise ValueError(f"popular_overrides.json の QID に、記事のある場所がありません: {', '.join(sorted(unknown))}")
    places = []
    for qid, title in titles.items():
        group = groups[qid]
        place = {"qid": qid, "title": title, "views": views.get(title, 0), **group}
        if qid in excluded:
            place["excluded"] = excluded[qid]
        elif qid not in accepted and (len(group["prefectures"]) > 1 or not names_overlap(title, group["names"])):
            # 2つ以上の都道府県の場所に同じ QID が付いていたら、どれかの QID が誤っている
            place["needsReview"] = True
        places.append(place)
    places.sort(key=lambda place: (-place["views"], int(place["qid"][1:])))
    return [{"rank": rank, **place} for rank, place in enumerate(places, start=1)]


# MARK: - 書き出し

def write_places(path: Path, label: str, places: list[dict]) -> None:
    """1か所を1行に書く（差分を見やすくする）"""
    lines = [json.dumps(place, ensure_ascii=False) for place in places]
    body = ",\n    ".join(lines)
    path.write_text(f'{{\n  "window": {json.dumps(label, ensure_ascii=False)},\n  "count": {len(places)},\n'
                    f'  "places": [\n    {body}\n  ]\n}}\n', encoding="utf-8")


def read_overrides(path: Path) -> dict:
    if not path.exists():
        return {}
    overrides = json.loads(path.read_text(encoding="utf-8"))
    for key in ("accept", "exclude"):
        for qid, reason in overrides.get(key, {}).items():
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"popular_overrides.json の {key} の {qid} に理由がありません")
    both = set(overrides.get("accept", {})) & set(overrides.get("exclude", {}))
    if both:
        raise ValueError(f"popular_overrides.json で accept と exclude の両方にある QID: {', '.join(sorted(both))}")
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--end", default=DEFAULT_END, help="数える12か月の終わりの月（YYYY-MM）")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="通信せず、対象の数と問い合わせの見込みを出す")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}")
    connection = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        start, end, label = window(args.end)
        groups = spot_groups(connection)
        overrides = read_overrides(POPULAR_OVERRIDES_FILE)
        print(f"■ QID のある場所: {len(groups)} 件（期間 {label}）", flush=True)
        if args.dry_run:
            entity_requests = math.ceil(len(groups) / ENTITY_BATCH)
            # 閲覧数は記事1つに1回。記事の数は記事の名前を引くまで分からないので、多くても QID の数
            longest = (entity_requests * REQUEST_INTERVAL + len(groups) * PAGEVIEWS_INTERVAL) / SECONDS_PER_MINUTE
            print(f"  記事の名前: {entity_requests} 回、閲覧数: 記事1つに1回（多くて {len(groups)} 回）")
            print(f"  保存した答えがなければ、長くて約 {math.ceil(longest)} 分（通信なしで終わりました）")
            return
        titles = article_titles(list(groups), args.refresh)
        print(f"  日本語版の記事があるのは {len(titles)} 件", flush=True)
        views = article_views(sorted(set(titles.values())), start, end, args.refresh)
        places = ranked_places(groups, titles, views, overrides)
        write_places(POPULAR_PLACES_FILE, label, places)
        review = [place for place in places if place.get("needsReview")]
        print(f"○ {len(places)} 件を閲覧数の順に書きました: {POPULAR_PLACES_FILE}")
        print(f"  名前が重ならず確かめが要る: {len(review)} 件（上位のもの）")
        for place in review[:30]:
            print(f"    {place['rank']}位 {place['qid']} 記事「{place['title']}」/ DB「{'・'.join(place['names'])}」"
                  f" {place['views']:,} 回")
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        raise SystemExit(f"閲覧数の一覧を作れませんでした: {error}") from error
    finally:
        connection.close()


if __name__ == "__main__":
    main()
