#!/usr/bin/env python3
"""日本語版 Wikipedia の記事から、寺社・城の「鍵」（ご祭神・ご本尊・宗派・社格・城郭構造など）と冒頭の数文を取り、spots.sqlite に書き足す。

使い方（リポジトリのルートで。details.py のあとに流す。fetch.py は DB を作り直すので、そのあとは details.py → wikipedia.py の順に流し直す）:
    python3 scripts/spot/wikipedia.py            # 国宝・重要文化財のある寺社・城、巡礼リストの場所、代表的な場所、閲覧数の上位
    python3 scripts/spot/wikipedia.py --refresh  # 保存した答えを使わず取り直す
    python3 scripts/spot/wikipedia.py --db 別の.sqlite  # 試すとき

- 集めるのは、国宝・重要文化財の建物がある寺社・城、巡礼リストの場所、featured_places.json で選んだ代表的な場所。
  代表的な場所の QID・名前・都道府県が DB と一致するか、通信の前に確かめる。
  残りは、popular.py が作る popular_places.json（日本語版の記事の閲覧数の順）の上から、合計が TARGET_TOTAL か所になるまで足す。
  閲覧数はここでは問い合わせない（一覧はファイルに残し、選び方を再現できるようにする）。
  順位の線が動いても、一度入れた記事は外さない（合計は TARGET_TOTAL を少し超える）。
- --dry-run は通信も DB の変更もせず、対象と選定理由を --report の JSON に書く。
  通常実行では同じ JSON に説明・情報欄の欠落も書く。既存の記事が取れなかったときは DB を書き換えずに止まる。
- Wikidata の QID から日本語版の記事の名前を引き（wbgetentities の sitelinks）、記事の本文（wikitext）の情報欄と、冒頭の文（TextExtracts）を読む。
  情報欄は型の名前（日本の寺院・神社・日本の城 など）に頼らず、主祭神・本尊・城郭構造などの項目を持つ最初の型を使う。
- 値は書式（脚注・リンク・改行・読みがなだけの括弧）を外して短くするだけにし、言い換えない。多ければ MAX_ITEMS 件までにして「など」を付ける。
  ご利益は情報欄にないので作らない。
- 冒頭の文は、最初の段落の文を SUMMARY_MAX_LENGTH 字まで（文の途中で切らない）そのまま使う。
- 記事の名前・版の日付・ライセンス（CC BY-SA 4.0）も一緒に持つ。アプリでは文のすぐ下に記事へのリンクを出し、ライセンスは設定のクレジットに出す。
- 表は spot_wiki（QID ごとに1行）。spots と spot_details には手を付けない。DB の版（details.data_version）も書き直す。
- 答えは一時フォルダに保存し、やり直しても送らない。Wikidata・Wikipedia の窓口の決まりに合わせ、送るたびに REQUEST_INTERVAL 秒あける
  （maxlag は付けない。付けると断られ続ける）。
- アプリに入れる DB を作ったときは、梅プランの人に出す1文目の JSON（summaries.py）も書き直す。
- 標準ライブラリだけで動く。
"""

import argparse
import datetime
import glob
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summaries import DEFAULT_OUT as DEFAULT_SUMMARIES_FILE, write_summaries  # noqa: E402
from details import (CACHE_DIR, DEFAULT_DB, DEFAULT_VERSION_FILE, MAX_RETRIES, READ_TIMEOUT, RETRY_WAIT,  # noqa: E402
                     USER_AGENT, data_version, write_version_file)

