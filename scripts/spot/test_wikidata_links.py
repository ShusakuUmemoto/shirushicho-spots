"""wikidata_links.py のテスト（通信しない）。

使い方（リポジトリのルートで）:
    python3 -m unittest scripts/spot/test_wikidata_links.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikidata_links  # noqa: E402

# 日吉大社のあたり（1度の緯度は約 111km なので、0.001 度は約 111m）
LAT, LON = 35.0703, 135.8764


def spot(identifier, name, lat=LAT, lon=LON, wikidata=""):
    return {"id": identifier, "name": name, "lat": lat, "lon": lon, "wikidata": wikidata}


def page(title, qid="Q1", lat=LAT, lon=LON, disambiguation=False):
    return {"title": title, "qid": qid, "lat": lat, "lon": lon, "disambiguation": disambiguation}


class ResolvePagesTests(unittest.TestCase):
    def test_表記の直しと転送をたどり座標とQIDと曖昧さ回避を読む(self):
        data = {"query": {
            "normalized": [{"from": "山寺 ", "to": "山寺"}],
            "redirects": [{"from": "山寺", "to": "立石寺"}],
            "pages": [
                {"title": "立石寺", "coordinates": [{"lat": 38.31, "lon": 140.43}], "pageprops": {"wikibase_item": "Q2"}},
                {"title": "八幡神社", "pageprops": {"disambiguation": "", "wikibase_item": "Q3"}},
                {"title": "無い寺", "missing": True},
            ]}}
        pages = wikidata_links.resolve_pages(data)
        self.assertEqual(pages["山寺 "]["title"], "立石寺")
        self.assertEqual(pages["山寺"], {"title": "立石寺", "qid": "Q2", "disambiguation": False, "lat": 38.31, "lon": 140.43})
        self.assertTrue(pages["八幡神社"]["disambiguation"])
        self.assertIsNone(pages["八幡神社"]["lat"])
        self.assertNotIn("無い寺", pages)

    def test_記事の名前に使えない名前は引かない(self):
        self.assertTrue(wikidata_links.is_valid_title("日吉大社"))
        for name in ("A|B", "社[1]", " ", "寺" * 100):
            self.assertFalse(wikidata_links.is_valid_title(name), name)


class FindLinksTests(unittest.TestCase):
    def test_近くの同じ名前のQIDのない場所に付ける(self):
        links = wikidata_links.find_links([spot("w1", "日吉大社", lat=LAT + 0.002)], {"日吉大社": page("日吉大社")}, set())
        self.assertEqual(links, {"w1": {"qid": "Q1", "title": "日吉大社", "name": "日吉大社", "distance": 222}})

    def test_座標なし曖昧さ回避遠い使われているQIDは付けない(self):
        spots = [spot("w1", "日吉大社"), spot("n9", "別の神社", wikidata="Q9")]
        for checked in (page("日吉大社", lat=None, lon=None), page("日吉大社", disambiguation=True),
                        page("日吉大社", lat=LAT + 0.02), page("日吉大社", qid="Q9"), page("日吉大社", qid=None)):
            with self.subTest(page=checked):
                self.assertEqual(wikidata_links.find_links(spots, {"日吉大社": checked}, set()), {})

    def test_転送先の名前が重ならなければ付けない(self):
        links = wikidata_links.find_links([spot("w1", "天橋立神社")], {"天橋立神社": page("天橋立")}, set())
        self.assertEqual(links, {})
        links = wikidata_links.find_links([spot("w1", "山寺")], {"山寺": page("立石寺")}, set())
        self.assertEqual(links, {})

    def test_同じ名前の場所が2つあれば近い方だけQIDのある場所と外した場所は飛ばす(self):
        spots = [spot("w1", "貴船神社", lat=LAT + 0.005), spot("w2", "貴船神社", lat=LAT + 0.001),
                 spot("w3", "貴船神社", wikidata="Q8"), spot("w4", "貴船神社")]
        pages = {"貴船神社": page("貴船神社")}
        self.assertEqual(list(wikidata_links.find_links(spots, pages, set())), ["w4"])
        self.assertEqual(list(wikidata_links.find_links(spots, pages, {"w4"})), ["w2"])


class ApplyLinksTests(unittest.TestCase):
    def test_QIDのない場所にだけ付け使われているQIDと外した場所は付けない(self):
        spots = [spot("w1", "日吉大社"), spot("w2", "立石寺", wikidata="Q7"), spot("w3", "貴船神社"),
                 spot("w4", "戸隠神社奥社"), spot("w5", "別の神社", wikidata="Q5")]
        links = {key: {"qid": qid} for key, qid in [("w1", "Q1"), ("w2", "Q2"), ("w3", "Q5"), ("w4", "Q4")]}
        applied = wikidata_links.apply_links(spots, links, {"w4": "別の記事"})
        self.assertEqual([item["id"] for item in applied], ["w1"])
        self.assertEqual([item["wikidata"] for item in spots], ["Q1", "Q7", "", "", "Q5"])


class MergeLinksTests(unittest.TestCase):
    def test_当てたあとの前の候補を残し新しい候補を足す(self):
        spots = [spot("w1", "日吉大社", wikidata="Q1"), spot("w2", "立石寺"), spot("w3", "貴船神社", wikidata="Q8"),
                 spot("w4", "外した神社"), spot("w5", "新しい寺")]
        previous = {"w1": {"qid": "Q1"}, "w2": {"qid": "Q2"}, "w3": {"qid": "Q3"}, "w4": {"qid": "Q4"},
                    "w9": {"qid": "Q9"}}
        merged = wikidata_links.merge_links(spots, previous, {"w5": {"qid": "Q5"}}, {"w4": "別の記事"})
        # w1 は当てたあと、w2 はまだ当てていない。w3 は OpenStreetMap で別の QID が付いた、w4 は外した、w9 は DB にない
        self.assertEqual(merged, {"w1": {"qid": "Q1"}, "w2": {"qid": "Q2"}, "w5": {"qid": "Q5"}})


class FilesTests(unittest.TestCase):
    def test_書いた候補と外した場所を読み直せ理由のない除外は拒否する(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.json"
            self.assertEqual(wikidata_links.load(path), ({}, {}))
            links = {"w1": {"qid": "Q1", "title": "日吉大社", "name": "日吉大社", "distance": 10}}
            wikidata_links.write_links(path, links, {"w2": "別の記事"})
            self.assertEqual(wikidata_links.load(path), (links, {"w2": "別の記事"}))
            path.write_text(json.dumps({"links": {}, "exclude": {"w2": ""}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "理由がありません"):
                wikidata_links.load(path)


if __name__ == "__main__":
    unittest.main()
