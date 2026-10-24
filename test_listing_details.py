"""Regression tests for exact prices, account details, rating controls and red buttons."""
import html
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from filters import matches
from keyboards import red_keyboard
from main import Bot, Listing, Market, ServiceError, State, Telegram, exact_price, request_json
from test_bot import raw
from test_settings import PhotoTelegram


def detail(item_id=2, level=4, second=1, price="123.4567", twofa=1, gifts=7, nft=3):
    item = raw(item_id, level, second)
    item["price"] = price
    item["telegram"].update({"two_factor":twofa,"gifts_count":gifts,"unique_gifts_count":nft})
    return item


class DetailTests(unittest.TestCase):
    def test_notification_displays_2fa_and_separate_gift_counts(self):
        message = Listing.parse(detail()).message()
        self.assertIn("2FA: <b>Есть</b>", message)
        self.assertIn("Обычные подарки: <b>7</b>", message)
        self.assertIn("NFT-подарки: <b>3</b>", message)
        no = Listing.parse(detail(twofa=0,gifts=0,nft=0)).message()
        self.assertIn("2FA: <b>Нет</b>", no)
        self.assertIn("Обычные подарки: <b>0</b>", no)
        self.assertIn("NFT-подарки: <b>0</b>", no)

    def test_missing_or_invalid_counts_are_unknown_not_zero(self):
        message = Listing.parse(raw()).message()
        self.assertIn("2FA: <b>Нет данных</b>", message)
        self.assertIn("Обычные подарки: <b>Нет данных</b>", message)
        self.assertIn("NFT-подарки: <b>Нет данных</b>", message)
        for bad in (None, -1, "unknown", True, "2.5"):
            with self.subTest(value=bad):
                message = Listing.parse(detail(gifts=bad, nft=bad)).message()
                self.assertIn("Обычные подарки: <b>Нет данных</b>", message)

    def test_prices_are_exact_without_rounding_or_float_precision_loss(self):
        cases={"123.456789":"123.456789", "1000.0100":"1 000.01", "0.000001":"0.000001", "50000":"50 000"}
        for source, expected in cases.items():
            with self.subTest(price=source):
                self.assertEqual(exact_price(source), expected)
                self.assertIn(expected + " ₽", Listing.parse(detail(price=source)).message())
        response = io.BytesIO(b'{"price":0.1234567890123456789}')
        with patch("main.urlopen", return_value=response):
            value = request_json("https://api.tronaccs.market/example")["price"]
        self.assertIsInstance(value, Decimal)
        self.assertEqual(str(value), "0.1234567890123456789")

    def test_public_card_refreshes_price_and_details(self):
        observed=[]
        def transport(url):
            observed.append(url)
            return 15 if url.endswith("/user/settings/commission") else detail(price="456.789")
        market=Market(transport=transport)
        item=market.enrich(Listing.parse(raw(2,4,1)))
        self.assertTrue(observed[0].endswith("/tg-accounts/2"))
        self.assertEqual(item.price,"456.789")
        self.assertEqual(item.attributes["gifts_count"],7)
        self.assertEqual(item.attributes["unique_gifts_count"],3)
        self.assertFalse(matches(item,{"giftsRegularFrom":8}))
        self.assertTrue(matches(item,{"giftsRegularFrom":7,"giftsNftTo":3}))

    def test_public_commission_total_matches_site_rounding_and_keeps_seller_price(self):
        item=detail(price="575")
        listing=Listing.parse(item)
        listing.attributes["commission_percent"]="15"
        message=listing.message()
        self.assertIn("Цена продавца: <b>575 ₽</b>",message)
        self.assertIn("Цена с публичной комиссией 15%: <b>662 ₽</b>",message)
        listing.attributes["commission_percent"]="0"
        self.assertIn("комиссией 0%: <b>575 ₽</b>",listing.message())

    def test_commission_uses_public_endpoint_and_is_cached(self):
        calls=[]
        market=Market(transport=lambda url:calls.append(url) or 15)
        self.assertEqual(market.commission(),Decimal(15))
        self.assertEqual(market.commission(),Decimal(15))
        self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].endswith("/user/settings/commission"))

    def test_missing_commission_does_not_guess_a_total_or_discard_gifts(self):
        def transport(url):
            if url.endswith("/user/settings/commission"):
                raise ServiceError("Unavailable",503)
            return detail()
        item=Market(transport=transport).enrich(Listing.parse(raw(2,4,1)))
        self.assertEqual(item.attributes["gifts_count"],7)
        self.assertIn("Цена продавца: <b>123.4567 ₽</b>",item.message())
        self.assertNotIn("Цена с публичной комиссией",item.message())

    def test_notification_caption_stays_within_telegram_limit(self):
        item=detail()
        item["title"]="😀"*180
        item["telegram"]["country"]={"name":"🌍"*80}
        import re
        listing=Listing.parse(item)
        listing.attributes["commission_percent"]="15"
        plain=html.unescape(re.sub(r"<[^>]*>","",listing.message()))
        self.assertLessEqual(len(plain.encode("utf-16-le"))//2,1024)


class BotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.state=State(Path(self.temp.name)/"state.sqlite3")
        self.state.activate_admin(42)
        self.tg=PhotoTelegram()
        self.bot=Bot({"poll_seconds":30,"allowed_chat_ids":[]},self.state,None,self.tg)
        self.bot.command({"chat":{"id":42,"type":"private"},"text":"/start"})

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def click(self,data):
        self.bot.callback({"id":"cb","from":{"id":42},"data":data,
                           "message":{"message_id":1,"chat":{"id":42,"type":"private"}}})

    def write(self,text):
        self.bot.command({"chat":{"id":42,"type":"private"},"text":text})

    def test_detection_enriches_only_new_accounts_before_queueing_price_and_counts(self):
        self.state.baseline([Listing.parse(raw(1))])
        urls=[]
        def transport(url):
            urls.append(url)
            if url.endswith("/user/settings/commission"):
                return 15
            if "/tg-accounts/" in url:
                return detail(price="456.789")
            return {"data":[raw(2,4,1),raw(1)],"hasMore":False}
        self.bot.market=Market(transport=transport,page_delay=0)
        self.bot.scan()
        self.bot.scan()
        rows=self.state.pending()
        self.assertEqual(len(rows),1)
        self.assertIn("456.789 ₽",rows[0]["text"])
        self.assertIn("Обычные подарки: <b>7</b>",rows[0]["text"])
        self.assertEqual(sum("/tg-accounts/" in url for url in urls),1)

    def test_unavailable_details_do_not_replace_unknown_gift_counts_with_zero(self):
        self.state.baseline([Listing.parse(raw(1))])
        def transport(url):
            if "/tg-accounts/" in url:
                raise ServiceError("Unavailable",503)
            return {"data":[raw(2,4,1)],"hasMore":False}
        self.bot.market=Market(transport=transport,page_delay=0)
        self.bot.scan()
        self.assertIn("Обычные подарки: <b>Нет данных</b>",self.state.pending()[0]["text"])

    def test_detail_rate_limit_preserves_checkpoint_and_pending_notifications(self):
        self.state.baseline([Listing.parse(raw(1))])
        checkpoint=self.state.get("watermark")
        def transport(url):
            if "/tg-accounts/" in url:
                raise ServiceError("Rate limited",429,30)
            return {"data":[raw(2,4,1)],"hasMore":False}
        self.bot.market=Market(transport=transport,page_delay=0)
        with self.assertRaises(ServiceError):self.bot.scan()
        self.assertEqual(self.state.get("watermark"),checkpoint)
        self.assertFalse(self.state.pending())

    def test_rating_bounds_and_site_search_link_preserve_other_filters(self):
        self.state.set_filters(42,{"raitingLevelFrom":2,"priceTo":500,"premium":0})
        self.write("/rating")
        self.click("bound:ratingLevel:from")
        self.write("3")
        self.click("bound:ratingLevel:to")
        self.write("5")
        self.assertEqual(self.state.chat_filters(42),{"raitingLevelFrom":3,"raitingLevelTo":5,"priceTo":500,"premium":0})
        self.click("nav:link")
        buttons=[b for row in self.tg.screens[-1]["keyboard"]["inline_keyboard"] for b in row]
        self.assertTrue(any(b.get("callback_data")=="fld:ratingLevel" for b in buttons))
        query=parse_qs(urlsplit(next(b["url"] for b in buttons if "url" in b)).query)
        self.assertEqual(query["raitingLevelFrom"],["3"])
        self.assertEqual(query["raitingLevelTo"],["5"])
        self.assertEqual(query["priceTo"],["500"])
        self.click("bound:ratingLevel:from")
        self.write("-")
        self.assertNotIn("raitingLevelFrom",self.state.chat_filters(42))
        self.assertEqual(self.state.chat_filters(42)["raitingLevelTo"],5)

    def test_invalid_rating_bound_does_not_overwrite_search(self):
        self.state.set_filters(42,{"raitingLevelFrom":2,"raitingLevelTo":5})
        self.click("bound:ratingLevel:from")
        self.write("7")
        self.assertEqual(self.state.chat_filters(42),{"raitingLevelFrom":2,"raitingLevelTo":5})
        self.assertEqual(self.state.ui_get(42)["pending"]["mode"],"bound_from")

    def test_all_menu_and_notification_buttons_are_red_without_new_icons(self):
        for data in ("nav:home","nav:filters","nav:link","fld:ratingLevel","nav:status","nav:help"):
            self.click(data)
        for screen in self.tg.screens:
            for row in screen["keyboard"]["inline_keyboard"]:
                for b in row:
                    self.assertEqual(b["style"],"danger")
                    self.assertNotIn("🟡",b["text"])
                    self.assertNotIn("🟨",b["text"])
        keyboard=Listing.parse(detail()).keyboard()
        self.assertTrue(all(b["style"]=="danger" for row in keyboard["inline_keyboard"] for b in row))
        original={"inline_keyboard":[[{"text":"Открыть","url":"https://example.com"}]]}
        changed=red_keyboard(original)
        self.assertEqual(changed["inline_keyboard"][0][0]["text"],"Открыть")
        self.assertNotIn("style",original["inline_keyboard"][0][0])

    def test_saved_old_keyboard_is_colored_when_sent_to_telegram(self):
        captured=[]
        def transport(url,payload,**kwargs):
            captured.append(payload)
            return {"ok":True,"result":{"message_id":1}}
        Telegram("fake",transport=transport).send(42,"Old queue",{"inline_keyboard":[[{"text":"Открыть","url":"https://example.com"}]]})
        self.assertEqual(captured[0]["reply_markup"]["inline_keyboard"][0][0]["style"],"danger")


if __name__=="__main__":unittest.main()