ROOT = Path(__file__).resolve().parents[2]
FEATURED_PLACES_FILE = Path(__file__).with_name("featured_places.json")
POPULAR_PLACES_FILE = Path(__file__).with_name("popular_places.json")
POPULAR_OVERRIDES_FILE = Path(__file__).with_name("popular_overrides.json")
# 集める場所の合計の目安（文化財・巡礼リスト・代表的な場所の残りを、閲覧数の上位で埋める）
TARGET_TOTAL = 3000
DEFAULT_REPORT = ROOT / "archive" / "spot" / "wikipedia-report.json"
PILGRIMAGE_FILES = ROOT / "GoshuinApp" / "Resources" / "Pilgrimages" / "pilgrimage-*.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://ja.wikipedia.org/w/api.php"
ARTICLE_URL = "https://ja.wikipedia.org/wiki/"
WIKI_CACHE_DIR = CACHE_DIR / "wikipedia"
LICENSE = "CC BY-SA 4.0"
# 1回に問い合わせる数（wbgetentities と本文は 50 件まで、冒頭の文〔exintro〕は 20 件まで）
ENTITY_BATCH = 50
CONTENT_BATCH = 50
EXTRACT_BATCH = 20
# 送るたびにあける秒数
REQUEST_INTERVAL = 1.0
# 冒頭の文の長さ（字）。文の途中では切らない（最初の1文がこれより長いときだけ切って「…」を付ける）
SUMMARY_MAX_LENGTH = 160
# 1つの鍵に並べる数と長さ（字）。超えたら「など」を付ける（一の宮の祭神は10柱を超えることがある）
MAX_ITEMS = 4
MAX_VALUE_LENGTH = 60
# 書く鍵と、情報欄の項目の名前（前にあるものほど先に使う。城の情報欄〔日本の城郭概要表〕は英語の名前）
FIELDS = {
    "deity": ("主祭神", "祭神"),
    "honzon": ("本尊",),
    "sect": ("宗派", "宗旨"),
    "rank": ("社格等", "社格"),
    "founded": ("創建", "創建年", "築城年", "build_y"),
    "founder": ("開基", "開山", "築城主", "builders"),
    "castle_structure": ("城郭構造", "struct"),
    "tenshu_structure": ("天守構造", "tower_struct"),
}
# 情報欄とみなす項目（どれか1つを持つ最初の型を情報欄にする）。創建・開基はほかの型にもある名前なので目印にしない
INFOBOX_KEYS = {name for key in ("deity", "honzon", "sect", "rank", "castle_structure", "tenshu_structure")
                for name in FIELDS[key]} | {"築城主", "builders"}
# 値がないことを書いた値（「天守構造 = なし」）。鍵を出さない
EMPTY_VALUES = {"なし", "無し", "不明", "-", "－", "—", "―"}
# 中身の1つ目を見せる型（{{仮リンク|大江山|en|...}} → 大江山）。型の名前は小文字・「_」を空白にした形で比べる
FIRST_ARGUMENT_TEMPLATES = {"仮リンク", "ruby", "読み仮名", "読み仮名 ruby不使用", "nowrap", "small", "nihongo",
                            "jis2004フォント", "補助漢字フォント", "unicode", "lang-ja"}
# 中身の2つ目を見せる型（{{lang|en|Himeji Castle}} → Himeji Castle、{{color|red|文字}} → 文字）
SECOND_ARGUMENT_TEMPLATES = {"lang", "color", "font color"}
# 記号を出す型（{{ndash}} は年の範囲「724年–729年」の間に置かれる）
SYMBOL_TEMPLATES = {"ndash": "–", "snd": "–", "mdash": "—", "spaced ndash": " – ", "sp": " ", "·": "・", "dot": "・", "〜": "〜"}
# 中身を1行ずつ並べる型（{{Ublist|天照大神|豊受大神}} → 1つずつの値）
LIST_TEMPLATES = {"ublist", "unbulleted list", "plainlist", "indented plainlist", "cslist", "flatlist", "hlist"}
# 読みがな・送りがなだけの括弧（「天照大御神（あまてらすおおみかみ）」「出雲大社（いずもおおやしろ / いずもたいしゃ）」の括弧）
KANA_PARENTHESES = re.compile(r"[（(][぀-ヿー・/／、\s]+[）)]")
# 値を区切る記号（改行・読点。括弧の中の読点では区切らない）
ITEM_SEPARATORS = {"\n", "、", ",", "，"}
OPENING_PARENTHESES = "（("
CLOSING_PARENTHESES = "）)"


# MARK: - 取得

def request(api: str, params: dict, refresh: bool) -> dict:
    """MediaWiki の窓口に問い合わせた JSON。同じ問い合わせは保存した答えを返す"""
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    return fetch_json(f"{api}?{query}", refresh)


