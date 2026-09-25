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
- 寺社・城の詳しい情報（表 `spot_details`）は、行政のオープンデータを加工して作っています。公開元とライセンスは下の「詳しい情報」を見てください。
  The `spot_details` table is derived from open data published by local governments (see "詳しい情報 / Details" below).
- スクリプト（`scripts/spot/fetch.py`・`scripts/spot/details.py`）は MIT License です（`LICENSE`）。

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

- 公開元は `scripts/spot/sources.json` に書きます（取得先・ライセンス・列の対応）。デジタル庁の「自治体標準オープンデータセット」の観光施設一覧の形を想定しています。
- 行を、DB の寺社・城に名前と距離（300m 以内）で当てます。名前は空白・括弧書き・旧字体をならし、括弧の中の別名と山号を外した名前も試します。
  境内や門前の別の施設（名前の後ろに言葉が付いたもの）は当てません。
- 画像は画像ごとにライセンスが違うため入れていません。

### 公開元 / Sources

| 公開元 | ライセンス | 表記 |
| --- | --- | --- |
| [京都府 観光施設一覧](https://data.bodik.jp/dataset/260002_kankou_shisetsu)（京都府） | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) | 出典：京都府「観光施設一覧」（CC BY 4.0）を加工して作成 |

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
| `updated` | 公開元の情報更新日 |
| `source`・`license` | 公開元の名前・ライセンス |

`meta` には、詳しい情報の件数（`detailsCount`）と公開元（`detailsSources`）も入ります。
