import asyncio
import os
import random
import re
import sys
from dotenv import load_dotenv
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from loguru import logger

# --- ORM IMPORTS ---
from tortoise import Tortoise
from models import Advertisement

load_dotenv()

# --- КОНФИГУРАЦИЯ ЛОГИРОВАНИЯ ---
logger.remove()  # Удаляем стандартный обработчик
# Лог в консоль (красивый и цветной)
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>", level="INFO")
# Лог в файл (для отладки и истории)
logger.add("parser.log", rotation="1 MB", retention="10 days", level="DEBUG", encoding="utf-8")

# --- КОНФИГУРАЦИЯ ---
LOCATION_PARAMS = {
    "locale": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "timezone_id": "Europe/Amsterdam",
    "geolocation": {"latitude": 52.370216, "longitude": 4.895168}
}

DB_DIR = "/home/debian/projects/avito/src/avito/DB"
DB_PATH = os.path.join(DB_DIR, "db.sqlite3")
USER_DATA_DIR = os.path.join(os.getcwd(), "profile")

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        logger.info(f"📁 Создана директория для БД: {DB_DIR}")
    await Tortoise.init(
        db_url=f'sqlite://{DB_PATH}',
        modules={'models': ['models']}
    )
    await Tortoise.generate_schemas()
    logger.success("🗄️ База данных подключена.")

# --- ФУНКЦИЯ ПЕРЕХОДА НА СЛЕДУЮЩУЮ СТРАНИЦУ ---
async def go_to_next_page(page):
    next_button_selector = '[data-marker="pagination-button/nextPage"]'
    try:
        next_button = page.locator(next_button_selector).last
        if await next_button.is_visible():
            logger.info("➡️ Клик по кнопке 'Далее'...")
            await next_button.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(1.0, 2.5))
            await next_button.click()
            await page.wait_for_load_state("domcontentloaded")
            return True
        else:
            logger.warning("🛑 Кнопка 'Следующая страница' не найдена.")
            return False
    except Exception as e:
        logger.error(f"⚠️ Ошибка навигации: {e}")
        return False

async def authorize_bot_on_site(page):
    login_button_selector = '[data-marker="header/login-button"]'
    if await page.is_visible(login_button_selector):
        logger.warning("🔒 БОТ НЕ АВТОРИЗОВАН! Пожалуйста, войдите в аккаунт и нажмите ENTER в консоли...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        logger.success("✅ Авторизация принята.")
        await page.wait_for_load_state("networkidle")
    else:
        logger.debug("✅ Сессия авторизации найдена.")

async def process_page_data(page):
    processed_ids = []
    new_items_count = 0
    stop_parsing = False
    
    # 1. Поиск контейнера по ТЗ
    container = page.locator('#bx_serp-item-list')
    
    if await container.count() == 0:
        logger.error("❌ Контейнер #bx_serp-item-list не найден на странице.")
        return None, True # Сигнал завершения работы

    # 2. Поиск объявлений внутри
    items_locator = container.locator('div[data-marker="item"]')
    items = await items_locator.all()
    
    if not items:
        logger.warning("❌ В контейнере нет объявлений.")
        return None, True

    # 3. Проверка на дубликаты и "отсечение" старых данных
    final_items_to_process = []
    for item in items:
        avito_id_str = await item.get_attribute('data-item-id')
        if not avito_id_str:
            continue
        
        avito_id = int(avito_id_str)
        
        # Если нашли ID в базе — прекращаем сбор
        if await Advertisement.filter(id=avito_id).exists():
            logger.info(f"⏸️ Найдено объявление {avito_id}, которое уже есть в базе. Парсинг новых завершен.")
            stop_parsing = True
            break
        else:
            final_items_to_process.append(item)

    # 4. Обработка только новых элементов
    for item in final_items_to_process:
        try:
            avito_id_str = await item.get_attribute('data-item-id')
            avito_id = int(avito_id_str)

            # Извлечение данных (кратко)
            title_loc = item.locator('[itemprop="name"]')
            title = await title_loc.inner_text() if await title_loc.count() > 0 else "Без названия"

            price_loc = item.locator('meta[itemprop="price"]')
            price = int(await price_loc.get_attribute('content')) if await price_loc.count() > 0 else 0

            # Поиск seller_id
            seller_id = None
            all_hrefs = await item.locator('a').evaluate_all("els => els.map(e => e.href)")
            for href in all_hrefs:
                match = re.search(r'/(user|brands)/([^/?]+)', href)
                if match:
                    seller_id = match.group(2)
                    break

            # Создание записи в БД
            await Advertisement.create(
                id=avito_id,
                title=title,
                price=price,
                seller_id=seller_id,
                # ... другие поля из вашей модели ...
            )
            processed_ids.append(str(avito_id))
            new_items_count += 1
            logger.debug(f"➕ Добавлено: {avito_id} | {title[:30]}...")
            
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке элемента {avito_id_str}: {e}")
            continue
            
    if new_items_count > 0:
        logger.success(f"💾 Обработано новых объявлений на странице: {new_items_count}")
    
    return processed_ids, stop_parsing


# --- ЗАПУСК ---
async def run_parser(url):
    await init_db()

    async with Stealth().use_async(async_playwright()) as p:
        ua_string = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, 
            headless=False,
            user_agent=ua_string,
            **LOCATION_PARAMS
        )

        page = context.pages[0] if context.pages else await context.new_page()
        current_page = 1
        total_new = 0
        
        try:
            logger.info(f"🌍 Переход на: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            await authorize_bot_on_site(page)
             
            while True:
                logger.info(f"📄 Анализ страницы {current_page}...")
                
                page_ids, should_stop = await process_page_data(page)
                
                # Если вернулся None — значит контейнер не найден или пуст
                if page_ids is None:
                    logger.info("🏁 Работа парсера завершена по условию отсутствия контента.")
                    break
                
                total_new += len(page_ids)
                
                # Если сработал триггер старого объявления
                if should_stop:
                    logger.info("✅ Актуальные данные собраны. Выход.")
                    break
                
                if not await go_to_next_page(page):
                    break
                
                current_page += 1
                await asyncio.sleep(random.uniform(2, 5))
            
            logger.info("=" * 40)
            logger.success(f"🏁 Итог сессии: добавлено {total_new} объявлений.")
            db_count = await Advertisement.all().count()
            logger.info(f"📊 Всего в базе записей: {db_count}")
            logger.info("=" * 40)

        except Exception as e:
            logger.critical(f"🔥 Критическая ошибка: {e}")
        finally:
            await Tortoise.close_connections()
            await context.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_parser(os.getenv('AVITO_URL_2')))
    except KeyboardInterrupt:
        logger.warning("⛔ Работа прервана пользователем.")
