"""
Парсер объявлений Avito.ru на базе curl_cffi.

curl_cffi используется вместо requests/httpx, потому что он умеет
имитировать TLS/HTTP2-фингерпринт реального браузера (Chrome/Firefox),
что помогает проходить проверки анти-бот систем, которые банят
стандартные Python-клиенты по JA3/JA4-отпечатку.

ВАЖНО:
- Верстка Avito регулярно меняется, поэтому CSS-селекторы в функции
  parse_listing_card() может понадобиться актуализировать.
- Avito активно применяет rate-limiting, капчи и блокировки IP.
  Скрипт делает паузы между запросами и поддерживает прокси,
  но не гарантирует обход всех защит.
- Используйте разумные интервалы и не создавайте чрезмерную нагрузку.
"""

from __future__ import annotations

import csv
import json
import logging
import random
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

logger = logging.getLogger("avito_parser")

BASE_URL = "https://www.avito.ru"

# Список TLS-профилей для имитации разных браузеров/версий.
# curl_cffi сам подбирает соответствующий TLS ClientHello и HTTP/2 фрейминг.
IMPERSONATE_PROFILES = [
    "chrome124",
    "chrome123",
    "chrome120",
    "safari17_0",
]

# Куки реального браузера (f/ft/sx) привязаны к семейству его TLS/UA-фингерпринта:
# с профилями Chrome сервер отвечает 403, со Safari — отдаёт полную выдачу.
# Поэтому при работе с чужими куками профиль не рандомизируем.
COOKIE_FRIENDLY_PROFILE = "safari17_0"

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _make_soup(html: str) -> BeautifulSoup:
    """
    html.parser вместо lxml: libxml2 обрывается на текущей разметке Avito
    (после ~100 тегов в <head> дальнейший документ теряется), и все карточки
    объявлений пропадают. Встроенный parser обрабатывает страницу целиком.
    """
    return BeautifulSoup(html, "html.parser")


@dataclass
class Listing:
    """Одно объявление."""

    id: Optional[str] = None
    title: Optional[str] = None
    price: Optional[str] = None
    price_raw: Optional[int] = None
    url: Optional[str] = None
    location: Optional[str] = None
    date_published: Optional[str] = None
    image_url: Optional[str] = None
    seller: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class AvitoBlockedError(RuntimeError):
    """Похоже, запрос заблокирован антибот-системой (капча/бан)."""


