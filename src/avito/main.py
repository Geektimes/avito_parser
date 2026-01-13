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
from models import Advertisement

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

DB_DIR = "/home/debian/projects/avito/src/avito/DB"
DB_PATH = os.path.join(DB_DIR, "db.sqlite3")

ua = UserAgent(browsers=['chrome', 'edge'])
USER_DATA_DIR = os.path.join(os.getcwd(), "playwright_profile")

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        print(f"📁 Создана директория для БД: {DB_DIR}")
    await Tortoise.init(
        db_url=f'sqlite://{DB_PATH}',
        modules={'models': ['models']}
    )
    await Tortoise.generate_schemas()
    print("🗄️ База данных подключена.")

# --- ФУНКЦИЯ ЧЕЛОВЕЧЕСКОГО СКРОЛЛА ---
async def human_scroll(page):
    """
    Эмулирует чтение страницы пользователем:
    - Скроллит вниз с переменной скоростью.
    - Иногда делает небольшие паузы.
    - Не обязательно скроллит до самого пикселя низа, но проходит основной контент.
    """
    print("   👀 Эмуляция просмотра страницы (скроллинг)...")
    
    # Получаем высоту страницы
    total_height = await page.evaluate("document.body.scrollHeight")
    viewport_height = await page.evaluate("window.innerHeight")
    current_scroll = 0
    
    while current_scroll < total_height:
        # Случайный шаг скролла (от 300 до 800 пикселей)
        scroll_step = random.randint(300, 800)
        current_scroll += scroll_step
        
        # Выполняем скролл
        await page.mouse.wheel(0, scroll_step)
        
        # Если "улетели" ниже конца страницы, корректируем
        if current_scroll > total_height:
            current_scroll = total_height
            
        # Случайная задержка между рывками (как будто человек читает заголовки)
        # От 0.1 до 0.8 секунды
        await asyncio.sleep(random.uniform(0.1, 0.8))
        
        # Иногда делаем паузу побольше, будто заинтересовало объявление
        if random.random() < 0.1: # 10% шанс
            await asyncio.sleep(random.uniform(1.0, 2.0))
            
    # Небольшая пауза в конце перед действием
    await asyncio.sleep(1)


# --- ФУНКЦИЯ ПЕРЕХОДА НА СЛЕДУЮЩУЮ СТРАНИЦУ ---
async def go_to_next_page(page):
    next_button_selector = '[data-marker="pagination-button/nextPage"]'
    try:
        next_button = page.locator(next_button_selector).last
        if await next_button.is_visible():
            print("   -> ➡️ Клик по кнопке 'Далее'...")
            # Скролл к кнопке уже не обязателен, если мы проскроллили всю страницу human_scroll,
            # но для надежности оставим нативный метод
            await next_button.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await next_button.click()
            
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
                await page.wait_for_load_state("domcontentloaded")
            return True
        else:
            print("   -> 🛑 Кнопка 'Следующая страница' не найдена.")
            return False
    except Exception as e:
        print(f"   -> ⚠️ Ошибка навигации: {e}")
        return False

