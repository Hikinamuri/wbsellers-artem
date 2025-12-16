from fastapi import FastAPI, Request, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError, InterfaceError
from sqlalchemy import text
from datetime import datetime, timezone
import httpx, uuid, hashlib, json
from yookassa import Configuration, Payment
from telegram import Bot
import os
import re
from database.db import get_session, AsyncSessionLocal
from database.models import Product, User, ProductStatus
from new_parser import parse_wb_product_api
import html  
from dotenv import load_dotenv
import time
import asyncio


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(BOT_TOKEN)

CHANNEL_ID = "@ekzoskidki" 
TELEGRAM_PROVIDER_TOKEN=os.getenv("TELEGRAM_PROVIDER_TOKEN")
PENDING_MESSAGES: dict[str, dict] = {}
YK_PENDING: dict[str, dict] = {}
PROCESSED_PAYMENTS: dict[str, dict] = {} 

bot = Bot(token=BOT_TOKEN)

app = FastAPI() 

scheduler = AsyncIOScheduler()
scheduler.start()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # можно указать ["http://localhost:5173"] если хочешь строго
    allow_credentials=True,
    allow_methods=["*"],  # разрешаем все методы (GET, POST, OPTIONS и т.д.)
    allow_headers=["*"],
)

def _sanitize_meta_field(value: any, max_len: int = 128) -> str:
    if value is None:
        return ""
    s = str(value)
    s = re.sub(r"[\r\n\t]+", " ", s).strip()
    if len(s) > max_len:
        return s[:max_len]
    return s

@app.on_event("startup")
async def startup_event():
    from database.db import test_connection
    await test_connection()