def fetch_json(url: str, refresh: bool, not_found_ok: bool = False, interval: float | None = None) -> dict | None:
    """URL の JSON。同じ URL は保存した答えを返す。not_found_ok なら、404（閲覧数の記録がない記事など）は None を返して残す。
    interval は送る前にあける秒数（既定は REQUEST_INTERVAL。窓口の決まりが違うときだけ変える）"""
    cache_path = WIKI_CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    http_request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_INTERVAL if interval is None else interval)
            with urllib.request.urlopen(http_request, timeout=READ_TIMEOUT) as response:
                data = json.loads(response.read())
            if "error" in data:
                raise SystemExit(f"窓口がエラーを返しました: {data['error']}\n  {url[:200]}")
            WIKI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if not_found_ok and isinstance(error, urllib.error.HTTPError) and error.code == 404:
                WIKI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text("null", encoding="utf-8")
                return None
            if attempt == MAX_RETRIES:
                raise SystemExit(f"取得できませんでした: {url[:200]}\n  {error!r}")
            print(f"  取得できなかったので {RETRY_WAIT} 秒後にやり直します（{attempt}/{MAX_RETRIES - 1}）: {error!r}"[:200],
                  flush=True)
            time.sleep(RETRY_WAIT)
    raise AssertionError("ここには来ない")


def batches(items: list, size: int) -> list[list]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def article_titles(qids: list[str], refresh: bool) -> dict[str, str]:
    """QID → 日本語版の記事の名前（記事のない QID は入らない）"""
    titles = {}
    for batch in batches(qids, ENTITY_BATCH):
        data = request(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(batch), "props": "sitelinks",
                                      "sitefilter": "jawiki"}, refresh)
        for qid, entity in data.get("entities", {}).items():
            title = entity.get("sitelinks", {}).get("jawiki", {}).get("title")
            if title:
                titles[qid] = title
    return titles


def article_contents(titles: list[str], refresh: bool) -> dict[str, tuple[str, str]]:
    """記事の名前 → （本文の wikitext・版の日付）"""
    contents = {}
    for batch in batches(titles, CONTENT_BATCH):
        data = request(WIKIPEDIA_API, {"action": "query", "prop": "revisions", "rvprop": "content|timestamp",
                                       "rvslots": "main", "titles": "|".join(batch)}, refresh)
        for page in data.get("query", {}).get("pages", []):
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            revision = revisions[0]
            text = revision.get("slots", {}).get("main", {}).get("content", "")
            contents[page["title"]] = (text, revision.get("timestamp", "")[:10])
    return contents


def article_extracts(titles: list[str], refresh: bool) -> dict[str, str]:
    """記事の名前 → 冒頭の文（書式を外した文）"""
    extracts = {}
    for batch in batches(titles, EXTRACT_BATCH):
        data = request(WIKIPEDIA_API, {"action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1",
                                       "exlimit": str(EXTRACT_BATCH), "titles": "|".join(batch)}, refresh)
        for page in data.get("query", {}).get("pages", []):
            if page.get("extract"):
                extracts[page["title"]] = page["extract"]
    return extracts


# MARK: - 情報欄

def split_top_level(text: str) -> list[str]:
    """型の中身を、中の型・リンクの外の「|」で分ける（[[a|b]] や {{lang|en|x}} の「|」では分けない）"""
    parts, depth, start, index = [], 0, 0, 0
    while index < len(text):
        pair = text[index:index + 2]
        if pair in ("{{", "[["):
            depth += 1
            index += 2
            continue
        if pair in ("}}", "]]"):
            depth -= 1
            index += 2
            continue
        if text[index] == "|" and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return parts


def top_level_templates(wikitext: str) -> list[str]:
    """本文の一番外側の型の中身（{{ と }} の内側）を、出てくる順に"""
    templates, depth, start, index = [], 0, 0, 0
    while index < len(wikitext) - 1:
        pair = wikitext[index:index + 2]
        if pair == "{{":
            if depth == 0:
                start = index + 2
            depth += 1
            index += 2
        elif pair == "}}" and depth > 0:
            depth -= 1
            if depth == 0:
                templates.append(wikitext[start:index])
            index += 2
        else:
            index += 1
    return templates


