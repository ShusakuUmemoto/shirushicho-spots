"""wikipedia.py の読み取りのテスト（通信しない）。

使い方（リポジトリのルートで）:
    python3 -m unittest scripts/spot/test_wikipedia.py
"""

import json
import sqlite3
import sys
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summaries  # noqa: E402
import wikipedia  # noqa: E402

CASTLE = """{{Otheruses|兵庫県の城|その他|姫路城 (曖昧さ回避)}}
{{日本の城郭
|名称 = 姫路城
|別名 = 白鷺城<ref>姫路市</ref>
|城郭構造 = [[平山城#渦郭式|渦郭式]][[平山城]]<!-- メモ -->
|天守構造 = 連立式望楼型5重6階地下1階<br />（1609年 [[木造]]）
|築城主 = [[赤松貞範]]{{Sfn|姫路市|2010|p=3}}
|築城年 = [[1346年]]（[[正平 (日本)|正平]]元年）
}}
'''姫路城'''（ひめじじょう）は、兵庫県姫路市にある日本の城。
"""

SHRINE = """{{神社
|名称 = 出雲大社
|主祭神 = * [[大国主大神]]（おおくにぬしのおおかみ）
|社格等 = [[式内社]]（名神大）<br>[[出雲国]][[一宮]]<br>[[官幣大社]]<br>[[勅祭社]]<br>[[別表神社]]
|創建 = 不詳（{{和暦|659}}説）
}}"""

TEMPLE = """{{日本の寺院
|名称 = 清水寺
|山号 = 音羽山
|宗派 = [[北法相宗]]
|本尊 = [[十一面観音|十一面千手観世音菩薩]]{{要出典|date=2020年1月}}
|創建年 = [[778年]]（[[宝亀]]9年）
|開基 = [[延鎮]]、{{仮リンク|坂上田村麻呂|en|Sakanoue no Tamuramaro}}（伝）
}}"""

# 実際の記事の城の情報欄（日本の城郭概要表）は英語の項目名
CASTLE_OUTLINE = """{{日本の城郭概要表
|name = 二条城
|struct = 輪郭式[[平城]]
|tower_struct = ※共に焼失し非現存※<br />複合式望楼型5重5階（[[1603年]]移築）
|builders = [[徳川家康]]
|build_y = [[1601年]]
}}"""

RANGE_TEMPLE = """{{日本の寺院
|本尊 = {{Ublist|薬師如来|日光菩薩}}
|創建年 = （伝）[[神亀]]年間（[[724年]]{{snd}}[[729年]]）
|開基 = なし
}}"""


class InfoboxTests(unittest.TestCase):
    def test_曖昧さ回避の型を飛ばして城の情報欄を読む(self):
        keys = wikipedia.keys_of(wikipedia.infobox(CASTLE))
        self.assertEqual(keys["castle_structure"], "渦郭式平山城")
        self.assertEqual(keys["tenshu_structure"], "連立式望楼型5重6階地下1階（1609年 木造）")
        self.assertEqual(keys["founder"], "赤松貞範")
        self.assertEqual(keys["founded"], "1346年（正平元年）")
        self.assertEqual(keys["deity"], "")

    def test_神社の祭神と社格を短くする(self):
        keys = wikipedia.keys_of(wikipedia.infobox(SHRINE))
        # 読みがなだけの括弧は外す
        self.assertEqual(keys["deity"], "大国主大神")
        # 5件あるので4件までにして「など」
        self.assertEqual(keys["rank"], "式内社（名神大）、出雲国一宮、官幣大社、勅祭社 など")
        self.assertEqual(keys["founded"], "不詳（659年説）")

    def test_寺の本尊と開基を読み仮リンクは名前だけにする(self):
        keys = wikipedia.keys_of(wikipedia.infobox(TEMPLE))
        self.assertEqual(keys["honzon"], "十一面千手観世音菩薩")
        self.assertEqual(keys["sect"], "北法相宗")
        self.assertEqual(keys["founder"], "延鎮、坂上田村麻呂（伝）")
        self.assertEqual(keys["founded"], "778年（宝亀9年）")

    def test_城郭概要表の英語の項目を読み注記を外す(self):
        keys = wikipedia.keys_of(wikipedia.infobox(CASTLE_OUTLINE))
        self.assertEqual(keys["castle_structure"], "輪郭式平城")
        self.assertEqual(keys["tenshu_structure"], "複合式望楼型5重5階（1603年移築）")
        self.assertEqual(keys["founder"], "徳川家康")
        self.assertEqual(keys["founded"], "1601年")

    def test_年の範囲の記号と箇条の型を残しなしは出さない(self):
        keys = wikipedia.keys_of(wikipedia.infobox(RANGE_TEMPLE))
        self.assertEqual(keys["founded"], "（伝）神亀年間（724年–729年）")
        self.assertEqual(keys["honzon"], "薬師如来、日光菩薩")
        self.assertEqual(keys["founder"], "")

    def test_情報欄がなければ空(self):
        self.assertEqual(wikipedia.infobox("{{Otheruses|a|b}}\n本文だけ"), {})

    def test_括弧の中の読点では分けない(self):
        self.assertEqual(wikipedia.items_of("田村大神（倭迹迹日百襲姫命、猿田彦大神）、天隠山命"),
                         ["田村大神（倭迹迹日百襲姫命、猿田彦大神）", "天隠山命"])
        self.assertEqual(wikipedia.items_of("連立式層塔型3重3階（1605年築・1850年再<br />外観復元1958年再）"),
                         ["連立式層塔型3重3階（1605年築・1850年再、外観復元1958年再）"])

    def test_長い値は1件目だけにして切らない(self):
        long_item = "あ" * (wikipedia.MAX_VALUE_LENGTH + 5)
        self.assertEqual(wikipedia.shortened([long_item, "い"]), f"{long_item} など")


