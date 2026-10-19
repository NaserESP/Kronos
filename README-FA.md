# Kronos-base — داشبورد پژوهشی محلی بیت‌کوین

این فورک کد اصلی [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos) را حفظ می‌کند و داشبورد فارسی پنج‌دقیقه‌ای را به آن اضافه می‌کند. معرفی و مستندات اصلی در [README.md](README.md) باقی مانده‌اند.

## قابلیت‌های افزوده

- دو مسیر هم‌زمان با یک مدل و GPU مشترک: کندل‌های واقعی BTCUSDT Spot بایننس و نمونه‌های مرزی Chainlink BTC/USD TWAP60 از metadata عمومی Limitless.
- حالت روشن/تیره، تاریخچه پایدار، خروجی CSV، نمایش سبز/قرمز و درصد موفقیت رنگ کندل بایننس.
- ارزیابی جهت هر مسیر با نتیجه رسمی بازار و مقایسه فقط روی دورهای مشترک معتبر.
- مسیر TWAP فقط نمونه مرزی دارد؛ OHLC کامل داخل بازه ندارد. ورودی آن O=H=L=C و حجم صفر است؛ فقط close پیش‌بینی‌شده ارزیابی می‌شود.
- اجرای محلی روی 127.0.0.1:7071؛ دریافت داده عمومی، بدون نیاز به حساب یا کلید. راهنمای رفتار و API: [dashboard/README-FA.md](dashboard/README-FA.md).

## نصب تازه در ویندوز

Python 3.12 نسخه ۶۴بیتی لازم است. دستورات زیر را در ریشه همین فورک اجرا کنید:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
# برای GPU سازگار با CUDA 12.6 و درایور NVIDIA مناسب:
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu126
# برای CPU، به جای دستور بالا:
# .\.venv\Scripts\python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe download_models.py
.\.venv\Scripts\python.exe prepare_btc_data.py
.\Start-Dashboard.cmd
```

دانلود وزن‌ها چند گیگابایت فضا و اینترنت می‌خواهد. دریافت داده بایننس، آرشیو ژوئیه تا سپتامبر ۲۰۲۶ را با checksum بررسی و به فرمت ورودی مدل تبدیل می‌کند. سپس داشبورد فاصله تا زمان حاضر را تکمیل می‌کند؛ شروع مسیر TWAP نیز نیازمند دریافت حداقل ۴۰۰ مرجع واقعی است.

`requirements-lock.txt` فهرست محیط مرجع ویندوز است؛ انتخاب PyTorch باید مطابق CPU/GPU شما انجام شود. اسکریپت‌های `download_torch.py` و `download_torch_cpu.py` ابزارهای کمکی دانلود wheel برای Python 3.12 ویندوز هستند و نصب خودکار انجام نمی‌دهند. روش پیشنهادی همان pip بالاست.

## اجرا و آزمون

- شروع: `Start-Dashboard.cmd`؛ توقف کامل: `Stop-Dashboard.cmd`.
- تست آفلاین و مستقل از داده واقعی:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s dashboard -p "test_*.py" -v
```

- تست همراه داده نمونه علی‌بابا: `Run-Test.cmd` یا `.\.venv\Scripts\python.exe run_test.py --offline`؛ خروجی‌ها در `results/` ساخته و در اجرای بعد بازنویسی می‌شوند.
- وزن‌ها و manifest در `models/`، داده بایننس در `data/` و تاریخچه زنده در `dashboard/state/` ساخته می‌شوند و از Git حذف‌اند. انتقال کد تاریخچه اجرای قبلی را منتقل نمی‌کند؛ پیش از جابه‌جایی نصب موجود، از داده‌ها جداگانه پشتیبان بگیرید.
- چیدمان قدیمی با کد اصلی در زیرپوشه `repo/` نیز در بارگذاری مدل پشتیبانی می‌شود. متغیر اختیاری `KRONOS_ROOT` ریشه داده و وزن‌ها را تعیین می‌کند؛ در نصب تازه لازم نیست.

این ابزار پژوهشی است؛ نتایج محدود، دقت آینده یا سودآوری را ثابت نمی‌کنند. هیچ مسیر معامله، کیف پول یا ثبت شرطی ندارد. افزودن کندل آموزش مجدد نیست.

## مجوزها

مجوز اصلی پروژه در `LICENSE` حفظ شده است. فونت Vazirmatn با مجوز OFL در `dashboard/static/OFL.txt` همراه است. مجوز و شرایط وزن‌های مدل در کارت‌های مدل دانلودشده باید جداگانه رعایت شوند.