# --- ПАРСИНГ И СОХРАНЕНИЕ ---
# --- ПАРСИНГ И СОХРАНЕНИЕ В БД ---
async def process_page_data(page):
    processed_ids = []
    new_items_count = 0
    
    try:
        await page.wait_for_selector('div[data-marker="item"]', state='attached', timeout=10000)
    except:
        print("⚠️ Объявления не прогрузились.")
        return []

    items = await page.query_selector_all('div[data-marker="item"]')
    
    for item in items:
        try:
            # 1. ID Объявления
            avito_id = await item.get_attribute('data-item-id')
            if not avito_id:
                continue
            
            avito_id = int(avito_id)
            processed_ids.append(str(avito_id))

            # 2. ПРОВЕРКА В БАЗЕ
            exists = await Advertisement.exists(id=avito_id)
            if exists:
                continue

            # 3. ПАРСИНГ ПОЛЕЙ
            
            # Название
            title_el = await item.query_selector('[itemprop="name"]')
            title = await title_el.inner_text() if title_el else "Без названия"

            # Цена
            price_meta = await item.query_selector('meta[itemprop="price"]')
            price = 0
            if price_meta:
                p_val = await price_meta.get_attribute('content')
                price = int(p_val) if p_val and p_val.isdigit() else 0

            # Описание
            desc_meta = await item.query_selector('meta[itemprop="description"]')
            legend = await desc_meta.get_attribute('content') if desc_meta else None

            # Фото (первое)
            photo = None
            img_el = await item.query_selector('img')
            if img_el:
                photo = await img_el.get_attribute('src')
            
            
            # --- ИЗМЕНЕННЫЙ БЛОК SELLER ID ---
            seller_id = None
            
            # 1. Ищем любую ссылку в карточке, содержащую /user/ или /brands/
            # Мы убрали поиск по div[class*="sellerInfo"], так как он пропускал часть объявлений
            seller_link_el = await item.query_selector('a[href*="/user/"], a[href*="/brands/"]')
            print(f"seller_link_el = {seller_link_el}")
            
            if seller_link_el:
                href = await seller_link_el.get_attribute('href')
                if href:
                    # 2. Улучшенная регулярка: ищет после user/ или brands/
                    # [^/?]+ означает: "брать всё до первого знака '?' или '/'"
                    match = re.search(r'/(user|brands)/([^/?]+)', href)
                    if match:
                        # group(2) берет именно хэш (то, что во второй скобке)
                        seller_id = match.group(2) 
            # ---------------------------------



            # Дата
            date_el = await item.query_selector('[data-marker="item-date"]')
            date_text = await date_el.inner_text() if date_el else None

            # Город
            city_el = await item.query_selector('[class*="geo-root"]') 
            city = await city_el.inner_text() if city_el else None

            # 4. ЗАПИСЬ В БД
            await Advertisement.create(
                id=avito_id,
                title=title,
                price=price,
                seller_id=seller_id,
                date=date_text,
                legend=legend,
                is_favorite=False,
                city=city,
                photo=photo
            )
            new_items_count += 1

        except Exception as e:
            # print(f"Ошибка при обработке элемента: {e}")
            continue
            
    print(f"   💾 Сохранено новых объявлений: {new_items_count}")
    return processed_ids


# --- ЗАПУСК ---
async def run_parser(url):
    if os.path.exists(USER_DATA_DIR):
        try:
            shutil.rmtree(USER_DATA_DIR)
        except:
            pass

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
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        max_pages = 1
        current_page = 1
        total_processed = 0
        
        try:
            print(f"🌍 Переход на: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            
            while current_page <= max_pages:
                print(f"\n--- 📄 Страница {current_page} из {max_pages} ---")
                
                # 1. Сначала скроллим как человек (для защиты от бана)
                await human_scroll(page)
                
                # 2. Собираем данные (они уже все в DOM после загрузки, скролл не влияет на наличие атрибутов data-marker)
                page_ids = await process_page_data(page)
                
                if page_ids:
                    print(f"🔎 Обработано объявлений: {len(page_ids)}")
                    total_processed += len(page_ids)
                else:
                    print("⚠️ Пустая страница.")
                    if current_page == 1: break

                if current_page < max_pages:
                    success = await go_to_next_page(page)
                    if success:
                        current_page += 1
                        # Пауза после перехода на новую страницу, перед началом скролла
                        await asyncio.sleep(random.uniform(2, 4))
                    else:
                        break
                else:
                    print("✅ Лимит страниц исчерпан.")
                    break
            
            print("=" * 40)
            print(f"🏁 Сессия завершена. Обработано ID: {total_processed}")
            count = await Advertisement.all().count()
            print(f"📊 Всего в базе: {count}")
            print("=" * 40)
            await asyncio.sleep(2)

        except Exception as e:
            print(f"\n🔥 Ошибка: {e}")
        
        finally:
            await Tortoise.close_connections()
            await context.close()

if __name__ == "__main__":
    try:
        target_url = "https://www.avito.ru/volgogradskaya_oblast/noutbuki?f=ASgCAQECAkCo5A30D969xBHe2WaA2mbQ2WbE2WaS2maC2mbG2WbU2WbA2Wa02WbC2Wbm2Wa22Wb02WaGoRQk0uSOA87kjgMCRcaaDBp7ImZyb20iOjE4MDAwLCJ0byI6MTI5MDAwfZyhFBV7ImZyb20iOjE2LCJ0byI6bnVsbH0&localPriority=1&s=104"
        asyncio.run(run_parser(target_url))
    except KeyboardInterrupt:
        print("\n⛔ Стоп.")