class SummaryTests(unittest.TestCase):
    def test_最初の段落を文の切れ目まで使う(self):
        extract = "姫路城（ひめじじょう）は、兵庫県姫路市にある日本の城。" + "い" * 150 + "。\n2段落目。"
        self.assertEqual(wikipedia.summary_of(extract), "姫路城は、兵庫県姫路市にある日本の城。")

    def test_読みが2つある括弧も外す(self):
        self.assertEqual(wikipedia.summary_of("出雲大社（いずもおおやしろ / いずもたいしゃ）は、島根県にある神社。"),
                         "出雲大社は、島根県にある神社。")

    def test_最初の文が長すぎれば切って印を付ける(self):
        summary = wikipedia.summary_of("あ" * 300 + "。")
        self.assertEqual(len(summary), wikipedia.SUMMARY_MAX_LENGTH)
        self.assertTrue(summary.endswith("…"))

    def test_句点で終わらない段落もそのまま(self):
        self.assertEqual(wikipedia.summary_of("清水寺は京都の寺"), "清水寺は京都の寺")


class FirstSentenceTests(unittest.TestCase):
    def test_括弧の中の句点では切らない(self):
        text = "知覧城は、鹿児島県にあった日本の城（中世山城。国の史跡）。国の史跡に指定されている。"
        self.assertEqual(summaries.first_sentence(text), "知覧城は、鹿児島県にあった日本の城（中世山城。国の史跡）。")

    def test_句点がなければ全体を返す(self):
        self.assertEqual(summaries.first_sentence("清水寺は京都の寺"), "清水寺は京都の寺")


class TargetPlacesTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)
        self.featured = self.directory / "featured.json"
        self.featured.write_text('{"places": []}', encoding="utf-8")
        self.popular = self.directory / "popular.json"
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE spots (id INTEGER, wikidata TEXT, name TEXT, prefecture TEXT);
            CREATE TABLE spot_details (id INTEGER, cultural_properties TEXT);
        """)

    def add_spot(self, identifier, qid, name, cultural_properties=""):
        self.connection.execute("INSERT INTO spots VALUES (?, ?, ?, ?)",
                                (identifier, qid, name, "東京都"))
        self.connection.execute("INSERT INTO spot_details VALUES (?, ?)",
                                (identifier, cultural_properties))

    def targets(self, total=wikipedia.TARGET_TOTAL):
        return wikipedia.target_places(self.connection, self.featured,
                                       str(self.directory / "pilgrimage-*.json"), self.popular, total)

    def write_featured(self, places):
        self.featured.write_text(json.dumps({"places": places}, ensure_ascii=False), encoding="utf-8")

    def test_国宝と重要文化財を含めその他の文化財とIDなしは除く(self):
        self.add_spot(1, "Q10", "国宝寺", "国宝: 本堂")
        self.add_spot(2, "Q2", "重文寺", "重要文化財: 山門")
        self.add_spot(3, "Q3", "史跡寺", "史跡: 境内")
        self.add_spot(4, "", "未同定寺", "重要文化財: 本堂")
        self.add_spot(5, "Q5", "未指定寺")
        targets = self.targets()
        self.assertEqual(list(targets), ["Q2", "Q10"])
        self.assertEqual(targets["Q2"]["reasons"], ["国宝・重要文化財の建物"])

    def test_代表施設を追加し重なる文化財と巡礼の名前と理由をまとめる(self):
        self.add_spot(1, "Q1", "代表神社", "重要文化財: 本殿")
        self.add_spot(2, "Q2", "代表寺")
        places = [{"qid": qid, "name": name, "prefecture": "東京都", "reason": "地域の代表"}
                  for qid, name in [("Q1", "代表神社"), ("Q2", "代表寺")]]
        self.write_featured(places)
        pilgrimage = {"spots": [{"id": "Q1", "name": "代表神社の別名"},
                                {"id": "Q1", "name": "代表神社の別名"},
                                {"id": "local-place", "name": "独自の場所"}]}
        (self.directory / "pilgrimage-test.json").write_text(json.dumps(pilgrimage), encoding="utf-8")
        targets = self.targets()
        self.assertEqual(set(targets), {"Q1", "Q2"})
        self.assertEqual(targets["Q1"]["names"], ["代表神社", "代表神社の別名"])
        self.assertEqual(targets["Q1"]["reasons"],
                         ["国宝・重要文化財の建物", "巡礼リストの場所", "代表的な場所: 地域の代表"])
        self.assertEqual(targets["Q2"]["reasons"], ["代表的な場所: 地域の代表"])

    def test_代表施設のIDと名前と都道府県がDBと一致しなければ拒否する(self):
        self.add_spot(1, "Q1", "代表神社")
        place = {"qid": "Q1", "name": "代表神社", "prefecture": "東京都", "reason": "地域の代表"}
        for field, wrong_value in [("qid", "Q2"), ("name", "別の神社"), ("prefecture", "京都府")]:
            with self.subTest(field=field):
                self.write_featured([{**place, field: wrong_value}])
                with self.assertRaisesRegex(ValueError, "DB と一致しません"):
                    self.targets()

    def test_代表施設のID重複と空の選定理由を拒否する(self):
        self.add_spot(1, "Q1", "代表神社")
        place = {"qid": "Q1", "name": "代表神社", "prefecture": "東京都", "reason": "地域の代表"}
        self.write_featured([place, place])
        with self.assertRaisesRegex(ValueError, "ID が重なっています"):
            self.targets()
        self.write_featured([{**place, "reason": " "}])
        with self.assertRaisesRegex(ValueError, "選定理由が必要"):
            self.targets()

    def write_popular(self, places):
        ranked = [{"rank": rank, "views": 1000 - rank, **place} for rank, place in enumerate(places, start=1)]
        self.popular.write_text(json.dumps({"window": "2025-10〜2026-09", "places": ranked}, ensure_ascii=False),
                                encoding="utf-8")

    def test_閲覧数の上位で合計まで埋め確かめが要る所と除いた所は飛ばす(self):
        self.add_spot(1, "Q1", "国宝寺", "国宝: 本堂")
        for identifier, qid in enumerate(["Q2", "Q3", "Q4", "Q5"], start=2):
            self.add_spot(identifier, qid, f"人気寺{qid}")
        self.write_popular([{"qid": "Q1", "title": "国宝寺"},  # すでに入っている所は数を増やさない
                            {"qid": "Q2", "title": "山の記事", "needsReview": True},
                            {"qid": "Q3", "title": "人気寺Q3"},
                            {"qid": "Q4", "title": "人気寺Q4", "excluded": "別の施設の記事"},
                            {"qid": "Q5", "title": "人気寺Q5"}])
        targets = self.targets(total=2)
        self.assertEqual(list(targets), ["Q1", "Q3"])
        self.assertEqual(targets["Q1"]["reasons"], ["国宝・重要文化財の建物", "閲覧数の上位（1位・2025-10〜2026-09 に 999 回）"])
        self.assertEqual(self.targets(total=3).keys(), {"Q1", "Q3", "Q5"})

    def test_一度入れた記事は合計を超えても残す(self):
        self.add_spot(1, "Q1", "人気寺")
        self.connection.executescript("""
            CREATE TABLE spot_wiki (qid TEXT, title TEXT);
            INSERT INTO spot_wiki VALUES ('Q1', '人気寺'), ('Q7', '前の人気寺');
        """)
        self.write_popular([{"qid": "Q1", "title": "人気寺"}])
        targets = self.targets(total=1)
        self.assertEqual(list(targets), ["Q1", "Q7"])
        self.assertEqual(targets["Q7"], {"qid": "Q7", "names": ["前の人気寺"], "reasons": ["前から入っている記事"]})

    def test_閲覧数の一覧の場所がDBになければ拒否し一覧がなければ足さない(self):
        self.write_popular([{"qid": "Q9", "title": "どこか"}])
        with self.assertRaisesRegex(ValueError, "DB にありません"):
            self.targets()
        self.popular.unlink()
        self.assertEqual(self.targets(), {})

    def test_文化財側の不正なIDを拒否する(self):
        self.add_spot(1, "Qinvalid", "国宝寺", "国宝: 本堂")
        with self.assertRaisesRegex(ValueError, "ID が不正"):
            self.targets()


class ArticleResultsTests(unittest.TestCase):
    def test_説明だけと情報欄だけの記事は残しどちらもない記事は除く(self):
        rows = wikipedia.build_rows(
            {"Q1": "説明だけ", "Q2": "情報欄だけ", "Q3": "空の記事"},
            {"情報欄だけ": (TEMPLE, "2026-09-28")},
            {"説明だけ": "説明だけの記事は、寺についての記事。"})
        self.assertEqual(set(rows), {"Q1", "Q2"})
        self.assertEqual(rows["Q1"]["summary"], "説明だけの記事は、寺についての記事。")
        self.assertFalse(any(rows["Q1"][key] for key in wikipedia.FIELDS))
        self.assertEqual(rows["Q2"]["summary"], "")
        self.assertEqual(rows["Q2"]["honzon"], "十一面千手観世音菩薩")
        self.assertEqual(rows["Q2"]["revised"], "2026-09-28")
        self.assertEqual(rows["Q2"]["license"], "CC BY-SA 4.0")
        self.assertEqual(set(rows["Q2"]), set(wikipedia.WIKI_COLUMNS))

    def test_取得結果で記事と説明と情報欄の欠落を別々に報告する(self):
        targets = {qid: {"qid": qid, "names": [qid], "reasons": ["代表的な場所"]}
                   for qid in ("Q1", "Q2", "Q3", "Q4")}
        titles = {"Q2": "説明だけ", "Q3": "情報欄だけ", "Q4": "空の記事"}
        rows = wikipedia.build_rows(titles, {"情報欄だけ": (TEMPLE, "2026-09-28")},
                                    {"説明だけ": "寺の説明。"})
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "reports" / "result.json"
            wikipedia.write_report(report, targets, titles, rows)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "fetched")
            self.assertEqual(payload["count"], 4)
            places = {place["qid"]: place for place in payload["places"]}
            self.assertEqual(places["Q1"]["issues"], ["日本語記事なし"])
            self.assertEqual(places["Q2"]["issues"], ["情報欄の項目なし"])
            self.assertEqual(places["Q3"]["issues"], ["説明文なし"])
            self.assertEqual(places["Q3"]["fields"]["honzon"], "十一面千手観世音菩薩")
            self.assertEqual(places["Q4"]["issues"], ["説明文なし", "情報欄の項目なし"])
            wikipedia.write_report(report, targets)
            dry_run = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(dry_run["mode"], "targets")
            self.assertEqual(dry_run["places"], list(targets.values()))

    def test_既存記事が消える結果を拒否し元の表を残す(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        wikipedia.ensure_existing_articles(connection, {})
        fields = ", ".join(f"{field} TEXT DEFAULT ''" for field in wikipedia.FIELDS)
        connection.execute(f"CREATE TABLE spot_wiki (qid TEXT PRIMARY KEY, summary TEXT, {fields})")
        connection.execute("INSERT INTO spot_wiki (qid, summary, deity) VALUES ('Q1', '既存の説明', '既存の祭神')")
        connection.commit()
        with self.assertRaisesRegex(ValueError, "既存の記事 1 件.*Q1"):
            wikipedia.ensure_existing_articles(connection, {"Q2": {"summary": "新しい説明"}})
        for missing_field, row in [("summary", {"deity": "既存の祭神"}),
                                   ("deity", {"summary": "更新した説明"})]:
            with self.subTest(missing_field=missing_field):
                with self.assertRaisesRegex(ValueError, f"既存の項目 1 件.*Q1/{missing_field}"):
                    wikipedia.ensure_existing_articles(connection, {"Q1": row})
        self.assertEqual(connection.execute("SELECT qid, summary, deity FROM spot_wiki").fetchall(),
                         [("Q1", "既存の説明", "既存の祭神")])
        wikipedia.ensure_existing_articles(connection,
                                           {"Q1": {"summary": "更新した説明", "deity": "既存の祭神"}, "Q2": {}})


class FetchTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        patches = [mock.patch.object(wikipedia, "WIKI_CACHE_DIR", Path(temporary_directory.name)),
                   mock.patch.object(wikipedia, "REQUEST_INTERVAL", 0)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def not_found(self, *args, **kwargs):
        raise urllib.error.HTTPError("https://example.org/a", 404, "Not Found", {}, None)

    def test_許したときだけ404をNoneにして残し次は送らない(self):
        with mock.patch.object(wikipedia.urllib.request, "urlopen", side_effect=self.not_found) as urlopen:
            self.assertIsNone(wikipedia.fetch_json("https://example.org/a", refresh=False, not_found_ok=True))
            self.assertIsNone(wikipedia.fetch_json("https://example.org/a", refresh=False, not_found_ok=True))
        self.assertEqual(urlopen.call_count, 1)

    def test_許していなければ404で止まる(self):
        with mock.patch.object(wikipedia, "MAX_RETRIES", 1), \
                mock.patch.object(wikipedia.urllib.request, "urlopen", side_effect=self.not_found):
            with self.assertRaises(SystemExit):
                wikipedia.fetch_json("https://example.org/b", refresh=False)


class ReportPathTests(unittest.TestCase):
    def test_DBと同じ場所や同じファイルへのリンクを拒否する(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "spots.sqlite"
            database.write_bytes(b"existing database")
            alias = Path(directory) / "report.json"
            alias.symlink_to(database)
            hard_link = Path(directory) / "hard-link.json"
            hard_link.hardlink_to(database)
            for report in (database, alias, hard_link, wikipedia.DEFAULT_VERSION_FILE,
                           wikipedia.DEFAULT_SUMMARIES_FILE, wikipedia.FEATURED_PLACES_FILE):
                with self.subTest(report=report):
                    with self.assertRaisesRegex(ValueError, "別の場所"):
                        wikipedia.validate_report_path(report, database)
            result = subprocess.run(
                [sys.executable, str(Path(wikipedia.__file__)), "--dry-run", "--db", str(database),
                 "--report", str(database)], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("別の場所", result.stderr)
            wikipedia.validate_report_path(Path(directory) / "new-report.json", database)
            self.assertEqual(database.read_bytes(), b"existing database")


class WriteTransactionTests(unittest.TestCase):
    def test_書き込み途中の失敗でもファイルDBの元の表を残す(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spots.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE spot_wiki (qid TEXT, summary TEXT)")
                connection.execute("INSERT INTO spot_wiki VALUES ('Q1', '既存の説明')")
                connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO meta VALUES ('wikiCount', '1')")
                connection.commit()
                # 新しい表を作ったあと、項目の不足で INSERT が失敗する
                with self.assertRaises(sqlite3.ProgrammingError):
                    wikipedia.write(connection, {"Q2": {"title": "不完全な記事"}})
            finally:
                connection.close()
            reopened = sqlite3.connect(path)
            try:
                self.assertEqual(reopened.execute("SELECT * FROM spot_wiki").fetchall(),
                                 [("Q1", "既存の説明")])
                self.assertEqual(reopened.execute("SELECT * FROM meta").fetchall(), [("wikiCount", "1")])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
