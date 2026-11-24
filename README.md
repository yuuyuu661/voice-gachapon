# OASIS Voice Gachapon

鯖内通貨から完全に独立した、シリアルコード式のボイス動画ガチャです。

## 機能

- おんなのこ / おとこのこ、2種類のガチャ
- R / SSR のサーバー側抽選（既定 SSR 5%）
- 管理画面から使い切りシリアルコードを一括発行
- 初回コードで個人アカウントを自動作成し、設定回数を抽選残数へ加算
- 使用済みコードを再入力して同じコレクションを復元
- 管理画面から動画景品を登録・排出停止
- 獲得動画の個人コレクション、再生、保存
- 同一リクエストの再送に耐える抽選処理
- コードはハッシュのみDB保存（発行時だけ平文を表示）

このサービスは専用セッション・専用DBだけで完結します。既存カジノのログイン、残高、収支テーブルは一切読み書きしません。最初に使用したシリアルコードはコレクション復元キーにもなるため、ユーザー側で保管してください。

## Railway

このリポジトリ全体が単体サービスです。Railwayの **Root Directory** は空欄（リポジトリ直下）のまま、専用PostgreSQLへ接続してください。

必須環境変数:

| 変数 | 内容 |
|---|---|
| `DATABASE_URL` | 専用PostgreSQL接続URL |
| `VOICE_GACHA_ADMIN_PASSWORD` | `/admin/` の管理パスワード |
| `VOICE_GACHA_MEDIA_DIR` | 動画保存先。例: `/data/voice-gachapon` |

任意環境変数:

| 変数 | 既定値 | 内容 |
|---|---:|---|
| `VOICE_GACHA_SSR_RATE_BPS` | `500` | SSR確率（basis points。500 = 5%） |
| `VOICE_GACHA_MAX_VIDEO_BYTES` | `524288000` | 動画1本の最大サイズ |

`VOICE_GACHA_MEDIA_DIR` にはRailway Volumeをマウントしてください。Volumeなしでは再デプロイ時にアップロード動画が消えます。

## 初期設定

1. デプロイ後に `/admin/` を開く。
2. おんなのこ・おとこのこ両方へ、RとSSRを最低1本ずつ登録する。
3. シリアルコードを発行する。
4. 公開URLをユーザーへ案内する。ユーザーは最初のシリアルコードで利用開始する。

抽選対象が片方のレア度しかない場合、そのレア度だけから安全に抽選します。景品が0本のガチャは回せません。

## ローカル起動

```powershell
python -m pip install -r requirements.txt
$env:DATABASE_URL='postgresql://...'
$env:VOICE_GACHA_ADMIN_PASSWORD='...'
uvicorn server:app --reload
```