class AvitoParser:
    def __init__(
        self,
        proxies: Optional[list[str]] = None,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        max_retries: int = 3,
        timeout: int = 20,
        cookies: Optional[dict | str] = None,
        debug_dump_dir: Optional[str | Path] = None,
        warm_up: bool = True,
    ) -> None:
        self.proxies = proxies or []
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.debug_dump_dir = Path(debug_dump_dir) if debug_dump_dir else None
        if self.debug_dump_dir:
            self.debug_dump_dir.mkdir(parents=True, exist_ok=True)

        # Базовые куки (например, скопированные из браузера): либо строка
        # формата "name=value; name2=value2", либо готовый словарь.
        self._base_cookies: dict[str, str] = {}
        if isinstance(cookies, str):
            self._base_cookies.update(self._parse_cookie_header(cookies))
        elif isinstance(cookies, dict):
            self._base_cookies.update(cookies)

        self._session: Optional[cf_requests.Session] = None
        self._rotate_session()

        if warm_up:
            self._warm_up()

    def _warm_up(self) -> None:
        """
        Заходит на главную страницу перед первым поисковым запросом —
        так сессия получает обычные cookies и выглядит как заход живого
        пользователя, а не прямой запрос сразу в поисковую выдачу.
        """
        try:
            assert self._session is not None
            logger.debug("Прогрев сессии: захожу на %s", BASE_URL)
            self._session.get(BASE_URL)
            time.sleep(random.uniform(1.5, 3.0))
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось прогреть сессию, продолжаю без этого", exc_info=True)

    # ------------------------------------------------------------------ #
    # Служебные методы
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_cookie_header(header: str) -> dict[str, str]:
        """Разбирает строку заголовка Cookie в словарь."""
        result: dict[str, str] = {}
        for part in header.replace("\n", "; ").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            result[name.strip()] = value.strip()
        return result

    def _collect_cookies(self) -> dict[str, str]:
        """Базовые куки + всё, что сервер положил в текущую сессию."""
        merged = dict(self._base_cookies)
        if self._session is not None:
            try:
                merged.update(dict(self._session.cookies))
            except Exception:  # noqa: BLE001
                pass
        return merged

    def _rotate_session(self) -> None:
        """Создаёт новую сессию со случайным TLS-профилем (и прокси, если заданы)."""
        if self._base_cookies:
            impersonate = COOKIE_FRIENDLY_PROFILE
        else:
            impersonate = random.choice(IMPERSONATE_PROFILES)
        proxy = random.choice(self.proxies) if self.proxies else None
        proxies_arg = {"http": proxy, "https": proxy} if proxy else None
        cookies_arg = self._collect_cookies() or None

        logger.debug("Новая сессия: impersonate=%s, proxy=%s", impersonate, proxy)
        self._session = cf_requests.Session(
            impersonate=impersonate,
            headers=DEFAULT_HEADERS,
            proxies=proxies_arg,
            timeout=self.timeout,
            cookies=cookies_arg,
        )

    def _dump_debug_html(self, html: str, url: str, tag: str) -> None:
        """Сохраняет сырой HTML на диск, если включён debug_dump_dir — чтобы
        можно было своими глазами посмотреть, что реально прислал сервер,
        вместо того чтобы гадать по логам."""
        if not self.debug_dump_dir:
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() else "_" for c in url)[-60:]
        path = self.debug_dump_dir / f"{ts}_{tag}_{safe_name}.html"
        try:
            path.write_text(html, encoding="utf-8")
            logger.info("Сохранил HTML для диагностики: %s", path)
        except OSError:
            logger.debug("Не удалось сохранить debug HTML", exc_info=True)

    def _sleep_between_requests(self) -> None:
        delay = random.uniform(self.min_delay, self.max_delay)
        logger.debug("Пауза %.2f сек", delay)
        time.sleep(delay)

    def _get(self, url: str, params: Optional[dict] = None) -> cf_requests.Response:
        """GET-запрос с ретраями, ротацией сессии/прокси при блокировках."""
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                assert self._session is not None
                resp = self._session.get(url, params=params)

                if resp.status_code == 200:
                    # Эвристики блокировки рассчитаны на HTML; JSON-ответы
                    # внутренних эндпоинтов (короткие!) проверяем отдельно
                    # в месте вызова.
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ctype and self._looks_blocked(resp.text):
                        self._dump_debug_html(resp.text, url, "suspected_block")
                        raise AvitoBlockedError(
                            f"Похоже на блокировку (status=200, url={resp.url})"
                        )
                    return resp

                if resp.status_code in (403, 429, 503):
                    self._dump_debug_html(resp.text, url, f"status_{resp.status_code}")
                    retry_after = resp.headers.get("Retry-After")
                    raise AvitoBlockedError(
                        f"Заблокировано, status_code={resp.status_code}, url={url}, "
                        f"Retry-After={retry_after}"
                    )

                resp.raise_for_status()
                return resp

            except (AvitoBlockedError, cf_requests.exceptions.RequestException) as exc:
                last_exc = exc
                # Экспоненциальная пауза перед следующей попыткой — важно не долбить
                # сервер сразу же после подозрения на блокировку, иначе можно
                # получить настоящий rate-limit (429) там, где его изначально не было.
                backoff = random.uniform(5, 12) * (2 ** (attempt - 1))
                logger.warning(
                    "Попытка %s/%s не удалась (%s). Меняю сессию/прокси и жду %.1f сек.",
                    attempt,
                    self.max_retries,
                    exc,
                    backoff,
                )
                self._rotate_session()
                time.sleep(backoff)

        raise RuntimeError(f"Не удалось получить {url}: {last_exc}")

    @staticmethod
    def _looks_blocked(html: str) -> bool:
        """
        Определяет реальную страницу-блокировку, а не обычную страницу выдачи.

        ВАЖНО: раньше здесь искалось слово "captcha" по всему HTML — но Avito
        подключает библиотеку reCAPTCHA (упоминания "captcha" в инлайн-JS/CSS)
        практически на всех страницах, включая обычную выдачу. Из-за этого
        нормальные ответы ошибочно считались блокировкой, парсер начинал
        ретраить и в итоге сам себя загонял под настоящий rate-limit (429).

        Теперь проверяем:
        1) заголовок <title> страницы — на реальных блокировках он специфичный
           ("Доступ ограничен", "Ой!" и т.п.), а не "Авито...";
        2) отсутствие обычных маркеров листинга (data-marker="item") —
           на блок-странице их не будет вовсе;
        3) длину страницы — блок-страницы обычно значительно короче обычной
           выдачи с десятками карточек.
        """
        soup = _make_soup(html)

        title = (soup.title.get_text(strip=True) if soup.title else "").lower()
        block_titles = ["доступ ограничен", "ошибка 403", "just a moment", "attention required"]
        title_looks_blocked = any(marker in title for marker in block_titles)

        has_listing_markers = bool(soup.select('div[data-marker="item"]')) or bool(
            soup.select('[data-marker^="item"]')
        )

        # Явный текст блокировки в теле страницы (более узкий список, чем раньше)
        body_text = soup.get_text(" ", strip=True).lower()
        body_block_phrases = [
            "мы обнаружили подозрительную активность",
            "подтвердите, что запросы отправляете вы, а не робот",
            "ваш ip-адрес временно заблокирован",
        ]
        body_looks_blocked = any(p in body_text for p in body_block_phrases)

        is_suspiciously_short = len(html) < 3000

        blocked = (
            title_looks_blocked
            or body_looks_blocked
            or (is_suspiciously_short and not has_listing_markers)
        )

        if blocked:
            logger.debug(
                "Признаки блокировки: title=%r, has_listing_markers=%s, len(html)=%s",
                title,
                has_listing_markers,
                len(html),
            )

        return blocked

    # ------------------------------------------------------------------ #
    # Публичное API
    # ------------------------------------------------------------------ #
    def get_html(self, url: str, params: Optional[dict] = None) -> str:
        """Публичная обёртка над _get: возвращает HTML страницы."""
        return self._get(url, params=params).text

    def pause(self) -> None:
        """Пауза между запросами (по настройкам min_delay/max_delay)."""
        self._sleep_between_requests()

    def get_api_items(self, search_url: str, page: int = 1) -> list[Listing]:
        """
        Загружает выдачу через внутренний JSON-эндпоинт SPA:
            GET /web/1/items/<путь категории>?<те же фильтры>

        Нужен для URL с фильтрами вида "f=ASgB..." — такие страницы Avito
        отдаёт только как пустой SPA-каркас без SSR, а данные фронтенд
        сам берёт этим запросом. Принимает обычный адрес из браузера.

        Один раз следует подсказке редиректа (canonical slug URL).
        """
        parts = urlsplit(search_url)
        api_url = BASE_URL + "/web/1/items" + parts.path
        params: dict[str, str] = dict(parse_qsl(parts.query))
        if page > 1:
            params["p"] = str(page)

        resp = self._get(api_url, params=params)
        data = self._json_or_blocked(resp)

        if data.get("redirected") and data.get("url"):
            canonical = urlsplit(str(data["url"]))
            logger.debug("API redirect -> %s", canonical.path)
            resp = self._get(
                BASE_URL + "/web/1/items" + canonical.path,
                params=dict(parse_qsl(canonical.query)) or None,
            )
            data = self._json_or_blocked(resp)

        raw_items = ((data.get("catalog") or {}).get("items")) or []
        listings = []
        for d in raw_items:
            item = self._item_from_api(d)
            if item is not None:
                listings.append(item)
        return listings

    @staticmethod
    def _json_or_blocked(resp: cf_requests.Response) -> dict:
        """
        Разбирает JSON-ответ внутреннего API и распознаёт программные
        блокировки, которые приходят с кодом 200 ({"too-many-requests": ...}).
        Подсказки редиректа (status.code=301) считаются нормальным ответом.
        """
        try:
            data = resp.json()
        except ValueError as exc:
            raise AvitoBlockedError(f"API вернул не-JSON ответ (url={resp.url})") from exc

        if not isinstance(data, dict):
            return {}

        status_code = (data.get("status") or {}).get("code")
        if "too-many-requests" in data or status_code in (403, 429, 503):
            raise AvitoBlockedError(f"API заблокирован (status={status_code}): {data}")

        return data

    @staticmethod
    def _item_from_api(d: dict) -> Optional[Listing]:
        """Преобразует элемент catalog.items из JSON-API в Listing."""
        try:
            price_raw = None
            price_str = (d.get("priceDetailed") or {}).get("string") or ""
            digits = "".join(ch for ch in price_str if ch.isdigit())
            if digits:
                price_raw = int(digits)

            location = ((d.get("geo") or {}).get("formattedAddress")) or (
                (d.get("addressDetailed") or {}).get("locationName")
            ) or None

            image_url = None
            images = d.get("images") or []
            if images and isinstance(images[0], dict):
                image_url = images[0].get("636x636") or next(iter(images[0].values()), None)

            ts = d.get("sortTimeStamp")
            date_published = None
            if isinstance(ts, (int, float)):
                date_published = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

            return Listing(
                id=str(d.get("id") or ""),
                title=d.get("title"),
                price=(d.get("priceDetailed") or {}).get("fullString"),
                price_raw=price_raw,
                url=urljoin(BASE_URL, d["urlPath"]) if d.get("urlPath") else None,
                location=location or None,
                date_published=date_published,
                image_url=image_url,
                extra={
                    "sortTimeStamp": ts,
                    "description": d.get("description") or None,
                    "source": "api",
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось разобрать элемент JSON-API")
            return None

    def search(
        self,
        query: Optional[str] = None,
        category_path: str = "rossiya",
        extra_params: Optional[dict] = None,
        max_pages: int = 1,
    ) -> Iterator[Listing]:
        """
        Итерируется по объявлениям поисковой выдачи.

        query: поисковая фраза (?q=...)
        category_path: путь категории/региона, например
            "rossiya" (все объявления по РФ),
            "moskva/kvartiry" и т.п. — берётся из адресной строки Avito.
        extra_params: дополнительные query-параметры (фильтры Avito),
            например {"pmin": "10000", "pmax": "50000"}.
        max_pages: сколько страниц выдачи обойти.
        """
        params = dict(extra_params or {})
        if query:
            params["q"] = query

        for page in range(1, max_pages + 1):
            page_params = dict(params)
            if page > 1:
                page_params["p"] = page

            url = urljoin(BASE_URL + "/", category_path.strip("/") + "/")
            logger.info("Загружаю страницу %s: %s?%s", page, url, urlencode(page_params))

            resp = self._get(url, params=page_params)
            listings = list(self.parse_search_page(resp.text))

            if not listings:
                logger.info("На странице %s объявлений не найдено — останавливаюсь.", page)
                self._dump_debug_html(resp.text, str(resp.url), "no_listings_found")
                break

            for item in listings:
                yield item

            if page < max_pages:
                self._sleep_between_requests()

    def parse_search_page(self, html: str) -> Iterator[Listing]:
        """Парсит HTML страницы выдачи и возвращает объявления."""
        soup = _make_soup(html)

        cards = soup.select('div[data-marker="item"]')
        if not cards:
            # запасной вариант — иногда Avito меняет разметку контейнера
            cards = soup.select('[data-marker^="item"]')

        for card in cards:
            listing = self._parse_card(card)
            if listing:
                yield listing

    def get_listing_details(self, url: str) -> dict:
        """Дополнительно загружает страницу объявления и достаёт JSON-LD, если есть."""
        resp = self._get(url)
        soup = _make_soup(resp.text)

        data = {}
        ld_json_tag = soup.find("script", {"type": "application/ld+json"})
        if ld_json_tag and ld_json_tag.string:
            try:
                data = json.loads(ld_json_tag.string)
            except json.JSONDecodeError:
                logger.debug("Не удалось распарсить JSON-LD на странице %s", url)

        description_tag = soup.select_one('[data-marker="item-view/item-description"]')
        if description_tag:
            data["description_text"] = description_tag.get_text(" ", strip=True)

        return data

    # ------------------------------------------------------------------ #
    # Парсинг карточки
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_card(card) -> Optional[Listing]:
        try:
            item_id = card.get("data-item-id") or card.get("id")

            title_tag = card.select_one('[itemprop="name"]') or card.select_one(
                'a[data-marker="item-title"], h3[itemprop="name"]'
            )
            title = title_tag.get_text(strip=True) if title_tag else None

            link_tag = card.select_one('a[data-marker="item-title"], a[itemprop="url"]')
            href = link_tag.get("href") if link_tag else None
            url = urljoin(BASE_URL, href) if href else None

            price_tag = card.select_one('[data-marker="item-price"] meta[itemprop="price"]')
            price_raw = None
            if price_tag and price_tag.get("content"):
                try:
                    price_raw = int(price_tag["content"])
                except ValueError:
                    price_raw = None

            price_text_tag = card.select_one('[data-marker="item-price"]')
            price_text = price_text_tag.get_text(" ", strip=True) if price_text_tag else None

            location_tag = card.select_one(
                '[data-marker="item-address"], [data-marker="item-location"]'
            )
            location = location_tag.get_text(" ", strip=True) if location_tag else None

            date_tag = card.select_one('[data-marker="item-date"]')
            date_published = date_tag.get_text(" ", strip=True) if date_tag else None

            seller_tag = card.select_one('[data-marker="seller-info/summary"]')
            seller = seller_tag.get_text(" ", strip=True) if seller_tag else None

            img_tag = card.select_one("img")
            image_url = img_tag.get("src") if img_tag else None

            return Listing(
                id=item_id,
                title=title,
                price=price_text,
                price_raw=price_raw,
                url=url,
                location=location,
                date_published=date_published,
                image_url=image_url,
                seller=seller,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось распарсить карточку объявления")
            return None


# -------------------------------------------------------------------- #
# Вспомогательные функции сохранения
# -------------------------------------------------------------------- #
def save_to_json(listings: Iterable[Listing], path: str | Path) -> None:
    path = Path(path)
    data = [l.to_dict() for l in listings]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Сохранено %s объявлений в %s", len(data), path)


def save_to_csv(listings: Iterable[Listing], path: str | Path) -> None:
    path = Path(path)
    listings = list(listings)
    if not listings:
        logger.warning("Нет данных для сохранения в CSV")
        return

    fieldnames = list(listings[0].to_dict().keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in listings:
            row = item.to_dict()
            row.pop("extra", None)
            writer.writerow(row)
    logger.info("Сохранено %s объявлений в %s", len(listings), path)
