# Docker و Coolify

## لوکال با Docker

```bash
docker compose up --build -d
```

- پنل: http://127.0.0.1:8000
- noVNC (لاگین Chromium): http://127.0.0.1:6080

## Coolify

1. New Resource → Docker Compose (همین ریپو، فایل `docker-compose.yml`).
2. Volume لازم است: مسیر کانتینر `/app/data` را به یک Persistent Volume وصل کن
   (در `docker-compose.yml` همان `divar-data:/app/data` است).
   داخلش می‌ماند: `panel.db` + `chrome-profiles/` (لاگین هر اکانت).
   بدون volume بعد از هر deploy دوباره باید لاگین کنی.
3. دو پورت را publish کن:
   - `8000` → دامنه پنل (مثلاً `https://divar.example.com`)
   - `6080` → دامنه noVNC (مثلاً `https://vnc.example.com`) یا فقط IP:6080
4. Env پیشنهادی:

```env
ENABLE_VNC=1
BROWSER_HEADLESS=0
DISPLAY=:99
DATA_DIR=/app/data
PORT=8000
VNC_URL=https://divar2.pixlink.ir/vnc.html
```

اگر کپچا بیاید، بنر قرمز در پنل ظاهر می‌شود: لینک VNC + دکمه «کپچا حل شد».

5. پنل را پشت Basic Auth / فایروال بگذار؛ احراز هویت داخلی ندارد.

## چطور در Chromium لاگین کنم؟

### روش ۱ — noVNC (پیشنهادی روی سرور)

1. در مرورگر خودت برو به noVNC (`:6080`).
2. دسکتاپ مجازی را می‌بینی.
3. پنل را باز کن (`:8000`) → برگه **حساب‌ها** → **باز کردن کروم برای لاگین**.
4. پنجره Chromium داخل noVNC باز می‌شود؛ با شماره وارد دیوار شو.
5. بعد از لاگین موفق، پنجره را باز نگه ندار؛ نشست در volume می‌ماند.
6. کار خزش با «دریافت شماره» را شروع کن.

### روش ۲ — کپی پروفایل از مک

روی مک یک‌بار لاگین کن، بعد پروفایل را به سرور بفرست:

```bash
# مک
tar czf chrome-profile.tgz -C data chrome-profile

# روی سرور / داخل volume Coolify
# محتویات را در /app/data/chrome-profile بریز
```

## نکات

- بدون لاگین، دکمه «اطلاعات تماس» به `LOGIN_REQUIRED` می‌خورد.
- `BROWSER_HEADLESS=1` را فقط وقتی بگذار که پروفایل از قبل لاگین شده باشد؛ برای لاگین دستی از noVNC با `BROWSER_HEADLESS=0` استفاده کن.
- Dockerfile بر پایه image رسمی Playwright است و Chromium از قبل داخلش هست.
