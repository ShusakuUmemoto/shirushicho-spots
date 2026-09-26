"""wikipedia.py の読み取りのテスト（通信しない）。

使い方（リポジトリのルートで）:
    python3 -m unittest scripts/spot/test_wikipedia.py
"""

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