def infobox(wikitext: str) -> dict[str, str]:
    """情報欄の項目（名前 → 書式を外す前の値）。主祭神・本尊・城郭構造などを持つ最初の型。なければ空"""
    text = re.sub(r"<!--.*?-->", "", wikitext, flags=re.DOTALL)
    for template in top_level_templates(text):
        fields = {}
        for part in split_top_level(template)[1:]:
            key, separator, raw = part.partition("=")
            if separator:
                fields[key.strip()] = raw.strip()
        if INFOBOX_KEYS & {key for key, raw in fields.items() if raw}:
            return fields
    return {}


def resolve_template(body: str) -> str:
    """一番内側の型1つを、見せる文字に置き換える。知らない型（脚注・要出典など）は消す"""
    parts = split_top_level(body)
    name = parts[0].strip().lower().replace("_", " ")
    arguments = [part.strip() for part in parts[1:] if "=" not in part]
    if name in FIRST_ARGUMENT_TEMPLATES:
        return arguments[0] if arguments else ""
    if name in SECOND_ARGUMENT_TEMPLATES or name.startswith("lang-"):
        return arguments[-1] if arguments else ""
    if name in LIST_TEMPLATES:
        return "\n".join(arguments)
    if name in SYMBOL_TEMPLATES:
        return SYMBOL_TEMPLATES[name]
    if name == "和暦":
        # {{和暦|710}} は「和銅3年（710年）」と出る型。年だけを残す
        return f"{arguments[0]}年" if arguments else ""
    return ""


