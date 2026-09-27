"""pilgrimage_places.py の照らし合わせのテスト（通信しない）。

使い方（リポジトリのルートで）:
    python3 -m unittest scripts/spot/test_pilgrimage_places.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pilgrimage_places  # noqa: E402


def place(qid, name, category, lat, lon, aliases=()):
    return {"qid": qid, "name": name, "aliases": list(aliases), "category": category,
            "lat": lat, "lon": lon, "prefecture": "長野県"}


def spot(spot_id, name, category, lat, lon, wikidata=""):
    return {"id": spot_id, "name": name, "kana": "", "category": category,
            "lat": lat, "lon": lon, "prefecture": "長野県", "wikidata": wikidata}


class MergeTests(unittest.TestCase):
    def test_skips_place_already_in_db_by_qid(self):
        spots = [spot("n1", "松本城", "castle", 36.2386, 137.9689, "Q1")]
        _, tagged, added, conflicted, _ = pilgrimage_places.merge(spots, [place("Q1", "松本城", "castle", 36.2386, 137.9689)])
        self.assertEqual((tagged, added, conflicted), ([], [], []))

    def test_tags_nearby_ruin_with_suffix(self):
        spots = [spot("n2", "上田城跡公園", "castle", 36.4035, 138.2445)]
        _, tagged, added, _, _ = pilgrimage_places.merge(spots, [place("Q2", "上田城", "castle", 36.4030, 138.2450)])
        self.assertEqual(len(tagged), 1)
        self.assertEqual(spots[0]["wikidata"], "Q2")
        self.assertEqual(added, [])

    def test_tags_name_with_number_and_mountain(self):
        spots = [spot("n3", "西国33番 谷汲山 華厳寺", "temple", 35.5301, 136.6102),
                 spot("n4", "青葉山 松尾寺（matsuno o dera)", "temple", 35.5000, 135.5000)]
        places = [place("Q3", "華厳寺", "temple", 35.5300, 136.6100),
                  place("Q4", "松尾寺", "temple", 35.5001, 135.5001)]
        _, tagged, added, _, _ = pilgrimage_places.merge(spots, places)
        self.assertEqual([s["id"] for _, s in tagged], ["n3", "n4"])
        self.assertEqual(added, [])

    def test_prefixed_name_only_when_very_close(self):
        near = [spot("n5", "西国25番 御嶽山 播州清水寺", "temple", 34.9990, 135.1000)]
        _, tagged, _, _, _ = pilgrimage_places.merge(near, [place("Q5", "清水寺", "temple", 34.9993, 135.1000)])
        self.assertEqual(len(tagged), 1)
        far = [spot("n6", "播州清水寺", "temple", 35.0030, 135.1000)]  # 約400m
        _, tagged, added, _, _ = pilgrimage_places.merge(far, [place("Q6", "清水寺", "temple", 35.0000, 135.1000)])
        self.assertEqual((tagged, len(added)), ([], 1))

    def test_does_not_tag_other_category_or_other_facility(self):
        spots = [spot("n7", "武田神社", "shrine", 35.6860, 138.5770, "Q9"),
                 spot("n8", "掛川城二の丸御殿", "castle", 34.7740, 138.0140)]
        places = [place("Q7", "武田氏館（武田神社）", "castle", 35.6861, 138.5771),
                  place("Q8", "掛川城", "castle", 34.7741, 138.0141)]
        _, tagged, added, _, _ = pilgrimage_places.merge(spots, places)
        self.assertEqual(tagged, [])
        self.assertEqual([s["id"] for s in added], ["q7", "q8"])

    def test_leaves_same_name_with_other_qid(self):
        spots = [spot("n9", "浅間神社", "shrine", 35.6300, 138.6700, "Q100")]
        _, tagged, added, conflicted, _ = pilgrimage_places.merge(spots, [place("Q10", "浅間神社", "shrine", 35.6300, 138.6700)])
        self.assertEqual((tagged, added, len(conflicted)), ([], [], 1))
        self.assertEqual(spots[0]["wikidata"], "Q100")

    def test_corrects_known_wrong_qid_before_matching(self):
        # 甲斐国一宮の浅間神社に、市川三郷町の一宮浅間神社の QID が付いていた
        spots = [spot("n2461655369", "浅間神社", "shrine", 35.647769, 138.697456, "Q11352589"),
                 spot("n6484004551", "一宮浅間神社", "shrine", 35.554223, 138.490081)]
        places = [place("Q11557476", "浅間神社", "shrine", 35.64775833, 138.69744167)]
        _, tagged, added, conflicted, corrected = pilgrimage_places.merge(spots, places)
        self.assertEqual([s["wikidata"] for s in spots], ["Q11557476", "Q11352589"])
        self.assertEqual((tagged, added, conflicted, len(corrected)), ([], [], [], 2))

    def test_does_not_correct_when_osm_value_changed(self):
        spots = [spot("n2461655369", "浅間神社", "shrine", 35.647769, 138.697456, "Q999")]
        _, _, _, _, corrected = pilgrimage_places.merge(spots, [])
        self.assertEqual((corrected, spots[0]["wikidata"]), ([], "Q999"))

    def test_new_spot_uses_qid_and_reading(self):
        places = [place("Q969909", "甲府城", "castle", 35.6667, 138.5717, aliases=["こうふじょう", "舞鶴城"])]
        _, _, added, _, _ = pilgrimage_places.merge([], places)
        self.assertEqual(added[0]["id"], "q969909")
        self.assertEqual(added[0]["kana"], "こうふじょう")
        self.assertEqual(added[0]["wikidata"], "Q969909")

    def test_merge_twice_adds_nothing(self):
        spots = []
        places = [place("Q11", "高遠城", "castle", 35.8330, 138.0620)]
        pilgrimage_places.merge(spots, places)
        _, tagged, added, _, _ = pilgrimage_places.merge(spots, places)
        self.assertEqual((tagged, added), ([], []))


class LoadTests(unittest.TestCase):
    def test_bundled_lists_have_known_categories(self):
        places = pilgrimage_places.load_places()
        self.assertGreater(len(places), 0)
        self.assertTrue({p["category"] for p in places} <= {"shrine", "temple", "castle"})


if __name__ == "__main__":
    unittest.main()
