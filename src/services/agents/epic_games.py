# -*- coding: utf-8 -*-
# Time       : 2023/8/14 23:16
# Author     : QIN2DIM
# Github     : https://github.com/QIN2DIM
# Description:
from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import List, Dict

import httpx
from loguru import logger
from playwright.sync_api import BrowserContext, expect
from playwright.sync_api import Page

from services.agents._hcaptcha_solver import Status, is_fall_in_captcha, Radagon
from services.models import EpicPlayer
from utils.toolbox import from_dict_to_model

# fmt:off
URL_CLAIM = "https://store.epicgames.com/en-US/free-games"
URL_LOGIN = f"https://www.epicgames.com/id/login?lang=en-US&noHostRedirect=true&redirectUrl={URL_CLAIM}"
URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
URL_PRODUCT_PAGE = "https://store.epicgames.com/en-US/p/"
URL_ORDER_HISTORY = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
URL_CART = "https://store.epicgames.com/en-US/cart"
URL_CART_SUCCESS = "https://store.epicgames.com/en-US/cart/success"
# -----
URL_STORE_EXPLORER = "https://store.epicgames.com/en-US/browse?sortBy=releaseDate&sortDir=DESC&priceTier=tierFree&count=40"
URL_STORE_EXPLORER_GRAPHQL = (
    "https://store.epicgames.com/graphql?operationName=searchStoreQuery"
    '&variables={"category":"games/edition/base","comingSoon":false,"count":80,"freeGame":true,"keywords":"","sortBy":"releaseDate","sortDir":"DESC","start":0,"tag":"","withPrice":true}'
    '&extensions={"persistedQuery":{"version":1,"sha256Hash":"13a2b6787f1a20d05c75c54c78b1b8ac7c8bf4efc394edf7a5998fdf35d1adb0"}}'
)
# fmt:on


@dataclass
class CompletedOrder:
    offerId: str
    namespace: str


@dataclass
class Game:
    url: str
    namespace: str
    title: str
    thumbnail: str
    in_library = None


def get_promotions() -> List[Game]:
    """
    获取周免游戏数据

    <即将推出> promotion["promotions"]["upcomingPromotionalOffers"]
    <本周免费> promotion["promotions"]["promotionalOffers"]
    :return: {"pageLink1": "pageTitle1", "pageLink2": "pageTitle2", ...}
    """
    _promotions: List[Game] = []

    params = {"local": "zh-CN"}
    resp = httpx.get(URL_PROMOTIONS, params=params)
    try:
        data = resp.json()
    except JSONDecodeError:
        pass
    else:
        elements = data["data"]["Catalog"]["searchStore"]["elements"]
        promotions = [e for e in elements if e.get("promotions")]
        # 获取商城促销数据&&获取<本周免费>的游戏对象
        for promotion in promotions:
            if offer := promotion["promotions"]["promotionalOffers"]:
                # 去除打折了但只打一点点的商品
                with suppress(KeyError, IndexError):
                    offer = offer[0]["promotionalOffers"][0]
                    if offer["discountSetting"]["discountPercentage"] != 0:
                        continue
                try:
                    query = promotion["catalogNs"]["mappings"][0]["pageSlug"]
                    promotion["url"] = f"{URL_PRODUCT_PAGE}{query}"
                except IndexError:
                    promotion["url"] = f"{URL_PRODUCT_PAGE}{promotion['productSlug']}"

                promotion["thumbnail"] = promotion["keyImages"][-1]["url"]

                _promotions.append(from_dict_to_model(Game, promotion))

    return _promotions


def get_order_history(
        cookies: Dict[str, str], page: str | None = None, last_create_at: str | None = None
) -> List[CompletedOrder]:
    """获取最近的订单纪录"""

    def request_history() -> str | None:
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                          " Chrome/115.0.0.0 Safari/537.36 Edg/115.0.1901.203"
        }
        params = {"locale": "zh-CN", "page": page or "0", "latCreateAt": last_create_at or ""}
        resp = httpx.get(URL_ORDER_HISTORY, headers=headers, cookies=cookies, params=params)
        if resp.status_code != 200:
            raise httpx.RequestError("Failed to get order history, cookie may have expired")
        return resp.text

    completed_orders: List[CompletedOrder] = []

    try:
        data = json.loads(request_history())
        logger.info("captcha order history", data=data)
        for order in data["orders"]:
            if order["orderType"] != "PURCHASE":
                continue
            for item in order["items"]:
                if len(item["namespace"]) != 32:
                    continue
                completed_orders.append(from_dict_to_model(CompletedOrder, item))
    except (JSONDecodeError, KeyError) as err:
        logger.warning(err)
    return completed_orders