@app.post("/api/payments/create")
async def create_payment(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    MIN_PAYMENT_RUB = 1.0
    amount = float(data.get("amount", 1.0))
    if amount < MIN_PAYMENT_RUB:
        amount = MIN_PAYMENT_RUB

    meta = data.get("meta", {}) or {}

    order_id = str(uuid.uuid4())

    title = "Оплата размещения товара"
    description = f"Размещение товара: {meta.get('name', 'Товар')}"

    # Telegram требует сумму в КОПЕЙКАХ
    prices = [{"label": "Публикация", "amount": int(amount * 100)}]

    # 🔒 Санитизируем и сохраняем meta
    safe_meta = {
        "order_id": order_id,
        "user_id": _sanitize_meta_field(meta.get("user_id") or meta.get("tg_id") or "", 64),
        "url": _sanitize_meta_field(meta.get("url", ""), 200),
        "name": _sanitize_meta_field(meta.get("name", ""), 128),
        "description": _sanitize_meta_field(meta.get("description", ""), 200),
        "price": _sanitize_meta_field(meta.get("price", ""), 32),
        "scheduled_date": _sanitize_meta_field(meta.get("scheduled_date", ""), 64),
        "category": _sanitize_meta_field(meta.get("category", ""), 64),
    }

    print("🧾 SAFE META:", safe_meta)

    # ⚙️ Создаём платёж в YooKassa (тест или боевой режим)
    yookassa_secret = os.getenv("YOOKASSA_SECRET_KEY")
    yookassa_account = os.getenv("YOOKASSA_SHOP_ID")
    
    expires_at_dt = (datetime.utcnow() + timedelta(seconds=10)).replace(microsecond=0)
    expires_at_iso = expires_at_dt.isoformat() + "Z"

    yookassa_payment = {}
    
    if not yookassa_secret or not yookassa_account:
        print("⚠️ Не удалось получить ключи YooKassa")
    else:
        async with httpx.AsyncClient() as client:
            yookassa_payment = await client.post(
                "https://api.yookassa.ru/v3/payments",
                auth=(yookassa_account, yookassa_secret),
                headers={"Idempotence-Key": order_id},
                json={
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "confirmation": {
                        "type": "redirect",
                        "return_url": "https://t.me/WBerriesSeller_bot"
                    },
                    "capture": True,
                    # "test": False,
                    "test": True,
                    "description": description,
                    "metadata": safe_meta,
                    "expires_at": expires_at_iso,        
                    "receipt": {  # 👇 Обязательно при включённой фискализации
                        "customer": {
                            "email": "danya.pochta76@gmail.com",  # или phone
                        },
                        "items": [
                            {
                                "description": meta.get("name", "Публикация товара"),
                                "quantity": "1.00",
                                "amount": {
                                    "value": f"{amount:.2f}",
                                    "currency": "RUB"
                                },
                                "vat_code": 1,
                                "payment_subject": "service",
                                "payment_mode": "full_payment"  
                            }
                        ]
                    }
                },
                timeout=10.0,
            )
            yookassa_payment = yookassa_payment.json()

    # 🧠 Возвращаем данные для Telegram Bot API
    payment_id = yookassa_payment.get("id")
    
    return {
        "success": True,
        "payload": f"order_{order_id}",
        "title": title,
        "description": description,
        "currency": "RUB",
        "prices": prices,
        "provider_token": os.getenv("TELEGRAM_PROVIDER_TOKEN"),
        "metadata": safe_meta,

        "provider_data": {
            "yookassa_payment_id": payment_id
        },

        "yookassa_payment_id": payment_id,
    }

async def publish_product(product_id: int, max_retries: int = 3):
    """Публикует товар в канал с автопереподключением к БД при обрывах.
    Если категория = 18+, фото скрывается (спойлерится).
    """
    from database.db import AsyncSessionLocal
    from database.models import Product
    import html
    from sqlalchemy.exc import OperationalError, InterfaceError
    import asyncio

    for attempt in range(max_retries):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Product).where(Product.id == product_id))
                product = result.scalar_one_or_none()

                if not product:
                    print(f"❌ Товар с id={product_id} не найден")
                    return

                # 🧮 Извлекаем данные
                name = product.name or "Без названия"
                url = product.url or ""
                price = f"{int(product.price)} ₽" if product.price else "—"
                basic_price = f"{int(product.basic_price)} ₽" if product.basic_price else "—"
                stocks = product.stocks or 0
                wb_id = product.wb_id or "—"
                category = product.category or "Разное"

                caption = (
                    f"✅ <b><a href=\"{html.escape(url)}\">{html.escape(name)}</a></b>\n\n"
                    f"💰 <b>Цена со скидкой:</b> {price}\n"
                    f"💸 <s>Цена старая: {basic_price}</s>\n"
                    f"🛒 <b>Остаток:</b> {stocks} шт.\n"
                    f"📝 <b>Артикул:</b> {wb_id}\n\n"
                    f"#{category.replace(' ', '_')}"
                )

                # 🔞 Проверяем категорию
                is_adult = "18" in category or "adult" in category.lower() or "nsfw" in category.lower()

                # 📨 Отправляем пост
                try:
                    if product.image_url:
                        await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=product.image_url,
                            caption=caption[:1024],
                            parse_mode="HTML",
                            has_spoiler=is_adult  # 👈 вот тут магия
                        )
                    else:
                        await bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=caption[:1024],
                            parse_mode="HTML",
                        )

                    print(f"✅ Сообщение о товаре {product.id} отправлено в Telegram")
                except Exception as tg_err:
                    print(f"⚠️ Ошибка Telegram API при публикации {product_id}: {tg_err}")

                # 🧾 Обновляем статус
                product.status = "posted"
                await session.commit()

                print(f"✅ Товар опубликован: {product.name}")
                return

        except (OperationalError, InterfaceError) as db_err:
            print(f"⚠️ Ошибка соединения с БД при публикации {product_id}: {db_err}")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
                print(f"🔁 Повтор попытки ({attempt + 2}/{max_retries})...")
                continue
            else:
                print(f"❌ Не удалось подключиться к БД после {max_retries} попыток")
                return

        except Exception as e:
            print(f"❌ Неожиданная ошибка при публикации {product_id}: {e}")
            return

       
@app.post("/api/products/parse")
async def parse_product(request: Request):
    """
    Парсит карточку товара по URL, но НЕ сохраняет её в базу.
    """
    data = await request.json()
    url = data.get("url")

    if not url:
        return {"success": False, "error": "Не передан url"}

    print(f"📩 Запрос на парсинг товара: {url}")

    # 🧩 Парсим карточку товара
    product_data = await parse_wb_product_api(url)
    if not product_data or not product_data.get("success"):
        print(f"⚠️ Не удалось распарсить товар: {url}")
        return {"success": False, "error": "Не удалось получить данные с Wildberries"}

    print(f"✅ Товар успешно распарсен: {product_data.get('name')}")
    return product_data

