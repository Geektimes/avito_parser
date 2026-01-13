import asyncio
import os
import random
import shutil
import re
from dotenv import load_dotenv
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# --- ORM IMPORTS ---
from tortoise import Tortoise, run_async
from models import Advertisement  # Импортируем нашу модель

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
IP = os.getenv('PROXY_IP')
PORT = os.getenv('PROXY_PORT')
USER = os.getenv('PROXY_USER')
PASSWORD = os.getenv('PROXY_PASS')

LOCATION_PARAMS = {
    "locale": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "timezone_id": "Europe/Amsterdam",
    "geolocation": {"latitude": 52.370216, "longitude": 4.895168}
}

# Путь к базе данных
DB_DIR = "/home/debian/projects/avito/src/avito/DB"
DB_PATH = os.path.join(DB_DIR, "db.sqlite3")

ua = UserAgent(browsers=['chrome', 'edge'])
USER_DATA_DIR = os.path.join(os.getcwd(), "playwright_profile")


# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    # Создаем директорию, если её нет
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        print(f"📁 Создана директория для БД: {DB_DIR}")

    await Tortoise.init(
        db_url=f'sqlite://{DB_PATH}',
        modules={'models': ['models']}
    )
    # Создает таблицы, если их нет
    await Tortoise.generate_schemas()
    print("🗄️ База данных подключена и проверена.")


# --- ФУНКЦИЯ ПЕРЕХОДА НА СЛЕДУЮЩУЮ СТРАНИЦУ ---
async def go_to_next_page(page):
    next_button_selector = '[data-marker="pagination-button/nextPage"]'
    try:
        next_button = page.locator(next_button_selector).last
        if await next_button.is_visible():
            print("   -> ➡️ Переход на следующую страницу...")
            await next_button.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(1.5, 3.5))
            await next_button.click()
            
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
                await page.wait_for_load_state("domcontentloaded")
            return True
        else:
            print("   -> 🛑 Кнопка 'Следующая страница' не найдена (конец списка).")
            return False
    except Exception as e:
        print(f"   -> ⚠️ Ошибка навигации: {e}")
        return False


# --- ПАРСИНГ И СОХРАНЕНИЕ В БД ---
async def process_page_data(page):
    """
    Парсит элементы на странице, проверяет их наличие в БД 
    и сохраняет новые. Возвращает список обработанных ID.
    """
    processed_ids = []
    new_items_count = 0
    
    try:
        # Ждем загрузки списка
        await page.wait_for_selector('div[data-marker="item"]', state='attached', timeout=10000)
    except:
        print("⚠️ Объявления не прогрузились.")
        return []

    # Получаем все блоки объявлений
    items = await page.query_selector_all('div[data-marker="item"]')
    
    for item in items:
        try:
            # 1. ID Объявления
            avito_id = await item.get_attribute('data-item-id')
            if not avito_id:
                continue
            
            avito_id = int(avito_id)
            processed_ids.append(str(avito_id))

            # 2. ПРОВЕРКА В БАЗЕ (Если есть - пропускаем парсинг полей для экономии времени)
            exists = await Advertisement.exists(id=avito_id)
            if exists:
                # print(f"   Skip: {avito_id} (уже в базе)")
                continue

            # 3. ПАРСИНГ ПОЛЕЙ (только для новых)
            
            # Название
            title_el = await item.query_selector('[itemprop="name"]')
            title = await title_el.inner_text() if title_el else "Без названия"

            # Цена
            price_meta = await item.query_selector('meta[itemprop="price"]')
            price = 0
            if price_meta:
                p_val = await price_meta.get_attribute('content')
                price = int(p_val) if p_val and p_val.isdigit() else 0

            # Описание (legend)
            desc_meta = await item.query_selector('meta[itemprop="description"]')
            legend = await desc_meta.get_attribute('content') if desc_meta else None

            # --- БЛОК ПОЛУЧЕНИЯ ФОТО (v3.0 - Самый надежный) ---
            photo = None
            
            # СПОСОБ 1: Извлекаем из атрибута data-marker элемента списка (там ссылка есть всегда)
            # Пример: data-marker="slider-image/image-https://..."
            slider_item = await item.query_selector('li[data-marker*="slider-image"]')
            
            if slider_item:
                marker_attr = await slider_item.get_attribute('data-marker')
                if marker_attr and "image-" in marker_attr:
                    # Разделяем строку по "image-" и берем вторую часть (саму ссылку)
                    photo = marker_attr.split("image-")[-1]
            
            # СПОСОБ 2 (Резервный): Если слайдера нет, ищем тег img с itemprop="image"
            if not photo:
                print("СПОСОБ 2 (Резервный): Если слайдера нет, ищем тег img")
                img_el = await item.query_selector('img[itemprop="image"]')
                if img_el:
                    # Сначала пробуем src
                    photo = await img_el.get_attribute('src')
                    
                    # Если src пуст (ленивая загрузка), пробуем srcset
                    if not photo:
                        srcset = await img_el.get_attribute('srcset')
                        if srcset:
                            # srcset выглядит как "url 1x, url 2x", берем первый url
                            photo = srcset.split(" ")[0]
            # ---------------------------------------------------

            # Дата
            date_el = await item.query_selector('[data-marker="item-date"]')
            date_text = await date_el.inner_text() if date_el else None

            # Город (ищем в блоке geo)
            # Селекторы города часто меняются, ищем по классу или маркеру
            city_el = await item.query_selector('[class*="geo-root"]') 
            city = await city_el.inner_text() if city_el else None

            # Seller ID (из ссылки)
            # Ссылка обычно внутри блока title или user info
            seller_id = None
            link_el = await item.query_selector('a[href*="/user/"]')
            if link_el:
                href = await link_el.get_attribute('href')
                # Пытаемся вытащить хэш юзера из ссылки вида /user/HASH/profile...
                match = re.search(r'/user/([^/]+)/', href)
                if match:
                    seller_id = match.group(1)

            # 4. ЗАПИСЬ В БД
            await Advertisement.create(
                id=avito_id,
                title=title,
                price=price,
                seller_id=seller_id,
                date=date_text,
                legend=legend,
                is_favorite=False, # По умолчанию
                city=city,
                photo=photo
            )
            new_items_count += 1
            # print(f"   New: {avito_id} saved.")

        except Exception as e:
            print(f"Ошибка при обработке элемента: {e}")
            continue
            
    print(f"   💾 Сохранено новых объявлений: {new_items_count}")
    return processed_ids