@dataclass
class EpicGames:
    player: EpicPlayer
    """
    Agent control
    """

    _radagon: Radagon = None
    """
    Module for anti-captcha
    """

    _promotions: List[Game] = field(default_factory=list)
    """
    Free promotional items for the week, 
    considered metadata for task sequence of the agent
    """

    @classmethod
    def from_player(cls, player: EpicPlayer):
        return cls(player=player)

    @property
    def radagon(self) -> Radagon:
        self._radagon = self._radagon or Radagon()
        return self._radagon

    @property
    def promotions(self) -> List[Game]:
        self._promotions = self._promotions or get_promotions()
        return self._promotions

    def _login(self, page: Page) -> str | None:
        page.goto(URL_CLAIM)
        while page.locator('a[role="button"]:has-text("Sign In")').count() > 0:
            logger.info("login", mode="game")
            page.goto(URL_LOGIN, wait_until="domcontentloaded")
            page.click("#login-with-epic")
            page.fill("#email", self.player.email)
            page.type("#password", self.player.password)
            page.click("#sign-in")
            page.wait_for_url(URL_CLAIM)
        return Status.AUTH_SUCCESS

    def authorize(self, context: BrowserContext) -> bool | None:
        page = context.new_page()

        beta = -1
        while beta < 8:
            beta += 1
            result = self._login(page)
            # Assert if you are fall in the hcaptcha challenge
            if result not in [Status.AUTH_SUCCESS]:
                result = is_fall_in_captcha(page)
            # Pass Challenge
            if result == Status.AUTH_SUCCESS:
                return True
            # Exciting moment :>
            if result == Status.AUTH_CHALLENGE:
                resp = self.radagon.anti_hcaptcha(page, window="login")
                if resp == self.radagon.CHALLENGE_SUCCESS:
                    return True
                if resp == self.radagon.CHALLENGE_REFRESH:
                    beta -= 0.5
                elif resp == self.radagon.CHALLENGE_BACKCALL:
                    beta -= 0.75
                elif resp == self.radagon.CHALLENGE_CRASH:
                    beta += 0.5
        logger.critical("Failed to flush token", agent=self.__class__.__name__)

    @staticmethod
    def claim_weekly_games(context: BrowserContext, promotions: List[Game]):
        page = context.new_page()

        # --> Add promotions to Cart
        for promotion in promotions:
            logger.info("go to store", url=promotion.url)
            page.goto(promotion.url, wait_until="domcontentloaded")

            # <-- Handle pre-page
            with suppress(TimeoutError):
                page.click("//button//span[text()='Continue']", timeout=2000)

            # --> Make sure promotion is not in the library before executing
            cta_btn = page.locator("//aside//button[@data-testid='add-to-cart-cta-button']")
            text = cta_btn.text_content()
            if text == "View In Cart":
                continue
            if text == "Add To Cart":
                cta_btn.click()
                expect(cta_btn).to_have_text("View In Cart")

        # --> Goto cart page
        page.goto(URL_CART, wait_until="domcontentloaded")
        page.click("//button//span[text()='Check Out']")

        # <-- Handle Any LICENSE
        with suppress(TimeoutError):
            page.click("//label[@for='agree']", timeout=2000)
            accept = page.locator("//button//span[text()='Accept']")
            if accept.is_enabled():
                accept.click()

        # --> Move to webPurchaseContainer iframe
        wpc = page.frame_locator("//iframe[@class='']")
        wpc.locator("//div[@class='payment-order-confirm']").click()
        logger.info("Move to webPurchaseContainer iframe")
        page.wait_for_url(URL_CART_SUCCESS)