@app.post("/api/products/add")
async def add_product(request: Request):
    data = await request.json()
    tg_id = data.get("user_id")
    url = data.get("url")
    name = data.get("name")
    description = data.get("description")
    image_url = data.get("image_url")
    price = data.get("price")
    scheduled_date = data.get("scheduled_date")
    category = data.get("category")
    
    print(f"📩 Запрос на добавление товара: {data}")

    if not all([tg_id, url, name, scheduled_date]):
        return {"success": False, "error": "Отсутствуют обязательные поля"}

    async for session in get_session():
        # Проверяем пользователя
        result = await session.execute(select(User).where(User.tg_id == str(tg_id)))
        user = result.scalar_one_or_none()
        if not user:
            return {"success": False, "error": "Пользователь не найден"}

        # Проверяем и парсим дату
        scheduled_dt = normalize_datetime(scheduled_date)
        if not scheduled_dt:
            return {"success": False, "error": "Некорректная дата (невозможно обработать)"}


        # 🧩 Парсим товар
        parsed = await parse_wb_product_api(url)
        if not parsed or not parsed.get("success"):
            parsed = {}
            print(f"⚠️ Не удалось распарсить товар: {url}")
        else:
            print(f"✅ Товар распарсен: {parsed.get('name')}")

        # 🖼 Основное изображение
        main_image = image_url or (parsed.get("images") or [None])[0]

        # 🏷 Категория (приоритет: фронт → парсер → запасное значение)
        categoryTry = data.get("category") 
        final_category = (
            category
            or parsed.get("category")
            or parsed.get("subcategory")
            or parsed.get("subject_name")
            or "Не указана"
        )
        print(f"📦 CATEGORY SELECTED: {categoryTry}")

        # 🧱 Создаём товар
        product = Product(
            user_id=str(tg_id),
            url=url,
            name=name or parsed.get("name"),
            description=description or parsed.get("description"),
            image_url=main_image,
            price=float(price) if price else (parsed.get("price") or 0.0),

            wb_id=int(parsed.get("id") or parsed.get("articul")) if parsed.get("id") or parsed.get("articul") else None,
            brand=parsed.get("brand"),
            seller=parsed.get("seller"),
            rating=float(parsed.get("rating")) if parsed.get("rating") is not None else None,
            feedbacks=int(parsed.get("feedbacks")) if parsed.get("feedbacks") is not None else None,
            basic_price=float(parsed.get("basic_price")) if parsed.get("basic_price") is not None else None,
            discount=int(parsed.get("discount")) if parsed.get("discount") is not None else None,
            stocks=int(parsed.get("stocks")) if parsed.get("stocks") is not None else None,
            stocks_by_size=parsed.get("stocks_by_size"),
            images=parsed.get("images"),
            info={"parsed_raw": parsed},
            status=ProductStatus.pending,
            category=final_category,  # ✅ теперь переменная определена
            scheduled_date=scheduled_dt,
        )

        # 💾 Сохраняем в БД
        session.add(product)
        await session.commit()
        await session.refresh(product)

        print(f"✅ Товар сохранён (ID={product.id}, Категория={product.category})")

        # ⏰ Планируем публикацию
        print(f"🕒 Серверное время сейчас: {datetime.now()}")
        print(f"🕒 scheduled_dt (для job): {scheduled_dt}")

        try:
            scheduler.add_job(
                publish_product,
                trigger=DateTrigger(run_date=scheduled_dt),
                args=[product.id],
                id=f"publish_{product.id}",
                replace_existing=True,  # 👈 чтобы не падало, если такая задача уже есть
                misfire_grace_time=300,
            )
            print(f"🗓 Задача добавлена: publish_{product.id}")
        except Exception as e:
            print(f"⚠️ Не удалось добавить задачу publish_{product.id}: {e}")


        print(f"🗓 Публикация запланирована на {scheduled_dt}")

        return {
            "success": True,
            "message": "Товар добавлен в очередь на выкладку",
            "product_id": product.id,
            "category": product.category,
        }