# --- ОСНОВНОЙ ПАРСЕР ---
async def run_parser(url):
    # Очистка профиля браузера (опционально)
    if os.path.exists(USER_DATA_DIR):
        try:
            shutil.rmtree(USER_DATA_DIR)
        except:
            pass

    # Инициализация БД
    await init_db()

    async with Stealth().use_async(async_playwright()) as p:
        ua_string = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        launch_options = {
            "headless": False,
            "args": ['--disable-webrtc', '--disable-blink-features=AutomationControlled'],
            "user_agent": ua_string,
            "viewport": {'width': 1920, 'height': 940},        
            **LOCATION_PARAMS
        }
        
        context = await p.chromium.launch_persistent_context(USER_DATA_DIR, **launch_options)
        
        # Скрываем webdriver
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        
        page = context.pages[0] if context.pages else await context.new_page()
        print(f"User-Agent: {await page.evaluate('navigator.userAgent')}")
        
        max_pages = 3
        current_page = 1
        total_processed = 0
        
        try:
            print(f"🌍 Переход на: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
            
            # while current_page <= max_pages:
            while current_page <= 1:
                print(f"\n--- 📄 Обработка страницы {current_page} из {max_pages} ---")
                
                # Запускаем парсинг и сохранение в БД
                page_ids = await process_page_data(page)
                
                if page_ids:
                    print(f"🔎 Найдено на странице: {len(page_ids)} объявлений")
                    total_processed += len(page_ids)
                else:
                    print("⚠️ Объявления не найдены. Возможно, бан или капча.")
                    if current_page == 1: break

                # Пагинация
                if current_page < max_pages:
                    success = await go_to_next_page(page)
                    if success:
                        current_page += 1
                        await asyncio.sleep(random.uniform(3, 6)) # Пауза между страницами
                    else:
                        break
                else:
                    print("✅ Достигнут лимит страниц.")
                    break
            
            print("=" * 40)
            print(f"🏁 Работа завершена. Обработано ID всего: {total_processed}")
            
            # Подсчет статистики из БД
            count = await Advertisement.all().count()
            print(f"📊 Всего записей в базе данных: {count}")
            print("=" * 40)

            await asyncio.sleep(2)

        except Exception as e:
            print(f"\n🔥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        
        finally:
            await Tortoise.close_connections()
            await context.close()

if __name__ == "__main__":
    try:
        # Ссылка с фильтрами из вашего примера
        target_url = "https://www.avito.ru/volgogradskaya_oblast/noutbuki?f=ASgCAQECAkCo5A30D969xBHe2WaA2mbQ2WbE2WaS2maC2mbG2WbU2WbA2Wa02WbC2Wbm2Wa22Wb02WaGoRQk0uSOA87kjgMCRcaaDBp7ImZyb20iOjE4MDAwLCJ0byI6MTI5MDAwfZyhFBV7ImZyb20iOjE2LCJ0byI6bnVsbH0&localPriority=1&s=104"
        asyncio.run(run_parser(target_url))
    except KeyboardInterrupt:
        print("\n⛔ Программа остановлена.")
