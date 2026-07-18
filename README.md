# Hyper Liquid Trader

ربات معامله‌گر خودکار Hyperliquid — موتور معامله + پنل کنترل تلگرام.

## ویژگی‌ها
- موتور معامله مستقل (`core_engine`) با استراتژی‌های or_low / or_high
- پنل مدیریت تلگرام (`control_bot`) برای تنظیم پارامترها، مشاهده وضعیت، بستن معامله و غیره
- گزارش‌گر تلگرام برای وضعیت حساب و معاملات
- پشتیبانی testnet / mainnet

## نصب یک‌خط (همه‌چی خودکار)
```bash
git clone https://github.com/matildameta/trading-bot-hl.git
cd trading-bot-hl
bash setup.sh
```
اسکریپت در ابتدا از شما می‌پرسد:
- کلید خصوصی Hyperliquid
- شبکه (testnet/mainnet)
- توکن ربات تلگرام (پنل)
- توکن ربات تلگرام (گزارش‌گر)
- آیدی چت مدیر
- مدل LLM پیش‌فرض
- کلید OpenRouter

سپس پایتون (در صورت نبود)، ابزارهای سیستم، محیط مجازی و همه‌ی وابستگی‌ها را نصب
و در نهایت هر دو بات را زیر `screen` بالا می‌آورد.

## اجرای دستی (بدون اسکریپت)
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r core_engine/requirements.txt -r control_bot/requirements.txt
# فایل config.yaml را از روی config.example.yaml پر کنید
screen -dmS traderbot bash -c "cd core_engine && ../.venv/bin/python -m src.main"
screen -dmS traderctl bash -c ".venv/bin/python src/bot.py"
```

## مدیریت
| کار | دستور |
|---|---|
| دیدن لاگ موتور | `screen -r traderbot` |
| دیدن لاگ پنل | `screen -r traderctl` |
| خروج از لاگ (بات زنده می‌ماند) | `Ctrl+A` سپس `D` |
| توقف موتور | `screen -S traderbot -X quit` |
| توقف پنل | `screen -S traderctl -X quit` |

## امنیت
فایل‌های `config.yaml` شامل کلیدهای واقعی هستند و در `.gitignore` قرار دارند —
**هرگز کامیت و پوش نشوند.** فقط `config.example.yaml` در مخزن است.

## پیش‌نیازها
- Python 3.10+ (اسکریپت 3.11/3.12 را ترجیح می‌دهد)
- git، gcc (برای کامپایل برخی پکیج‌ها)
- دسترسی sudo برای نصب پکیج‌های سیستم
