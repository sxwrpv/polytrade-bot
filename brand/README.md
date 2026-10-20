# PolyTrade brand assets

`polytrade-logo-source.jpg` (800×800) is the source of every mark below. All raster
icons are crops centred on the glyph (it sits at 400, 399 with a radius of about 255px),
so the logo is the same everywhere it appears.

| File | Size | Used by |
| --- | --- | --- |
| `frontend/public/brand/polytrade-mark.png` | 256×256 | Mini App header, landing page, launch screens, bot card |
| `frontend/public/favicon.ico` | 16, 32, 48 | browsers asking for `/favicon.ico` (landing, Mini App, docs, API docs) |
| `frontend/public/brand/favicon-32.png` | 32×32 | `<link rel="icon">` on the landing page and the Swagger/ReDoc shells |
| `frontend/public/brand/apple-touch-icon.png` | 180×180 | iOS home screen |
| `frontend/public/brand/icon-192.png`, `icon-512.png`, `icon-maskable-512.png` | 192, 512 | `site.webmanifest` (Android home screen; the maskable one keeps the glyph inside the safe circle) |
| `frontend/public/brand/og-image.png` | 1200×630 | social preview (`og:image`, `twitter:image`) for the landing page, the docs and the screener |
| `docs/site/favicon-32.png`, `apple-touch-icon.png` | 32, 180 | the documentation site, served from `/docs/assets/` |
| `trader-screener/public/brand/*` | 256, 32, 180 | the screener header and icons; it ships as its own image, so it carries its own copies |
| `telegram/bot-avatar.png` | 640×640 | the bot's profile photo (upload below) |
| `telegram/bot-description.png` | 640×360 | the picture above "What can this bot do?" in an empty chat (upload below) |

The small favicons have their contrast lifted slightly so the glass glyph still reads at
16px. The social card uses the site's own type (Instrument Serif and Inter) on the paper
background.

## Uploading the bot visuals (manual, in Telegram)

The bot's profile lives in BotFather, outside this repo.

1. Open [@BotFather](https://t.me/BotFather), send `/mybots`, choose **@cpolytrade_bot**.
2. **Edit Bot → Edit Botpic**, then send `telegram/bot-avatar.png` as a photo. Telegram
   crops it to a circle; the glyph keeps a margin inside it.
3. **Edit Bot → Edit Description Picture**, then send `telegram/bot-description.png`.
   It shows to people who open the bot before pressing Start.