@app.post("/api/users/register")
async def register_user(request: Request):
    data = await request.json()
    tg_id = data.get("tg_id")
    name = data.get("name")
    phone = data.get("phone")

    if not tg_id or not phone:
        return {"success": False, "error": "Не переданы tg_id или телефон"}

    async for session in get_session():
        # Проверяем, существует ли уже пользователь
        result = await session.execute(select(User).where(User.tg_id == str(tg_id)))
        user = result.scalars().first()

        if not user:
            # Создаём нового
            user = User(tg_id=str(tg_id), name=name, phone=phone)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            print(f"✅ Новый пользователь зарегистрирован: {user.name} ({user.phone})")
        else:
            print(f"ℹ️ Пользователь уже есть: {user.name} ({user.phone})")

        return {"success": True, "user_id": user.id}
    
    
@app.get("/api/users/{tg_id}")
async def check_user_exists(tg_id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    return {"exists": user is not None}

@app.get("/api/products/{tg_id}")
async def get_user_products(tg_id: str, session: AsyncSession = Depends(get_session)):
    """Возвращает список товаров пользователя по его Telegram ID"""
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"success": False, "error": "Пользователь не найден"}

    # ✅ теперь ищем по строковому user_id (tg_id)
    result = await session.execute(select(Product).where(Product.user_id == user.tg_id))
    products = result.scalars().all()

    return {
        "success": True,
        "tg_id": tg_id,
        "user_id": user.tg_id,  # тоже исправляем, чтобы всё было консистентно
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.price,
                "url": p.url,
                "status": p.status.value if hasattr(p.status, "value") else p.status,
                "created_at": p.created_at,
                "scheduled_date": p.scheduled_date,
            }
            for p in products
        ],
    }

@app.post("/api/payments/callback")
async def yookassa_callback(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event = payload.get("event")
    obj = payload.get("object", {})  

    print("💳 YooKassa callback:", event)
    print("💳 CALLBACK RAW:", json.dumps(payload, ensure_ascii=False))

    metadata = obj.get("metadata", {}) or {}
    user_id = metadata.get("user_id") or metadata.get("tg_id")
    order_id = metadata.get("order_id")
    pid = obj.get("id")

    # Safety: если нет pid — просто ответим ok
    if not pid:
        print("⚠️ Callback без id -> игнорируем")
        return {"success": True}
    
    if pid in PROCESSED_PAYMENTS and PROCESSED_PAYMENTS[pid]["status"] == "succeeded":
        print(f"⚠️ Payment {pid} already succeeded, ignoring cancellation")
        return {"success": True}

    # Если уже обработано — не делать лишних действий (идемпотентность)
    processed = PROCESSED_PAYMENTS.get(pid)
    if processed:
        # если уже помечено как succeeded и мы получили canceled — игнорируем cancel
        if event == "payment.canceled" and processed.get("status") == "succeeded":
            print(f"ℹ️ Ignoring payment.canceled for {pid} because we've already processed succeeded")
            return {"success": True}
        # если уже помечено как canceled и пришёл succeeded — всё ещё обрабатывать succeeded (в редких race-условиях),
        # но если уже succeeded — просто вернуть OK.
        if event in ("payment.succeeded", "payment.captured", "payment.paid") and processed.get("status") == "succeeded":
            print(f"ℹ️ Duplicate succeeded callback for {pid} — игнорируем")
            return {"success": True}


    # ==== Обработка отмены платежа ====
    # if event == "payment.canceled":
    #     if pid in PROCESSED_PAYMENTS and PROCESSED_PAYMENTS[pid]["status"] == "succeeded":
    #         print(f"⚠️ Payment {pid} already succeeded, ignoring cancellation")
    #         return {"success": True}
    #     # если мы уже обрабатывали succeeded — выше вернули True
    #     YK_PENDING.pop(pid, None)
    #     PROCESSED_PAYMENTS[pid] = {"status": "canceled", "ts": time.time()}
    #     print(f"🚫 YooKassa callback marked payment canceled {pid}")

    #     if user_id:
    #         try:
    #             await bot.send_message(
    #                 chat_id=int(user_id),
    #                 text="⛔ <b>Оплата отменена</b>\nВы можете попробовать снова.",
    #                 parse_mode="HTML"
    #             )
    #         except Exception as e:
    #             print("Ошибка отправки пользователю (canceled):", e)

    #     # удаляем кнопку оплаты если есть
    #     if order_id and order_id in PENDING_MESSAGES:
    #         info = PENDING_MESSAGES.pop(order_id, None)
    #         if info:
    #             try:
    #                 await bot.delete_message(chat_id=info["chat_id"], message_id=info["message_id"])
    #             except Exception:
    #                 pass

    #     return {"success": True}

    # ==== Обработка успешной оплаты ====
    if event in ("payment.succeeded", "payment.captured", "payment.paid"):
        print(f"✅ Payment succeeded for id={pid}")
        # пометим как успешно обработанный
        PROCESSED_PAYMENTS[pid] = {"status": "succeeded", "ts": time.time()}

        # отменяем отложенные задачи-отмены, если они есть
        pending = YK_PENDING.pop(pid, None)
        if pending:
            # отменим фоновую задачу, если она сохранена
            task = pending.get("cancel_task")
            if task and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass

        # уведомляем пользователя
        if user_id:
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text="✅ <b>Оплата получена</b>\nТовар добавлен в очередь на выкладку.",
                    parse_mode="HTML"
                )
            except Exception as e:
                print("⚠️ Не получилось уведомить пользователя:", e)

        # удаляем кнопку оплаты (если есть)
        if order_id and order_id in PENDING_MESSAGES:
            info = PENDING_MESSAGES.pop(order_id, None)
            if info:
                try:
                    await bot.delete_message(chat_id=info["chat_id"], message_id=info["message_id"])
                except Exception as e:
                    print("⚠️ Ошибка удаления pending message:", e)

        # добавляем товар в базу асинхронно
        if metadata:
            try:
                asyncio.create_task(
                    add_product_to_db(
                        user_id=str(user_id),
                        url=metadata.get("url"),
                        name=metadata.get("name"),
                        description=metadata.get("description") or "",
                        image_url=metadata.get("image_url"),
                        price=float(metadata.get("price") or 0),
                        scheduled_date=metadata.get("scheduled_date"),
                        category=metadata.get("category"),
                    )
                )
            except Exception as e:
                print("⚠️ Ошибка при планировании add_product_to_db:", e)

        return {"success": True}

    # default
    return {"success": True}

