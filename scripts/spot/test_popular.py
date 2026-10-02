"""popular.py のテスト（通信しない）。

使い方（リポジトリのルートで）:
    python3 -m unittest scripts/spot/test_popular.py
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import popular  # noqa: E402


class WindowTests(unittest.TestCase):
    def test_終わりの月から12か月をさかのぼる(self):
        self.assertEqual(popular.window("2026-09"), ("20251001", "20260930", "2025-10〜2026-09"))
        self.assertEqual(popular.window("2026-12"), ("20260101", "20261231", "2026-01〜2026-12"))
        self.assertEqual(popular.window("2028-02"), ("20270301", "20280229", "2027-03〜2028-02"))


class ViewsTests(unittest.TestCase):
    def test_月ごとの閲覧数を合計し記録がなければ0(self):
        data = {"items": [{"views": 120}, {"views": 30}, {"views": 0}]}
        self.assertEqual(popular.total_views(data), 150)
        self.assertEqual(popular.total_views({"items": []}), 0)
        self.assertEqual(popular.total_views(None), 0)

    def test_記事の名前の空白と記号をURLに入れられる形にする(self):
        url = popular.pageviews_url("長谷寺 (鎌倉市)/a", "20251001", "20260930")
        self.assertTrue(url.endswith("/%E9%95%B7%E8%B0%B7%E5%AF%BA_%28%E9%8E%8C%E5%80%89%E5%B8%82%29%2Fa/monthly/20251001/20260930"))


class NameOverlapTests(unittest.TestCase):
    def test_同じ名前と曖昧さ回避と旧字体と山号は重なる(self):
        self.assertTrue(popular.names_overlap("清水寺", ["清水寺"]))
        self.assertTrue(popular.names_overlap("長谷寺 (鎌倉市)", ["長谷寺"]))
        self.assertTrue(popular.names_overlap("彌彦神社", ["弥彦神社"]))
        self.assertTrue(popular.names_overlap("音羽山清水寺", ["清水寺"]))

    def test_寺社や城の名前の形で含むものは重なる(self):
        self.assertTrue(popular.names_overlap("大阪城", ["大阪城天守閣"]))
        self.assertTrue(popular.names_overlap("今帰仁城", ["今帰仁城跡"]))

    def test_山や町の記事と呼び名の違う記事は重ならない(self):
        self.assertFalse(popular.names_overlap("富士山", ["富士山本宮浅間大社"]))
        self.assertFalse(popular.names_overlap("鎌倉市", ["長谷寺"]))
        self.assertFalse(popular.names_overlap("平間寺", ["川崎大師"]))


class RankedPlacesTests(unittest.TestCase):
    def setUp(self):
        self.groups = {qid: {"names": [name], "prefectures": ["東京都"], "categories": ["temple"]}
                       for qid, name in [("Q1", "浅草寺"), ("Q2", "川崎大師"), ("Q3", "浅間神社"), ("Q4", "人気寺")]}
        self.titles = {"Q1": "浅草寺", "Q2": "平間寺", "Q3": "富士山", "Q4": "人気寺"}
        self.views = {"浅草寺": 500, "平間寺": 300, "富士山": 900, "人気寺": 500}

    def test_閲覧数の順に並べ名前が重ならなければ確かめの印を付ける(self):
        places = popular.ranked_places(self.groups, self.titles, self.views, {})
        self.assertEqual([place["qid"] for place in places], ["Q3", "Q1", "Q4", "Q2"])
        self.assertEqual([place["rank"] for place in places], [1, 2, 3, 4])
        self.assertTrue(places[0]["needsReview"])
        self.assertNotIn("needsReview", places[1])
        self.assertEqual(places[1]["names"], ["浅草寺"])

    def test_2つ以上の都道府県の場所に付いたQIDは確かめの印を付ける(self):
        self.groups["Q1"]["prefectures"] = ["東京都", "京都府"]
        places = {place["qid"]: place for place in popular.ranked_places(self.groups, self.titles, self.views, {})}
        self.assertTrue(places["Q1"]["needsReview"])

    def test_確かめた結果で受け入れと除外を当てる(self):
        overrides = {"accept": {"Q2": "川崎大師の正式名称"}, "exclude": {"Q3": "山の記事"}}
        places = {place["qid"]: place for place in popular.ranked_places(self.groups, self.titles, self.views, overrides)}
        self.assertNotIn("needsReview", places["Q2"])
        self.assertEqual(places["Q3"]["excluded"], "山の記事")
        self.assertNotIn("needsReview", places["Q3"])

    def test_記事のないQIDの確かめ結果は拒否する(self):
        with self.assertRaisesRegex(ValueError, "記事のある場所がありません"):
            popular.ranked_places(self.groups, self.titles, self.views, {"accept": {"Q99": "理由"}})


class FilesTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)

    def test_一覧を1か所1行で書き読み直せる(self):
        path = self.directory / "popular.json"
        places = [{"rank": 1, "qid": "Q1", "title": "浅草寺", "views": 10}, {"rank": 2, "qid": "Q2", "title": "平間寺", "views": 5}]
        popular.write_places(path, "2025-10〜2026-09", places)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"window": "2025-10〜2026-09", "count": 2, "places": places})
        self.assertEqual(len([line for line in path.read_text(encoding="utf-8").splitlines() if '"qid"' in line]), 2)

    def test_確かめ結果の理由の抜けと重なりを拒否しファイルがなければ空(self):
        path = self.directory / "overrides.json"
        self.assertEqual(popular.read_overrides(path), {})
        path.write_text(json.dumps({"accept": {"Q1": " "}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "理由がありません"):
            popular.read_overrides(path)
        path.write_text(json.dumps({"accept": {"Q1": "a"}, "exclude": {"Q1": "b"}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "両方にある"):
            popular.read_overrides(path)

    def test_同じQIDの場所の名前と都道府県と分類をまとめる(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE spots (id TEXT, name TEXT, category TEXT, prefecture TEXT, wikidata TEXT);
            INSERT INTO spots VALUES ('a', '名古屋城', 'castle', '愛知県', 'Q1'), ('b', '名古屋城', 'castle', '愛知県', 'Q1'),
                                     ('c', '天守', 'castle', '愛知県', 'Q1'), ('d', '無名社', 'shrine', '愛知県', '');
        """)
        self.assertEqual(popular.spot_groups(connection),
                         {"Q1": {"names": ["名古屋城", "天守"], "prefectures": ["愛知県"], "categories": ["castle"]}})


if __name__ == "__main__":
    unittest.main()
