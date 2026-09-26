# shirushicho-spots

iOS アプリ「印帖（しるしちょう）」が地図に出す、全国の神社・寺院・城のデータベース（SQLite）の作り方です。
データは [OpenStreetMap](https://www.openstreetmap.org/) から取っています。

This repository describes how the shrine / temple / castle database bundled with the iOS app
"Shirushicho" is derived from OpenStreetMap data.

## ライセンス / License

- アプリに入っているデータベースは OpenStreetMap の派生データベースで、
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/) のもとにあります。
  © OpenStreetMap contributors
- The database is a Derivative Database of OpenStreetMap and is licensed under ODbL 1.0.
  This repository fulfils ODbL section 4.6 by publishing the method used to create it.
- 寺社・城の詳しい情報（表 `spot_details`）は、行政のオープンデータと文化庁の国指定文化財等データベースを加工して作っています。公開元とライセンスは下の「詳しい情報」を見てください。
  The `spot_details` table is derived from open data published by local governments (see "詳しい情報 / Details" below).
- ご祭神・ご本尊・城郭構造などと冒頭の文（表 `spot_wiki`）は、日本語版 Wikipedia の各記事を加工したもので、
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.ja) のもとにあります（行ごとに記事の名前・URL・版の日付を持ちます）。下の「Wikipedia の情報」を見てください。
  The `spot_wiki` table is adapted from Japanese Wikipedia articles and is licensed under CC BY-SA 4.0.
- 札所の一覧（`GoshuinApp/Resources/Pilgrimages/*.json`）は、番号・名前が Wikipedia（CC BY-SA 4.0）の一覧表、QID・座標の一部が Wikidata（CC0）から来ています。
- スクリプト（`scripts/spot/` の `fetch.py`・`details.py`・`wikipedia.py`・`summaries.py`）は MIT License です（`LICENSE`）。

## 作り方 / How to build

Python 3（標準ライブラリだけ）で動きます。

```
python3 scripts/spot/fetch.py
```

`GoshuinApp/Resources/Spots/spots.sqlite` に書き出します。アプリに入っているものと同じ手順で作れます。
OpenStreetMap のデータは日々更新されるため、作った日によって中身は少し変わります（DB の `meta` の `builtAt` が作った日です）。

### 取り方

- Overpass API に、都道府県（ISO 3166-2 の JP-01〜JP-47）ごとに問い合わせます。
- 対象は、名前のある次のものです。
  - 神社: `amenity=place_of_worship` + `religion=shinto`
  - 寺院: `amenity=place_of_worship` + `religion=buddhist`
  - 城: `historic=castle`
  - 境内だけが宗教用地として登録された寺社: `landuse=religious` + `religion=shinto|buddhist`
- way・relation は代表点（center）を使います。
- 「本殿」「拝殿」のような境内の建物の名前だけのものは除きます。
- 同じ名前で 50m 以内のものは1件にまとめます。境内と同じ名前の建物は、500m 以内なら1件にまとめます。

### 表 / Schema

`spots`

| 列 | 中身 |
| --- | --- |
| `id` | OpenStreetMap の要素（`n123`・`w456`・`r789`） |
| `name` | 名前（`name:ja`、なければ `name`） |
| `kana` | 読み（`name:ja-Hira`・`name:ja_kana`・`name:ja-Kana` をひらがなにしたもの。ないときは空） |
| `category` | `shrine`・`temple`・`castle` |
| `lat`・`lon` | 緯度・経度 |
| `prefecture` | 都道府県 |
| `wikidata` | Wikidata の ID（ないときは空） |

`meta` には、作った日（`builtAt`）・件数（`count`）・ライセンス（`license`）が入ります。

## 詳しい情報 / Details

寺社・城の説明・拝観の時間・料金などは、行政のオープンデータから `scripts/spot/details.py` で同じ DB に書き足します（`fetch.py` のあとに流します）。

```
python3 scripts/spot/details.py
```

- 公開元は `scripts/spot/sources.json` に書きます（取得先・ライセンス・列の対応・画面に出す出典の名前 `title`）。デジタル庁の「自治体標準オープンデータセット」の観光施設一覧の形を想定しています。
- 行を、DB の寺社・城に名前と距離（300m 以内）で当てます。名前は空白・括弧書き・旧字体をならし、括弧の中の別名と山号を外した名前も試します。
  境内や門前の別の施設（名前の後ろに言葉が付いたもの）は当てません。
- 同じ寺社・城に2つの公開元が当たったら、先の公開元の空の項目を後の公開元で埋め、出典にはどちらも書きます。更新日には公開元の名前を添えます（京都府「観光施設一覧」2019-02-13）。
- 画像は画像ごとにライセンスが違うため入れていません。

### 公開元 / Sources