async def add_product_to_db(
    user_id: str,
    url: str,
    name: str,
    description: str,
    image_url: str,
    price: float,
    scheduled_date: str,
    category: str = None, 
):
    from backend.new_parser import parse_wb_product_api  # локальный импорт

    async for session in get_session():
        result = await session.execute(select(User).where(User.tg_id == str(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            print(f"❌ Пользователь {user_id} не найден при добавлении товара в DB")
            return {"success": False, "error": "Пользователь не найден"}

        scheduled_dt = normalize_datetime(scheduled_date)
        if not scheduled_dt:
            print(f"❌ Некорректная дата: {scheduled_date}")
            return {"success": False, "error": "Некорректная дата"}


        # Парсим ещё раз, чтобы получить все поля (или можно принимать parsed из frontend)
        parsed = await parse_wb_product_api(url)
        if not parsed or not parsed.get("success"):
            print(f"⚠️ Не удалось дополнительно распарсить товар {url}")
            parsed = {}

        # Берём основную картинку - приоритет: image_url (переданный) -> parsed.images[0] -> parsed['images'] -> None
        main_image = image_url or (parsed.get("images") or [None])[0] or parsed.get("image") or None

        # Собираем extra info (оставляем копию parsed в info)
        extra_info = {
            "parsed_raw": parsed,  # можно убрать/сжать при желании
        }

        product = Product(
            user_id=str(user.tg_id),
            url=url,
            name=name or parsed.get("name"),
            description=description or parsed.get("description"),
            image_url=main_image,
            price=float(price) if price is not None else (parsed.get("price") or 0.0),

            # Новые поля
            wb_id=int(parsed.get("id") or parsed.get("articul")) if parsed.get("id") or parsed.get("articul") else None,
            brand=parsed.get("brand"),
            seller=parsed.get("seller"),
            rating=float(parsed.get("rating")) if parsed.get("rating") is not None else None,
            feedbacks=int(parsed.get("feedbacks")) if parsed.get("feedbacks") is not None else None,
            basic_price=float(parsed.get("basic_price")) if parsed.get("basic_price") is not None else None,
            discount=int(parsed.get("discount")) if parsed.get("discount") is not None else None,
            stocks=int(parsed.get("stocks")) if parsed.get("stocks") is not None else None,
            stocks_by_size=parsed.get("stocks_by_size"),
            images=parsed.get("images"),
            category=category,
            info=extra_info,
            status=ProductStatus.pending,
            scheduled_date=scheduled_dt,
        )

        session.add(product)
        await session.commit()
        await session.refresh(product)

        # Планируем публикацию
        try:
            scheduler.add_job(
                publish_product,
                trigger=DateTrigger(run_date=scheduled_dt),
                args=[product.id],
                id=f"publish_{product.id}",
            )
        except Exception as e:
            print(f"⚠️ Не удалось добавить задачу в scheduler: {e}")

        print(f"✅ Товар '{product.name}' сохранён и запланирован на {scheduled_dt}")
        return {"success": True, "product_id": product.id}


from datetime import timedelta
import pytz

@app.get("/api/admin/stats")
async def admin_stats(
    session: AsyncSession = Depends(get_session),
    type: str = Query("day", description="Тип периода: day|week|month|all"),
    year: int = Query(None, description="Год (например, 2025)"),
    month: int = Query(None, description="Месяц (1-12)"),
    week: int = Query(None, description="Номер недели (1–5 внутри месяца)"),
):
    """
    📊 Возвращает статистику по постам:
    - type=day → за сегодня
    - type=month&year=2025&month=1 → за январь 2025
    - type=week&year=2025&month=1&week=2 → за вторую неделю января 2025
    - type=all → за всё время
    """
    try:
        tz = pytz.timezone("Europe/Moscow")
        now = datetime.now(tz)

        # 🧮 Определяем временные границы
        if type == "day":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + timedelta(days=1)

        elif type == "month" and year and month:
            start_date = datetime(year, month, 1, tzinfo=tz)
            # следующий месяц минус 1 секунда
            if month == 12:
                end_date = datetime(year + 1, 1, 1, tzinfo=tz)
            else:
                end_date = datetime(year, month + 1, 1, tzinfo=tz)

        elif type == "week" and year and month and week:
            month_start = datetime(year, month, 1, tzinfo=tz)
            # считаем недельные интервалы от начала месяца
            week_start = month_start + timedelta(days=(week - 1) * 7)
            week_end = week_start + timedelta(days=7)
            start_date, end_date = week_start, week_end

        elif type == "all":
            start_date, end_date = None, None

        else:
            return JSONResponse(
                content={"success": False, "error": "Некорректные параметры периода"},
                status_code=400,
            )

        # 🧩 Запрос к БД
        query = select(Product)
        if start_date and end_date:
            query = query.where(Product.created_at >= start_date, Product.created_at < end_date)
        elif start_date:
            query = query.where(Product.created_at >= start_date)

        result = await session.execute(query)
        products = result.scalars().all()

        posted = [p for p in products if str(p.status) in ("posted", "ProductStatus.posted")]
        pending = [p for p in products if str(p.status) in ("pending", "ProductStatus.pending")]

        stats = {
            "type": type,
            "year": year,
            "month": month,
            "week": week,
            "total_posts": len(products),
            "posted_count": len(posted),
            "pending_count": len(pending),
            "posted_amount": len(posted) * 300,
            "pending_amount": len(pending) * 300,
        }

        return JSONResponse(content={"success": True, "stats": stats})

    except Exception as e:
        print(f"❌ Ошибка при вычислении статистики: {e}")
        return JSONResponse(content={"success": False, "error": str(e)}, status_code=500)


def normalize_datetime(value):
    if isinstance(value, str):
        # 🧠 Убираем Z и заменяем на совместимый с Python формат
        value = value.replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            print(f"⚠️ Невозможно распарсить дату: {value}")
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        else:
            return value.astimezone().replace(tzinfo=None)
    return value
