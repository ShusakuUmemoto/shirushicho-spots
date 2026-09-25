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
- スクリプト（`scripts/spot/fetch.py`）は MIT License です（`LICENSE`）。

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