def plain_text(raw: str) -> str:
    """wikitext の値から書式を外した文字（言い換えはしない）"""
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    text = re.sub(r"<ref[^>/]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # 内側の型から順に置き換える（{{Refnest|{{Sfn|...}}}} も外まで消える）
    while True:
        replaced = re.sub(r"\{\{([^{}]*)\}\}", lambda match: resolve_template(match.group(1)), text)
        if replaced == text:
            break
        text = replaced
    text = re.sub(r"\[\[(?:ファイル|画像|File|Image):[^\]]*\]\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", "", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    # 全角の括弧・数字は画面にそのまま出すので、NFKC でならさない
    text = text.replace("&nbsp;", " ")
    text = KANA_PARENTHESES.sub("", text)
    # 「※共に焼失し非現存※」のような編集の注記
    text = re.sub(r"※[^※\n]*※", "", text)
    return text


def split_outside_parentheses(text: str) -> list[str]:
    """括弧の外の区切り（ITEM_SEPARATORS）で分ける（「田村大神（倭迹迹日百襲姫命、猿田彦大神）」は1つのまま）"""
    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
        if character in OPENING_PARENTHESES:
            depth += 1
        elif character in CLOSING_PARENTHESES:
            depth = max(depth - 1, 0)
        elif character in ITEM_SEPARATORS and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def items_of(raw: str) -> list[str]:
    """値を1つずつに分けたもの（改行・読点・箇条書きの印で区切る。重なりは1つに）"""
    items = []
    for item in split_outside_parentheses(plain_text(raw)):
        item = re.sub(r"^[*#:;・\s]+", "", item).strip()
        # 括弧の中の改行（「1850年再<br />外観復元1958年再」）は読点にする
        item = re.sub(r"\s*\n\s*", "、", item)
        item = re.sub(r"\s+", " ", item)
        if not item or item in EMPTY_VALUES:
            continue
        # 改行のあとの括弧書きは前の値の補足（「連立式望楼型5重6階<br />（1609年）」）なので、前の値につなげる
        if items and item[0] in "（(":
            items[-1] += item
        elif item not in items:
            items.append(item)
    return items


def shortened(items: list[str]) -> str:
    """MAX_ITEMS 件・MAX_VALUE_LENGTH 字までを「、」でつなぐ。超えたら「など」を付ける（1件目が長ければその1件だけ）"""
    kept: list[str] = []
    for item in items[:MAX_ITEMS]:
        if kept and len("、".join([*kept, item])) > MAX_VALUE_LENGTH:
            break
        kept.append(item)
    text = "、".join(kept)
    return f"{text} など" if len(kept) < len(items) else text


def keys_of(fields: dict[str, str]) -> dict[str, str]:
    """情報欄の項目から、書く鍵（deity・honzon など → 値）。値のない鍵は空"""
    keys = {}
    for key, names in FIELDS.items():
        keys[key] = next((shortened(items) for name in names if (items := items_of(fields.get(name, "")))), "")
    return keys


# MARK: - 冒頭の文

def summary_of(extract: str) -> str:
    """冒頭の文の最初の段落から、SUMMARY_MAX_LENGTH 字までの文（文の途中では切らない）"""
    paragraph = next((line.strip() for line in extract.splitlines() if line.strip()), "")
    paragraph = KANA_PARENTHESES.sub("", paragraph)
    # 文ごとに分ける（区切りの「。」は文に残す。最後の文は「。」で終わらないことがある）
    sentences = re.findall(r"[^。]+。?", paragraph)
    summary = ""
    for sentence in sentences:
        if summary and len(summary + sentence) > SUMMARY_MAX_LENGTH:
            break
        summary += sentence
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[:SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


# MARK: - 集める先

def target_places(connection: sqlite3.Connection, featured_file: Path = FEATURED_PLACES_FILE,
                  pilgrimage_files: str = str(PILGRIMAGE_FILES), popular_file: Path = POPULAR_PLACES_FILE,
                  total: int = TARGET_TOTAL, overrides_file: Path = POPULAR_OVERRIDES_FILE) -> dict[str, dict]:
    """取得対象の名前と選定理由。同じ QID の理由はまとめる。
    文化財・巡礼リスト・代表的な場所はすべて入れ、残りを閲覧数の上位で total か所まで埋める。
    最後に、すでに spot_wiki にある記事を残す（このぶんは total を超えてよい）。
    ただし popular_overrides.json の exclude に書いた記事は残さない（誤りと分かった記事を外す道）。"""
    targets: dict[str, dict] = {}

    def add(qid: str, name: str, reason: str) -> None:
        if not re.fullmatch(r"Q[1-9][0-9]*", qid):
            raise ValueError(f"Wikidata の ID が不正です: {name} / {qid}")
        target = targets.setdefault(qid, {"qid": qid, "names": [], "reasons": []})
        if name not in target["names"]:
            target["names"].append(name)
        if reason not in target["reasons"]:
            target["reasons"].append(reason)

    for qid, name in connection.execute(
            "SELECT s.wikidata, s.name FROM spots s JOIN spot_details d ON d.id = s.id "
            "WHERE (d.cultural_properties LIKE '%国宝%' OR d.cultural_properties LIKE '%重要文化財%') "
            "AND s.wikidata != '' ORDER BY s.id"):
        add(qid, name, "国宝・重要文化財の建物")
    for path in sorted(glob.glob(pilgrimage_files)):
        pilgrimage = json.loads(Path(path).read_text(encoding="utf-8"))
        for spot in pilgrimage["spots"]:
            if spot["id"].startswith("Q"):
                add(spot["id"], spot["name"], "巡礼リストの場所")

    featured = json.loads(featured_file.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for place in featured["places"]:
        qid, name, prefecture, reason = (place[key] for key in ("qid", "name", "prefecture", "reason"))
        if not all(isinstance(value, str) and value.strip() for value in (qid, name, prefecture, reason)):
            raise ValueError("代表的な場所には ID・名前・都道府県・選定理由が必要です")
        if qid in seen:
            raise ValueError(f"代表的な場所の ID が重なっています: {qid}")
        seen.add(qid)
        matched = connection.execute(
            "SELECT 1 FROM spots WHERE wikidata = ? AND name = ? AND prefecture = ?",
            (qid, name, prefecture)).fetchone()
        if matched is None:
            raise ValueError(f"代表的な場所が DB と一致しません: {name}（{prefecture}）/ {qid}")
        add(qid, name, f"代表的な場所: {reason}")

    # 閲覧数の多い順に、合計が total に届くまで足す（popular.py が作る。まだなければ足さない）
    if popular_file.exists():
        popular = json.loads(popular_file.read_text(encoding="utf-8"))
        for place in popular["places"]:
            if len(targets) >= total:
                break
            if place.get("needsReview") or place.get("excluded"):
                continue
            qid = place["qid"]
            names = [name for (name,) in connection.execute("SELECT DISTINCT name FROM spots WHERE wikidata = ?", (qid,))]
            if not names:
                raise ValueError(f"閲覧数の一覧の場所が DB にありません: {place.get('title')} / {qid}")
            add(qid, names[0], f"閲覧数の上位（{place['rank']}位・{popular['window']} に {place['views']:,} 回）")

    # 一度入れた記事は外さない（順位の線が動いても、利用者がもう見ている情報を次のデータで消さない）。total の外に足す
    if has_table(connection, "spot_wiki"):
        excluded = excluded_articles(overrides_file)
        for qid, title in connection.execute("SELECT qid, title FROM spot_wiki ORDER BY qid"):
            if qid not in targets and qid not in excluded:
                add(qid, title, "前から入っている記事")
    return dict(sorted(targets.items(), key=lambda item: int(item[0][1:])))


def has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def excluded_articles(overrides_file: Path = POPULAR_OVERRIDES_FILE) -> set[str]:
    """popular_overrides.json の exclude の QID（誤りと分かった記事。前からある記事でも外してよい）"""
    if not overrides_file.exists():
        return set()
    return set(json.loads(overrides_file.read_text(encoding="utf-8")).get("exclude", {}))


def build_rows(titles: dict[str, str], contents: dict[str, tuple[str, str]],
               extracts: dict[str, str]) -> dict[str, dict]:
    """通信の結果を既存の表と同じ形にする。欠落した項目は補作しない。"""
    rows = {}
    for qid, title in titles.items():
        wikitext, revised = contents.get(title, ("", ""))
        keys = keys_of(infobox(wikitext))
        summary = summary_of(extracts.get(title, ""))
        if not summary and not any(keys.values()):
            continue
        rows[qid] = {"title": title, "url": ARTICLE_URL + urllib.parse.quote(title.replace(" ", "_")),
                     "summary": summary, **keys, "revised": revised, "license": LICENSE}
    return rows


def write_report(path: Path, targets: dict[str, dict], titles: dict[str, str] | None = None,
                 rows: dict[str, dict] | None = None) -> None:
    places = []
    for qid, target in targets.items():
        place = dict(target)
        if titles is not None:
            row = (rows or {}).get(qid, {})
            place["title"] = titles.get(qid)
            place["issues"] = []
            if qid not in titles:
                place["issues"].append("日本語記事なし")
            else:
                if not row.get("summary"):
                    place["issues"].append("説明文なし")
                if not any(row.get(key) for key in FIELDS):
                    place["issues"].append("情報欄の項目なし")
            place["summary"] = row.get("summary", "")
            place["fields"] = {key: row[key] for key in FIELDS if row.get(key)}
        places.append(place)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mode": "targets" if titles is None else "fetched", "count": len(places), "places": places}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_existing_articles(connection: sqlite3.Connection, rows: dict[str, dict],
                             removable: set[str] = frozenset()) -> None:
    """取得できなかった既存の記事を、表の作り直しで消さない。removable（exclude に書いた記事）だけは消してよい。"""
    if not has_table(connection, "spot_wiki"):
        return
    missing = [qid for (qid,) in connection.execute("SELECT qid FROM spot_wiki") if qid not in rows and qid not in removable]
    if missing:
        raise ValueError(f"既存の記事 {len(missing)} 件を取得できないため、DB は変更しません: {', '.join(missing)}")
    # 通信結果の一部だけが空でも、既存の説明や情報欄を消さず、確認してから再実行する
    lost = []
    columns = ("summary", *FIELDS)
    for values in connection.execute(f"SELECT qid, {', '.join(columns)} FROM spot_wiki"):
        qid = values[0]
        if qid not in rows:
            continue
        for column, previous in zip(columns, values[1:]):
            if previous and not rows[qid].get(column):
                lost.append(f"{qid}/{column}")
    if lost:
        raise ValueError(f"既存の項目 {len(lost)} 件が空になるため、DB は変更しません: {', '.join(lost)}")


# MARK: - 書き込み

WIKI_COLUMNS = ("title", "url", "summary", *FIELDS, "revised", "license")


def write(connection: sqlite3.Connection, rows: dict[str, dict]) -> None:
    # 表の作り直しを途中で失敗しても、元の情報を残す
    with connection:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS spot_wiki")
        column_definitions = ", ".join(f"{column} TEXT NOT NULL" for column in WIKI_COLUMNS)
        connection.execute(f"CREATE TABLE spot_wiki (qid TEXT PRIMARY KEY, {column_definitions})")
        placeholders = ", ".join(f":{column}" for column in ("qid",) + WIKI_COLUMNS)
        connection.executemany(f"INSERT INTO spot_wiki VALUES ({placeholders})",
                               [{"qid": qid, **row} for qid, row in rows.items()])
        connection.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("wikiCount", str(len(rows))),
            ("dataVersion", data_version(connection)),
            ("updatedAt", datetime.date.today().isoformat()),
        ])
    connection.execute("VACUUM")


def validate_report_path(report: Path, database: Path) -> None:
    """確認用の一覧で、読み取り元やアプリのデータを上書きしない。"""
    protected = [database, DEFAULT_DB, DEFAULT_VERSION_FILE, DEFAULT_SUMMARIES_FILE, FEATURED_PLACES_FILE,
                 POPULAR_PLACES_FILE, POPULAR_OVERRIDES_FILE]
    protected.extend(Path(path) for path in glob.glob(str(PILGRIMAGE_FILES)))
    for source in protected:
        same_path = report.resolve() == source.resolve()
        same_file = report.exists() and source.exists() and report.samefile(source)
        if same_path or same_file:
            raise ValueError(f"確認用の一覧は入力・配布ファイルと別の場所に保存してください: {report}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="通信も DB の変更もせず、取得対象を確認する")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="対象と取得結果の確認用 JSON")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"DB がありません: {args.db}（先に fetch.py と details.py を流す）")
    try:
        validate_report_path(args.report, args.db)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    connection = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode={'ro' if args.dry_run else 'rw'}", uri=True)
    try:
        targets = target_places(connection)
        qids = list(targets)
        print(f"■ 集める先: {len(qids)} か所（文化財・巡礼リスト・代表的な場所・閲覧数の上位）", flush=True)
        if not POPULAR_PLACES_FILE.exists():
            print(f"  △ 閲覧数の一覧がないので、閲覧数の上位は足していません（先に popular.py を流す）: {POPULAR_PLACES_FILE}",
                  flush=True)
        if args.dry_run:
            write_report(args.report, targets)
            print(f"○ 取得対象を書きました（通信・DB 変更なし）: {args.report}")
            return
        titles = article_titles(qids, args.refresh)
        print(f"  日本語版の記事があるのは {len(titles)} か所", flush=True)
        unique_titles = sorted(set(titles.values()))
        contents = article_contents(unique_titles, args.refresh)
        extracts = article_extracts(unique_titles, args.refresh)
        rows = build_rows(titles, contents, extracts)
        write_report(args.report, targets, titles, rows)
        print(f"○ 説明・情報欄の欠落を含む確認用の一覧: {args.report}")
        ensure_existing_articles(connection, rows, excluded_articles())
        write(connection, rows)
        print(f"○ {len(rows)} か所の鍵と冒頭の文を書きました: {args.db}")
        for key in FIELDS:
            print(f"  {key}: {sum(1 for row in rows.values() if row[key])} か所")
        # 試しの DB で本体の版や冒頭の文を書き換えない
        if args.db.resolve() == DEFAULT_DB.resolve():
            write_version_file(connection, DEFAULT_VERSION_FILE)
            print(f"○ {write_summaries(connection, DEFAULT_SUMMARIES_FILE)} か所の1文目を書きました: {DEFAULT_SUMMARIES_FILE}")
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        raise SystemExit(f"Wikipedia のデータを作れませんでした: {error}") from error
    finally:
        connection.close()


if __name__ == "__main__":
    main()
