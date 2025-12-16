# new_parser.py
import aiohttp
import re
import asyncio
import logging
from typing import Dict, Optional, List, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class WBParser:
    async def setup(self):
        if not hasattr(self, 'session') or self.session is None:
            self.session = aiohttp.ClientSession()
            logger.info("✅ Сессия aiohttp создана")

    async def close(self):
        if hasattr(self, 'session') and self.session:
            await self.session.close()
            self.session = None
            logger.info("🛑 Сессия aiohttp закрыта")

    @staticmethod
    def extract_articul(url: str) -> Optional[str]:
        m = re.search(r'/catalog/(\d+)/detail', url)
        if m:
            return m.group(1)
        m2 = re.search(r'nm=(\d+)', url)
        if m2:
            return m2.group(1)
        return None

    async def parse_card_json(self, articul: str) -> Dict[str, Any]:
        """
        Парсинг card.json (если доступен) — собираем name, brand, description, images (полные url).
        """
        if not self.session:
            await self.setup()

        vol = articul[:4]
        part = articul[:6]
        json_url = f"https://sam-basket-cdn-01mt.geobasket.ru/vol{vol}/part{part}/{articul}/info/ru/card.json"
        try:
            async with self.session.get(json_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("imt_name") or data.get("name") or ""
                    brand = data.get("selling", {}).get("brand_name") or data.get("brand") or ""
                    description = data.get("description") or data.get("shortDescription") or ""
                    characteristics = {}
                    if isinstance(data.get("options"), list):
                        for opt in data.get("options", []):
                            try:
                                k = opt.get("name")
                                v = opt.get("value")
                                if k:
                                    characteristics[k] = v
                            except Exception:
                                continue

                    images: List[str] = []
                    # Попытки собрать URL-ы картинок, если они уже полные
                    for key in ("images", "imt_images", "pics", "gallery", "media", "mediaFiles"):
                        val = data.get(key)
                        if isinstance(val, list):
                            for it in val:
                                if isinstance(it, str) and it.startswith(("http://", "https://")):
                                    images.append(it)
                                elif isinstance(it, dict):
                                    u = it.get("url") or it.get("image")
                                    if isinstance(u, str) and u.startswith(("http://", "https://")):
                                        images.append(u)
                        elif isinstance(val, str) and val.startswith(("http://", "https://")):
                            images.append(val)

                    # очистка дубликатов
                    images = [u for i, u in enumerate(images) if images.index(u) == i]

                    return {
                        "name": name,
                        "brand": brand,
                        "description": description,
                        "characteristics": characteristics,
                        "images": images,
                    }
        except Exception as e:
            logger.debug(f"❌ Ошибка при получении card.json {json_url}: {e}", exc_info=True)

        return {}

    async def _check_url_is_image(self, url: str, timeout: float = 5.0) -> bool:
        """
        Проверка доступности URL-а картинки.
        Сначала делает HEAD, если HEAD отвечает неудачно — пытает GET с небольшим таймаутом и только заголовки.
        """
        if not self.session:
            await self.setup()
        try:
            # HEAD
            async with self.session.head(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status == 200:
                    ctype = resp.headers.get("Content-Type", "")
                    if ctype and ("image" in ctype or "webp" in ctype):
                        return True
                    # иногда WB отвечает без content-type, но статус 200 — считаем рабочим
                    return True
        except Exception:
            # попробуем GET, но не читаем тело полностью
            try:
                async with self.session.get(url, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status == 200:
                        ctype = resp.headers.get("Content-Type", "")
                        if ctype and ("image" in ctype or "webp" in ctype or "jpeg" in ctype or "jpg" in ctype):
                            return True
                        # если нет content-type — всё равно принимаем 200
                        return True
            except Exception:
                return False
        return False
    
    async def _find_valid_images(
        self, articul: str, candidate_idxs: List[int] = None, max_images: int = 2
    ) -> List[str]:
        """
        Проверяет все известные CDN (асинхронно и конкурентно),
        возвращает реально существующие картинки.
        """
        if not self.session:
            await self.setup()

        if candidate_idxs is None:
            candidate_idxs = list(range(1, max_images + 1))

        # Схемы: сначала новая, потом старая
        path_variants = [
            (articul[:4], articul[:6]),
            (articul[:3], articul[:5]),
        ]

        domains = [
            *(f"https://sam-basket-cdn-{str(i).zfill(2)}mt.geobasket.ru" for i in range(1, 10)),
            *(f"https://basket-{str(i).zfill(2)}.wbbasket.ru" for i in range(1, 10)),
            "https://cdn.wbstatic.net",
            "https://img1.wbstatic.net",
        ]

        subdirs = ["c516x688", "c800x1000", "c246x328", "big", ""]
        extensions = ["webp", "jpg", "jpeg"]

        # Собираем ВСЕ возможные URL для первой картинки (1.ext)
        test_urls = []
        for vol, part in path_variants:
            for domain in domains:
                for subdir in subdirs:
                    for ext in extensions:
                        subdir_path = f"/{subdir}" if subdir else ""
                        test_urls.append((
                            f"{domain}/vol{vol}/part{part}/{articul}/images{subdir_path}/1.{ext}",
                            vol, part, subdir, ext
                        ))

        async def check_candidate(url_info):
            url, vol, part, subdir, ext = url_info
            ok = await self._check_url_is_image(url, timeout=2.5)
            return (url_info if ok else None)

        # Проверяем все URL одновременно
        results = await asyncio.gather(*[check_candidate(info) for info in test_urls], return_exceptions=False)

        # выбираем первый рабочий вариант
        valid = next((r for r in results if r), None)
        if valid:
            url, vol, part, subdir, ext = valid
            domain = url.split("/vol")[0]
            subdir_path = f"/{subdir}" if subdir else ""
            logger.info(
                f"🖼️ Найден CDN для {articul}: {domain} "
                f"(vol={vol}, part={part}, subdir='{subdir}', ext={ext})"
            )
            return [
                f"{domain}/vol{vol}/part{part}/{articul}/images{subdir_path}/{i}.{ext}"
                for i in candidate_idxs[:max_images]
            ]

        # fallback — ничего не нашли
        logger.warning(f"⚠️ Не удалось найти изображения для {articul}, возвращаем fallback.")
        vol, part = articul[:3], articul[:5]
        return [
            f"https://sam-basket-cdn-03mt.geobasket.ru/vol{vol}/part{part}/{articul}/images/c516x688/{i}.webp"
            for i in candidate_idxs[:max_images]
        ]


    async def parse_api_detail(self, articul: str) -> Dict[str, Any]:
        """
        Получение деталей товара через card.wb.ru (v2).
        Возвращает: id, name, price, basic_price, seller, rating, feedbacks, stocks, stocks_by_size, images.
        """
        if not self.session:
            await self.setup()

        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&lang=ru&nm={articul}"
        logger.info(f"📩 Запрос к WB API: {url}")

        try:
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"❌ WB API вернул статус {resp.status} для артикула {articul}")
                    return {}
                data = await resp.json()
        except Exception as e:
            logger.error(f"❌ Ошибка запроса к WB API для артикула {articul}: {e}", exc_info=True)
            return {}

        products = data.get("data", {}).get("products") or []
        if not products:
            logger.warning(f"⚠️ В ответе WB API нет products для артикула {articul}")
            return {}

        p = products[0]
        sizes = p.get("sizes") or []

        logger.info(f"💰 WB RAW: salePriceU={p.get('salePriceU')}, priceU={p.get('priceU')} | sizes_count={len(sizes)}")

        # --- Цены ---
        sale_price = 0.0
        basic_price = 0.0

        # 1️⃣ Стандартные поля
        try:
            sale_u = p.get("salePriceU")
            price_u = p.get("priceU")
            if sale_u:
                sale_price = float(sale_u) / 100.0
            if price_u:
                basic_price = float(price_u) / 100.0
        except Exception:
            pass

        # 2️⃣ Fallback — если верхних полей нет
        if not sale_price or not basic_price:
            for s in sizes:
                price_info = s.get("price") or {}
                if isinstance(price_info, dict):
                    sale_price = float(price_info.get("product", 0)) / 100.0
                    basic_price = float(price_info.get("basic", 0)) / 100.0
                    if sale_price:
                        logger.info(f"💰 Fallback price from sizes: {sale_price}/{basic_price}")
                        break

        discount = int(100 - (sale_price / basic_price * 100)) if basic_price else 0

        # --- Остатки ---
        stocks_by_size: List[Dict[str, Any]] = []
        for s in sizes:
            qty = 0
            for st in s.get("stocks", []):
                try:
                    qty += int(st.get("qty", 0))
                except Exception:
                    pass
            stocks_by_size.append({
                "size": s.get("name") or "",
                "qty": qty
            })
        total_stocks = sum(i["qty"] for i in stocks_by_size)

        # --- Изображения ---
        images: List[str] = []
        pics_count = int(p.get("pics") or 0)
        if pics_count > 0:
            images = await self._find_valid_images(articul, candidate_idxs=list(range(1, min(pics_count, 3) + 1)))
        else:
            images = await self._find_valid_images(articul, candidate_idxs=[1, 2], max_images=2)

        result = {
            "id": p.get("id") or int(articul),
            "name": p.get("name"),
            "brand": p.get("brand"),
            "supplier": p.get("supplierName") or p.get("supplier"),
            "seller": p.get("supplierName") or p.get("supplier"),
            "rating": p.get("reviewRating") or p.get("rating") or 0,
            "feedbacks": p.get("feedbacks") or 0,
            "price": round(sale_price, 2),
            "basic_price": round(basic_price, 2),
            "discount": discount,
            "stocks": total_stocks,
            "stocks_by_size": stocks_by_size,
            "images": images,
        }

        logger.info(
            f"✅ Итог для {articul}: price={result['price']} base={result['basic_price']} "
            f"stocks={result['stocks']} images={len(images)}"
        )

        return result

    async def parse_product(self, url: str) -> Dict[str, Any]:
        """
        Основной метод: объединяем card.json и API (api_data имеет приоритет).
        """
        articul = self.extract_articul(url)
        if not articul:
            return {"success": False, "error": "Не удалось извлечь артикул из URL", "url": url}

        await self.setup()

        card_data = await self.parse_card_json(articul)
        api_data = await self.parse_api_detail(articul)

        if not card_data and not api_data:
            return {"success": False, "error": "Не удалось получить данные о товаре", "articul": articul}

        merged: Dict[str, Any] = {**card_data, **api_data}
        merged.update({
            "success": True,
            "articul": articul,
            "url": url,
            "id": int(api_data.get("id") or articul),
        })

        # если нет images из API — берем из card.json
        if not merged.get("images") and card_data.get("images"):
            merged["images"] = card_data.get("images")

        if merged.get("supplier") and not merged.get("seller"):
            merged["seller"] = merged.get("supplier")

        # можно удалить сырые данные, если не нужно
        # merged.pop("raw_product", None)

        return merged


# Утилиты
_parser: Optional[WBParser] = None

async def get_parser() -> WBParser:
    global _parser
    if _parser is None:
        _parser = WBParser()
    await _parser.setup()
    return _parser

async def parse_wb_product_api(url: str) -> Dict:
    parser = await get_parser()
    return await parser.parse_product(url)