| 公開元 | ライセンス | 表記 |
| --- | --- | --- |
| [京都府 観光施設一覧](https://data.bodik.jp/dataset/260002_kankou_shisetsu)（京都府） | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) | 出典：京都府「観光施設一覧」（CC BY 4.0）を加工して作成 |
| [大阪府 観光施設一覧](https://data.bodik.jp/dataset/270008_tourism)（大阪府） | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) | 出典：大阪府「観光施設一覧」（CC BY 4.0）を加工して作成 |
| [長崎県 ながさき旅ネット 観光スポット情報](https://data.bodik.jp/dataset/420000_nagasakitabinet)（長崎県） | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) | 出典：長崎県「ながさき旅ネット 観光スポット情報」（CC BY 4.0）を加工して作成 |
| [三重県 文化資産等情報](https://www.bunka.pref.mie.lg.jp/87741000001.htm)（三重県。種別「寺社等」「城跡等」） | [CC BY 2.1 JP](https://creativecommons.org/licenses/by/2.1/jp/) | 出典：三重県「文化資産等情報」（CC BY 2.1 JP）を加工して作成 |
| [熊本県 観光施設一覧](https://data.bodik.jp/dataset/430005_04)（熊本県） | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) | 出典：熊本県「観光施設一覧」（CC BY 4.0）を加工して作成 |

### 表 / Schema

`spot_details`（値のない項目は空）

| 列 | 中身 |
| --- | --- |
| `id` | `spots` の `id` |
| `description` | 説明 |
| `days`・`hours` | 拝観できる日・拝観の時間（特記事項を含む） |
| `fee` | 料金 |
| `address`・`access`・`parking` | 住所・アクセス・駐車場 |
| `phone`・`url` | 電話・公式サイト |
| `cultural_properties` | 国宝・重要文化財の建物（種別ごとに改行） |
| `updated` | 公開元の情報更新日（2つの公開元をつないだときは公開元の名前つき） |
| `source`・`license` | 公開元の名前・ライセンス |

`meta` には、詳しい情報の件数（`detailsCount`）・公開元（`detailsSources`）・データの版（`dataVersion`）・書き足した日（`updatedAt`）も入ります。
データの版は `spots` と `spot_details` の全行から作る値（SHA-256 の先頭 16 桁）で、中身が同じなら流し直しても変わりません。
アプリに入れる DB を作ったときは、同じ版を `GoshuinApp/Resources/spots-version.txt` にも書きます（アプリは2つを比べて、古いデータを自動で落とし直します）。

## Wikipedia の情報 / Wikipedia

ご祭神・ご本尊・宗派・社格・城郭構造などと冒頭の文は、日本語版 Wikipedia の記事から `scripts/spot/wikipedia.py` で同じ DB に書き足します
（`details.py` のあとに流します。`fetch.py` → `details.py` → `wikipedia.py` の順）。

```
python3 scripts/spot/wikipedia.py
python3 -m unittest scripts/spot/test_wikipedia.py   # 読み取りのテスト（通信しない）
```

- 対象は、国宝の建物がある寺社・城（`spot_details` の `cultural_properties`）と、札所の一覧（`GoshuinApp/Resources/Pilgrimages/*.json`）の札所だけです。
- Wikidata の QID から日本語版の記事の名前を引き（`wbgetentities` の sitelinks）、記事の本文（wikitext）の情報欄と冒頭の文（TextExtracts）を読みます。
- 値は書式（脚注・リンク・読みがなだけの括弧）を外して短くするだけにし、言い換えません。多いときは4件までにして「など」を付けます。
- 冒頭の文は、最初の段落の文を 160 字まで（文の途中で切らない）そのまま使います。
- 窓口には User-Agent を付け、送るたびに1秒あけます。答えは一時フォルダに残し、やり直しても送りません。
- 最後に `scripts/spot/summaries.py` で、各場所の1文目だけを `GoshuinApp/Resources/spot-summaries.json` に書き出します（アプリ本体に入れるもの。DB を落とさない人の画面に出します）。

### 表 / Schema

`spot_wiki`（QID ごとに1行。値のない項目は空）

| 列 | 中身 |
| --- | --- |
| `qid` | Wikidata の ID（`spots` の `wikidata`、札所の一覧の QID） |
| `title`・`url` | 記事の名前・URL |
| `summary` | 冒頭の文 |
| `deity`・`honzon`・`sect`・`rank` | 主祭神・本尊・宗派・社格 |
| `founded`・`founder` | 創建（築城）の年・開基（築城主） |
| `castle_structure`・`tenshu_structure` | 城郭構造・天守構造 |
| `revised` | 読んだ記事の版の日付 |
| `license` | `CC BY-SA 4.0` |

`meta` には件数（`wikiCount`）も入り、データの版（`dataVersion`）は `spot_wiki` も含めて作り直します。
