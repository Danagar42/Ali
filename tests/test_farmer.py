import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import requests

from src.config import (
    BROWSER_PROFILES,
    get_random_browser_profile,
    get_client_hints_for_ua,
    get_random_user_agent
)
from src.auth import (
    parse_cookie_string,
    _cookie_fingerprint,
    _natural_sort_key,
    load_accounts
)
from src.client import AliClient
from src.checkin import (
    generate_mtop_sign,
    is_checkin_success,
    is_already_checked_in,
    is_captcha_required,
    CheckinManager
)
from src.coins import CoinsFarmManager, REPORT_TASK_URLS
from src.coupons import CouponCollector
import src.main as main_module


class TestAuth(unittest.TestCase):
    def test_parse_cookie_string_standard(self):
        cookie_str = "aep_usuc_f=region=UA; xman_us_f=x_l=0; _m_h5_tk=abcdef_123"
        parsed = parse_cookie_string(cookie_str)
        self.assertEqual(parsed["aep_usuc_f"], "region=UA")
        self.assertEqual(parsed["xman_us_f"], "x_l=0")
        self.assertEqual(parsed["_m_h5_tk"], "abcdef_123")

    def test_parse_cookie_string_with_prefix_and_whitespace(self):
        cookie_str = "  Cookie:  foo=bar;   baz=qux; ; =invalid; empty=   "
        parsed = parse_cookie_string(cookie_str)
        self.assertEqual(parsed["foo"], "bar")
        self.assertEqual(parsed["baz"], "qux")
        self.assertEqual(parsed["empty"], "")
        self.assertNotIn("", parsed)

    def test_parse_cookie_string_empty(self):
        self.assertEqual(parse_cookie_string(""), {})
        self.assertEqual(parse_cookie_string("   "), {})

    def test_natural_sort_key(self):
        keys = ["ALI_COOKIES_10", "ALI_COOKIES_1", "ALI_COOKIES_2", "ALI_COOKIES_20"]
        sorted_keys = sorted(keys, key=_natural_sort_key)
        self.assertEqual(sorted_keys, ["ALI_COOKIES_1", "ALI_COOKIES_2", "ALI_COOKIES_10", "ALI_COOKIES_20"])

    def test_cookie_fingerprint(self):
        c1 = {"A": "1", "b": "2"}
        c2 = {"b": "2", "a": "1"}
        self.assertEqual(_cookie_fingerprint(c1), _cookie_fingerprint(c2))

    @patch.dict(os.environ, {
        "ALI_COOKIES": "token=primary",
        "ALI_COOKIES_1": "token=account1",
        "ALI_COOKIES_2": "token=account2",
        "ALI_COOKIES_10": "token=account10",
        "ALI_COOKIES_DUP": "token=account1",  # duplicate of account 1
        "ALI_COOKIES_EMPTY": "",
    }, clear=True)
    def test_load_accounts_sorting_and_deduplication(self):
        accounts = load_accounts()
        account_names = [name for name, _ in accounts]

        # Primary comes first
        self.assertEqual(account_names[0], "Account_Primary")
        # Followed by 1, 2, 10 in natural sort order
        self.assertEqual(account_names[1:], ["Account_1", "Account_2", "Account_10"])
        # Dup and empty should not be present
        self.assertNotIn("Account_DUP", account_names)
        self.assertNotIn("Account_EMPTY", account_names)


class TestConfigAndClient(unittest.TestCase):
    def test_browser_profiles_integrity(self):
        self.assertGreater(len(BROWSER_PROFILES), 3)
        for profile in BROWSER_PROFILES:
            self.assertIn("user_agent", profile)
            self.assertIn("client_hints", profile)

    def test_client_hints_firefox_safari_none(self):
        firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"
        hints = get_client_hints_for_ua(firefox_ua)
        self.assertEqual(hints, {})

        safari_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15"
        hints = get_client_hints_for_ua(safari_ua)
        self.assertEqual(hints, {})

    def test_client_hints_chrome_windows_match(self):
        chrome_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36"
        hints = get_client_hints_for_ua(chrome_ua)
        self.assertIn("sec-ch-ua", hints)
        self.assertEqual(hints.get("sec-ch-ua-platform"), '"Windows"')
        self.assertEqual(hints.get("sec-ch-ua-mobile"), "?0")

    def test_client_init_headers(self):
        firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0"
        client = AliClient(cookies={"sid": "test_sid"}, user_agent=firefox_ua)
        self.assertEqual(client.session.headers["User-Agent"], firefox_ua)
        self.assertNotIn("sec-ch-ua", client.session.headers)
        self.assertEqual(client.get_cookie("sid"), "test_sid")


class TestCheckin(unittest.TestCase):
    def test_is_checkin_success_safety(self):
        # Empty ret list (must NOT raise IndexError)
        self.assertFalse(is_checkin_success({"ret": []}))
        # None ret (must NOT raise TypeError)
        self.assertFalse(is_checkin_success({"ret": None}))
        # Standard ret success
        self.assertTrue(is_checkin_success({"ret": ["SUCCESS::success"]}))
        # Boolean success flag
        self.assertTrue(is_checkin_success({"success": True}))
        # Nested result success
        self.assertTrue(is_checkin_success({"result": {"success": True}}))
        # Arbitrary dictionary without success
        self.assertFalse(is_checkin_success({"code": 500, "message": "error"}))
        # Non-dict values
        self.assertFalse(is_checkin_success(None))
        self.assertFalse(is_checkin_success("string response"))

    def test_is_already_checked_in(self):
        self.assertTrue(is_already_checked_in({"ret": ["FAIL_SYS_ALREADY_CHECKED_IN"]}))
        self.assertTrue(is_already_checked_in({"msg": "user has checked in today"}))
        self.assertTrue(is_already_checked_in({"result": "SUCCESS_REPEAT_CHECKIN"}))
        self.assertFalse(is_already_checked_in({"ret": ["SUCCESS"]}))
        self.assertFalse(is_already_checked_in(None))

    def test_generate_mtop_sign(self):
        sign = generate_mtop_sign("token123", "1728900000000", "12574478", "{}")
        self.assertEqual(len(sign), 32)  # valid md5 hex length

    @patch("src.client.AliClient.sleep_random")
    def test_checkin_run_no_false_positive_on_html(self, mock_sleep):
        client = AliClient(cookies={"aep_usuc_f": "test"})
        manager = CheckinManager(client)

        # Mock responses: HTML page with "success" in text should NOT succeed
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_resp.text = '<div class="alert alert-success hide">Failed to check in</div>'

        with patch.object(client, "get", return_value=mock_resp), \
             patch.object(client, "post", return_value=mock_resp):
            success = manager.run_checkin()
            self.assertFalse(success)

    @patch("src.client.AliClient.sleep_random")
    def test_checkin_run_success(self, mock_sleep):
        client = AliClient(cookies={"_m_h5_tk": "mytoken_1234567"})
        manager = CheckinManager(client)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ret": ["SUCCESS::ok"], "data": {"coinCount": 10}}

        with patch.object(client, "get", return_value=mock_resp), \
             patch.object(client, "post", return_value=mock_resp):
            success = manager.run_checkin()
            self.assertTrue(success)


class TestCoinsAndCoupons(unittest.TestCase):
    @patch("src.client.AliClient.sleep_random")
    def test_coins_farm_browse_tasks(self, mock_sleep):
        client = AliClient(cookies={"sid": "test"})
        manager = CoinsFarmManager(client)

        mock_task_resp = MagicMock()
        mock_task_resp.status_code = 200
        mock_task_resp.json.return_value = {
            "data": {
                "taskList": [
                    {"taskId": "TASK_1", "taskType": "BROWSE_ITEM", "completed": False}
                ]
            }
        }

        mock_report_resp = MagicMock()
        mock_report_resp.status_code = 200
        mock_report_resp.json.return_value = {"success": True}

        with patch.object(client, "get", return_value=mock_task_resp), \
             patch.object(client, "post", return_value=mock_report_resp) as mock_post:
            manager.farm_browse_tasks(count=2)
            self.assertTrue(mock_post.called)
            called_payload = mock_post.call_args[1]["json"]
            self.assertEqual(called_payload["channel"], "COIN")
            self.assertIn("taskId", called_payload)

    @patch("src.client.AliClient.sleep_random")
    def test_coupon_collector_with_extracted_candidates(self, mock_sleep):
        client = AliClient(cookies={"sid": "test"})
        collector = CouponCollector(client)

        mock_html = '<html><div data-id="1" couponId="CPN_999" activityId="ACT_888"></div></html>'
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = mock_html

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {"ret": ["SUCCESS"]}

        with patch.object(client, "get", return_value=mock_get_resp), \
             patch.object(client, "post", return_value=mock_post_resp) as mock_post:
            collector.collect_available_coupons()
            self.assertTrue(mock_post.called)


class TestMainErrorHandling(unittest.TestCase):
    @patch("src.main.initial_random_jitter")
    @patch("time.sleep")
    @patch.dict(os.environ, {
        "ALI_COOKIES_1": "token=valid1",
        "ALI_COOKIES_2": "token=valid2",
        "SKIP_RANDOM_SLEEP": "1"
    }, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    @patch("src.client.AliClient.sleep_random")
    def test_main_loop_isolation_one_failing_does_not_stop_next(
        self, mock_sleep_random, mock_coupons, mock_coins, mock_checkin, mock_time_sleep, mock_jitter
    ):
        # Account 1 raises an exception in CheckinManager
        def side_effect(client):
            instance = MagicMock()
            if client.account_name == "Account_1":
                instance.run_checkin.side_effect = RuntimeError("Account 1 failed connection")
            else:
                instance.run_checkin.return_value = True
            return instance

        mock_checkin.side_effect = side_effect

        with patch("sys.exit") as mock_exit:
            main_module.main()
            # main() should NOT exit with error because Account_2 succeeded!
            mock_exit.assert_not_called()

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {}, clear=True)
    def test_main_exits_when_no_accounts(self, mock_jitter):
        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    def test_main_exits_when_all_accounts_fail(self, mock_checkin, mock_jitter):
        instance = MagicMock()
        instance.run_checkin.side_effect = RuntimeError("Fatal error on single account")
        mock_checkin.return_value = instance

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)

    def test_mtop_params_generation(self):
        client_with_token = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client_with_token)
        params = mgr._prepare_mtop_params()
        self.assertIsNotNone(params)
        self.assertEqual(params["appKey"], "12574478")
        self.assertEqual(len(params["sign"]), 32)
        self.assertEqual(params["api"], "mtop.aliexpress.coin.pc.sign.list")
        self.assertEqual(params["jsv"], "2.7.5")
        self.assertEqual(params["asac"], "2E227052O27XFLCLMIUIOA")

        client_without_token = AliClient(cookies={"other_cookie": "val"})
        mgr_no_token = CheckinManager(client_without_token)
        self.assertIsNone(mgr_no_token._prepare_mtop_params())

        client_empty_token = AliClient(cookies={"_m_h5_tk": "_1720000000"})
        mgr_empty_token = CheckinManager(client_empty_token)
        self.assertIsNone(mgr_empty_token._prepare_mtop_params())

    def test_parse_cookie_edge_cases(self):
        cookie_str = "key1=val=with=equals; key2=; ; =val_no_key; key3"
        parsed = parse_cookie_string(cookie_str)
        self.assertEqual(parsed["key1"], "val=with=equals")
        self.assertEqual(parsed["key2"], "")
        self.assertNotIn("key3", parsed)
        self.assertNotIn("", parsed)

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    def test_main_exits_when_zero_successful_actions(self, mock_coupons, mock_coins, mock_checkin, mock_jitter):
        instance_checkin = MagicMock()
        instance_checkin.run_checkin.return_value = False  # Checkin failed
        mock_checkin.return_value = instance_checkin

        instance_coins = MagicMock()
        instance_coins.farm_browse_tasks.return_value = 0  # 0 tasks done
        mock_coins.return_value = instance_coins

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)

    def test_mtop_business_failure_not_treated_as_success(self):
        # MTOP gateway says SUCCESS, but business data says success: False
        mtop_biz_failure = {
            "ret": ["SUCCESS::接口调用成功"],
            "data": {"success": False, "errorMsg": "Risk control rejected"}
        }
        self.assertFalse(is_checkin_success(mtop_biz_failure))

        top_level_failure = {
            "ret": ["SUCCESS::接口调用成功"],
            "success": False
        }
        self.assertFalse(is_checkin_success(top_level_failure))

    def test_repeat_checkin_not_treated_as_fresh_success(self):
        repeat_resp = {
            "ret": ["SUCCESS_REPEAT_CHECKIN::repeated"],
            "data": {}
        }
        self.assertFalse(is_checkin_success(repeat_resp))
        self.assertTrue(is_already_checked_in(repeat_resp))

    def test_is_session_expired(self):
        from src.checkin import is_session_expired
        self.assertTrue(is_session_expired({"ret": ["FAIL_SYS_USER_NOT_LOGIN::Session expired"]}))
        self.assertTrue(is_session_expired("https://login.aliexpress.com/login.htm"))
        self.assertFalse(is_session_expired({"ret": ["SUCCESS::ok"], "data": {"coinCount": 5}}))

    def test_auth_json_multi_account_support(self):
        import json
        json_accounts = {
            "First": "aep_usuc_f=region=UA; xman_us_f=1",
            "Second": "aep_usuc_f=region=US; xman_us_f=2"
        }
        with patch.dict(os.environ, {"ALI_COOKIES_JSON": json.dumps(json_accounts)}, clear=True):
            accounts = load_accounts()
            self.assertEqual(len(accounts), 2)
            names = [a[0] for a in accounts]
            self.assertIn("Account_First", names)
            self.assertIn("Account_Second", names)

    def test_auth_cookie_validation(self):
        from src.auth import validate_aliexpress_cookies
        valid = {"xman_us_f": "123", "aep_usuc_f": "UA"}
        invalid = {"foo": "bar", "session_token": "unknown"}
        self.assertTrue(validate_aliexpress_cookies("TestAcc", valid))
        self.assertFalse(validate_aliexpress_cookies("TestAcc", invalid))

    def test_cookie_fingerprint_handles_volatile_and_timestamp(self):
        # Two cookies for same account with different timestamps and volatile tokens
        c1 = {"xman_us_f": "user123", "_m_h5_tk": "tokenabc_1700000000", "isg": "hash1"}
        c2 = {"xman_us_f": "user123", "_m_h5_tk": "tokenabc_1700999999", "isg": "hash2"}
        self.assertEqual(_cookie_fingerprint(c1), _cookie_fingerprint(c2))

    def test_coupon_extractor_unquoted_js(self):
        collector = CouponCollector(AliClient(cookies={}))
        html = """
        <script>
            window.pageData = {
                activityId: 'ACT_UNQUOTED_123',
                couponId: 'CPN_UNQUOTED_456'
            };
        </script>
        """
        candidates = collector._extract_coupon_candidates(html)
        act_ids = [c.get("activityId") for c in candidates if "activityId" in c]
        cpn_ids = [c.get("couponId") for c in candidates if "couponId" in c]
        self.assertIn("ACT_UNQUOTED_123", act_ids)
        self.assertIn("CPN_UNQUOTED_456", cpn_ids)


class TestNotificationsAndStability(unittest.TestCase):
    def test_mtop_token_expired_distinction(self):
        from src.checkin import is_mtop_token_expired, is_session_expired
        mtop_expired_data = {"ret": ["FAIL_SYS_TOKEN_EXPIRED::Session expired"], "data": {}}
        real_expired_data = {"ret": ["FAIL_SYS_USER_NOT_LOGIN::Not logged in"], "data": {}}

        self.assertTrue(is_mtop_token_expired(mtop_expired_data))
        # MTOP token expiration should NOT be classified as general dead account session
        self.assertFalse(is_session_expired(mtop_expired_data))

        self.assertFalse(is_mtop_token_expired(real_expired_data))
        self.assertTrue(is_session_expired(real_expired_data))

    def test_account_config_unpacking_and_proxy(self):
        from src.auth import AccountConfig
        acc = AccountConfig("TestAcc", {"a": "1"}, proxy="http://127.0.0.1:8080")
        name, cookies = acc
        self.assertEqual(name, "TestAcc")
        self.assertEqual(cookies, {"a": "1"})
        self.assertEqual(acc[0], "TestAcc")
        self.assertEqual(acc[1], {"a": "1"})
        self.assertEqual(acc.proxy, "http://127.0.0.1:8080")

    def test_auth_structured_json_with_proxy(self):
        import json
        payload = {
            "Acc_With_Proxy": {
                "cookies": "aep_usuc_f=region=UA; xman_us_f=123",
                "proxy": "http://user:pass@192.168.1.1:8080"
            }
        }
        with patch.dict(os.environ, {"ALI_COOKIES_JSON": json.dumps(payload)}, clear=True):
            accounts = load_accounts()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].name, "Account_Acc_With_Proxy")
            self.assertEqual(accounts[0].cookies["aep_usuc_f"], "region=UA")
            self.assertEqual(accounts[0].proxy, "http://user:pass@192.168.1.1:8080")

    def test_mask_proxy_url(self):
        from src.client import mask_proxy_url
        raw = "http://myuser:secret123@1.2.3.4:9000"
        masked = mask_proxy_url(raw)
        self.assertNotIn("secret123", masked)
        self.assertIn("myuser:***", masked)

    def test_client_proxy_assignment(self):
        client = AliClient(cookies={}, proxy="http://10.0.0.1:3128")
        self.assertEqual(client.session.proxies.get("http"), "http://10.0.0.1:3128")
        self.assertEqual(client.session.proxies.get("https"), "http://10.0.0.1:3128")

    def test_notification_dispatcher_step_summary(self):
        import tempfile
        from src.notifications import NotificationDispatcher, AccountSummary

        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name

        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                dispatcher = NotificationDispatcher()
                dispatcher.add_summary(AccountSummary(
                    name="Account_1",
                    checkin_status="SUCCESS",
                    coins_earned=35,
                    streak_days=5,
                    total_coins=1200,
                    tasks_done=4,
                    coupons_claimed=1,
                    session_valid=True
                ))
                dispatcher.add_summary(AccountSummary(
                    name="Account_2",
                    checkin_status="SESSION_EXPIRED",
                    session_valid=False
                ))

                summary_md = dispatcher.generate_step_summary()
                self.assertIsNotNone(summary_md)
                self.assertIn("AliExpress Farm Summary Report", summary_md)
                self.assertIn("Account_1", summary_md)
                self.assertIn("🔥 5 дн.", summary_md)
                self.assertIn("+35 🪙", summary_md)
                self.assertIn("CRITICAL", summary_md)

                with open(temp_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
                self.assertIn("Account_1", file_content)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_notification_dispatcher_telegram_send(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:FAKE_TOKEN", "TELEGRAM_CHAT_ID": "987654"}):
            dispatcher = NotificationDispatcher()
            dispatcher.add_summary(AccountSummary(
                name="Account_1",
                checkin_status="SUCCESS",
                coins_earned=40,
                streak_days=7
            ))

            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                res = dispatcher.send_telegram_notification()
                self.assertTrue(res)
                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args[1]
                self.assertIn("123456:FAKE_TOKEN", mock_post.call_args[0][0])
                self.assertEqual(call_kwargs["json"]["chat_id"], "987654")
                self.assertIn("Account_1", call_kwargs["json"]["text"])

    def test_checkin_telemetry_extraction(self):
        client = AliClient(cookies={})
        mgr = CheckinManager(client)
        resp_payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "continuousCheckinDays": 12,
                "awardCoins": 25,
                "totalCoins": 4500
            }
        }
        mgr._extract_telemetry(resp_payload)
        self.assertEqual(mgr.streak_days, 12)
        self.assertEqual(mgr.coins_earned, 25)
        self.assertEqual(mgr.total_coins, 4500)


class TestModernCheckinAndRegionalCompatibility(unittest.TestCase):
    def test_client_regional_compatibility_kr(self):
        # 1. Existing cookie with region=UA replaced with region=KR
        client_ua = AliClient(cookies={"aep_usuc_f": "site=glo&region=UA&b_locale=uk_UA"})
        self.assertIn("region=KR", client_ua.get_cookie("aep_usuc_f"))
        self.assertNotIn("region=UA", client_ua.get_cookie("aep_usuc_f"))

        # 2. Existing cookie without region gets region=KR appended
        client_no_region = AliClient(cookies={"aep_usuc_f": "site=glo&c_tp=USD"})
        self.assertIn("region=KR", client_no_region.get_cookie("aep_usuc_f"))

        # 3. No aep_usuc_f cookie initially gets initialized with region=KR
        client_empty = AliClient(cookies={})
        self.assertIn("region=KR", client_empty.get_cookie("aep_usuc_f"))

    @patch("src.client.AliClient.sleep_random")
    def test_checkin_sign_today_true_sets_already_done(self, mock_sleep):
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        mgr = CheckinManager(client)

        sign_list_resp = MagicMock()
        sign_list_resp.status_code = 200
        sign_list_resp.json.return_value = {
            "api": "mtop.aliexpress.coin.pc.sign.list",
            "ret": ["SUCCESS::接口调用成功"],
            "data": {
                "success": True,
                "data": {
                    "signToday": True,
                    "signDays": 7,
                    "breaked": 0
                }
            }
        }

        exec_resp = MagicMock()
        exec_resp.status_code = 200
        exec_resp.json.return_value = {
            "api": "mtop.aliexpress.coin.execute",
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "success": True,
                "data": [{"name": "userCoinsNum", "value": "1540"}]
            }
        }

        def mock_post(url, *args, **kwargs):
            if "mtop.aliexpress.coin.pc.sign.list" in url:
                return sign_list_resp
            if "mtop.aliexpress.coin.execute" in url:
                return exec_resp
            # If mtop.aliexpress.coin.pc.sign is called, fail the test
            if "mtop.aliexpress.coin.pc.sign" in url:
                self.fail("mtop.aliexpress.coin.pc.sign should NOT be called when signToday is True!")
            return MagicMock(status_code=200)

        with patch.object(client, "get", return_value=MagicMock(status_code=200, text="<html></html>")), \
             patch.object(client, "post", side_effect=mock_post):
            res = mgr.run_checkin()
            self.assertTrue(res)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.streak_days, 7)
            self.assertEqual(mgr.total_coins, 1540)

    @patch("src.client.AliClient.sleep_random")
    def test_checkin_sign_today_false_executes_pc_sign(self, mock_sleep):
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        mgr = CheckinManager(client)

        sign_list_resp = MagicMock()
        sign_list_resp.status_code = 200
        sign_list_resp.json.return_value = {
            "api": "mtop.aliexpress.coin.pc.sign.list",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 4
                }
            }
        }

        sign_exec_resp = MagicMock()
        sign_exec_resp.status_code = 200
        sign_exec_resp.json.return_value = {
            "api": "mtop.aliexpress.coin.pc.sign",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 5,
                    "coins": 20
                }
            }
        }

        exec_resp = MagicMock()
        exec_resp.status_code = 200
        exec_resp.json.return_value = {
            "api": "mtop.aliexpress.coin.execute",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": [{"name": "userCoinsNum", "value": "2050"}]
            }
        }

        calls = []

        def mock_post(url, *args, **kwargs):
            if "mtop.aliexpress.coin.pc.sign.list" in url:
                calls.append("list")
                return sign_list_resp
            if "mtop.aliexpress.coin.pc.sign" in url:
                calls.append("sign")
                return sign_exec_resp
            if "mtop.aliexpress.coin.execute" in url:
                calls.append("execute")
                return exec_resp
            return MagicMock(status_code=200)

        with patch.object(client, "get", return_value=MagicMock(status_code=200, text="<html></html>")), \
             patch.object(client, "post", side_effect=mock_post):
            res = mgr.run_checkin()
            self.assertTrue(res)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.streak_days, 5)
            self.assertEqual(mgr.coins_earned, 20)
            self.assertEqual(mgr.total_coins, 2050)
            self.assertEqual(calls, ["list", "sign", "execute"])

    @patch("src.client.AliClient.sleep_random")
    def test_coin_execute_balance_parsing(self, mock_sleep):
        client = AliClient(cookies={"_m_h5_tk": "token_123"})
        mgr = CheckinManager(client)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "success": True,
                "data": [
                    {"name": "userCoinsNum", "value": "8888"},
                    {"name": "valueMoney", "value": "88.88"}
                ]
            }
        }

        with patch.object(client, "post", return_value=resp):
            mgr._fetch_coin_balance()
            self.assertEqual(mgr.total_coins, 8888)

    @patch("src.client.AliClient.sleep_random")
    def test_mtop_self_healing_token_expired(self, mock_sleep):
        client = AliClient(cookies={"_m_h5_tk": "expired_123"})
        mgr = CheckinManager(client)

        expired_resp = MagicMock()
        expired_resp.status_code = 200
        expired_resp.json.return_value = {
            "ret": ["FAIL_SYS_TOKEN_EXPIRED::Session expired"],
            "data": {}
        }

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {
            "ret": ["SUCCESS::ok"],
            "data": {"success": True, "data": {"signToday": True, "signDays": 2}}
        }

        call_count = [0]

        def side_effect(url, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate server returning Set-Cookie on token expired
                client.session.cookies.set("_m_h5_tk", "fresh_token_99999")
                return expired_resp
            return success_resp

        with patch.object(client, "post", side_effect=side_effect):
            res = mgr._call_mtop("mtop.aliexpress.coin.pc.sign.list")
            self.assertIsNotNone(res)
            self.assertEqual(res["ret"], ["SUCCESS::ok"])
            self.assertEqual(call_count[0], 2)

    def test_is_already_checked_in_prevents_false_positives_on_errors(self):
        # 1. Flow limit with "repeat later" should NOT be classified as already checked in
        self.assertFalse(is_already_checked_in({"ret": ["FAIL_SYS_FLOW_LIMIT::Please repeat later"]}))
        # 2. Token expired message containing "already"
        self.assertFalse(is_already_checked_in({"ret": ["FAIL_SYS_TOKEN_EXPIRED::Token already expired"]}))
        # 3. Rate limit
        self.assertFalse(is_already_checked_in({"ret": ["FAIL_SYS_RATE_LIMIT::Rate limit exceeded"]}))

    def test_is_checkin_success_rejects_business_error_code(self):
        # MTOP gateway returns SUCCESS, but data has business errorCode or errorMsg
        biz_err = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"errorCode": "RISK_REJECT", "errorMsg": "Risk control rejected"}
        }
        self.assertFalse(is_checkin_success(biz_err))

        inner_err = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"data": {"errorCode": "ILLEGAL_ARGUMENT"}}
        }
        self.assertFalse(is_checkin_success(inner_err))

        result_err = {
            "ret": ["SUCCESS::调用成功"],
            "result": {"errorCode": "ERR_BUSY"}
        }
        self.assertFalse(is_checkin_success(result_err))

    def test_client_cookie_domain_deduplication(self):
        client = AliClient(cookies={"aep_usuc_f": "site=glo&region=UA", "_m_h5_tk": "initial_token"})
        # Verify single cookie instance with domain .aliexpress.com
        matching_aep = [c for c in client.session.cookies if c.name == "aep_usuc_f"]
        self.assertEqual(len(matching_aep), 1)
        self.assertEqual(matching_aep[0].domain, ".aliexpress.com")

        # Simulate MTOP server returning new _m_h5_tk with .aliexpress.com domain
        client.session.cookies.set("_m_h5_tk", "fresh_token_123", domain=".aliexpress.com", path="/")
        matching_tk = [c for c in client.session.cookies if c.name == "_m_h5_tk"]
        self.assertEqual(len(matching_tk), 1)
        self.assertEqual(client.get_cookie("_m_h5_tk"), "fresh_token_123")

    def test_balance_parsing_commas_and_floats(self):
        client = AliClient(cookies={"_m_h5_tk": "token_123"})
        mgr = CheckinManager(client)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {
                "success": True,
                "data": [
                    {"name": "userCoinsNum", "value": "1,540"},
                    {"name": "coinCount", "value": "2050.0"}
                ]
            }
        }
        with patch.object(client, "post", return_value=resp):
            mgr._fetch_coin_balance()
            self.assertEqual(mgr.total_coins, 1540)

    def test_sign_list_telemetry_coins_earned_extraction(self):
        client = AliClient(cookies={})
        mgr = CheckinManager(client)
        resp_payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 3,
                    "signList": [
                        {"days": 1, "coins": 10},
                        {"days": 2, "coins": 15},
                        {"days": 3, "coins": 30}
                    ]
                }
            }
        }
        mgr._extract_telemetry(resp_payload)
        self.assertEqual(mgr.streak_days, 3)
        self.assertEqual(mgr.coins_earned, 30)

    def test_coins_dead_endpoints_not_spammed(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        manager = CoinsFarmManager(client)

        html_resp = MagicMock()
        html_resp.status_code = 200
        html_resp.headers = {"content-type": "text/html;charset=utf-8"}
        html_resp.text = "<html>Error</html>"

        with patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(client, "post", return_value=html_resp) as mock_post:
            done = manager.farm_browse_tasks(count=3)
            self.assertEqual(done, 0)
            # Both report URLs should only be attempted once, then disabled (so max 2 POST calls instead of 6)
            self.assertLessEqual(mock_post.call_count, 2)


class TestAuditFixesAndOptimizations(unittest.TestCase):
    def setUp(self):
        CoinsFarmManager.reset_dead_urls()
        CouponCollector.reset_dead_endpoints()

    def tearDown(self):
        CoinsFarmManager.reset_dead_urls()
        CouponCollector.reset_dead_endpoints()

    # --- Task 1: Failure Masking Fix ---

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    def test_main_exits_with_error_when_checkin_fails_even_if_browse_tasks_done(
        self, mock_coupons, mock_coins, mock_checkin, mock_jitter
    ):
        instance_checkin = MagicMock()
        instance_checkin.run_checkin.return_value = False  # Checkin failed
        instance_checkin.session_valid = True
        instance_checkin.status = "FAILED"
        mock_checkin.return_value = instance_checkin

        instance_coins = MagicMock()
        instance_coins.farm_browse_tasks.return_value = 4  # Browsing succeeded
        mock_coins.return_value = instance_coins

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    def test_main_succeeds_when_checkin_ok_and_session_valid(
        self, mock_coupons, mock_coins, mock_checkin, mock_jitter
    ):
        instance_checkin = MagicMock()
        instance_checkin.run_checkin.return_value = True
        instance_checkin.session_valid = True
        instance_checkin.status = "SUCCESS"
        mock_checkin.return_value = instance_checkin

        instance_coins = MagicMock()
        instance_coins.farm_browse_tasks.return_value = 0
        mock_coins.return_value = instance_coins

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_not_called()

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    def test_main_exits_when_checkin_ok_but_session_invalid(
        self, mock_coupons, mock_coins, mock_checkin, mock_jitter
    ):
        instance_checkin = MagicMock()
        instance_checkin.run_checkin.return_value = True
        instance_checkin.session_valid = False
        instance_checkin.status = "SESSION_EXPIRED"
        mock_checkin.return_value = instance_checkin

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)

    def test_notifications_counts_success_only_on_valid_checkin(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Acc1_BrowseOnly",
            checkin_status="FAILED",
            tasks_done=4,
            session_valid=True
        ))
        dispatcher.add_summary(AccountSummary(
            name="Acc2_CheckinOk",
            checkin_status="SUCCESS",
            tasks_done=0,
            session_valid=True
        ))
        dispatcher.add_summary(AccountSummary(
            name="Acc3_AlreadyDone_InvalidSession",
            checkin_status="ALREADY_DONE",
            tasks_done=2,
            session_valid=False
        ))

        # Checkin success should only be 1 (Acc2_CheckinOk)
        success_count = sum(
            1 for s in dispatcher.summaries
            if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
        )
        self.assertEqual(success_count, 1)

    def test_coins_browse_no_fake_completion_on_page_ok(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        manager = CoinsFarmManager(client)

        with patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(manager, "_report_task_complete", return_value=False):
            done = manager.farm_browse_tasks(count=3)
            self.assertEqual(done, 0, "Browsing HTTP 200 must not increment completed count if reporting failed!")

    def test_coins_browse_counts_real_reported_tasks(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        manager = CoinsFarmManager(client)

        reports = [True, False, True]
        with patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(manager, "_report_task_complete", side_effect=reports):
            done = manager.farm_browse_tasks(count=3)
            self.assertEqual(done, 2)

    # --- Task 2: Coin Leakage Fix ---

    def test_coins_earned_not_set_if_checkin_fails(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 5,
                    "signList": [{"days": 5, "coins": 25}, {"days": 6, "coins": 30}]
                }
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return {"ret": ["FAIL_SYS_SERVICE_TIMEOUT::Timeout"]}
                return None
            mock_mtop.side_effect = mock_call
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "FAILED")
            self.assertEqual(mgr.coins_earned, 0, "coins_earned must remain 0 when checkin fails!")

    def test_coins_earned_populated_when_sign_today_true(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": True,
                    "signDays": 7,
                    "signList": [{"days": 7, "coins": 35}]
                }
            }
        }
        with patch.object(mgr, "_call_mtop", return_value=status_res), \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.coins_earned, 35)

    def test_coins_earned_populated_when_pc_sign_succeeds(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 4,
                    "signList": [{"days": 5, "coins": 25}]
                }
            }
        }
        sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 5,
                    "coins": 25
                }
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return sign_res
                return None
            mock_mtop.side_effect = mock_call
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 25)

    # --- Task 3: HTML Escaping for Telegram ---

    def test_telegram_html_escaping(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "test_token"
        dispatcher.telegram_chat_id = "12345"
        dispatcher.add_summary(AccountSummary(
            name="Buyer <VIP & Super>",
            checkin_status="FAILED",
            error_message="Error: <tag> & symbols"
        ))
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            dispatcher.send_telegram_notification()
            self.assertTrue(mock_post.called)
            sent_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("Buyer &lt;VIP &amp; Super&gt;", sent_text)
            self.assertNotIn("Buyer <VIP & Super>", sent_text)
            self.assertIn("Error: &lt;tag&gt; &amp; symbols", sent_text)
            self.assertNotIn("<tag>", sent_text)

    # --- Task 4: Proxy Secrets in Workflow ---

    def test_workflow_contains_proxy_secrets_1_to_10(self):
        workflow_path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "farm.yml")
        self.assertTrue(os.path.exists(workflow_path), f"Workflow file not found at {workflow_path}")
        with open(workflow_path, "r", encoding="utf-8") as f:
            content = f.read()

        for i in range(1, 11):
            expected_key = f"ALI_PROXY_{i}:"
            expected_secret = f"${{{{ secrets.ALI_PROXY_{i} }}}}"
            self.assertIn(expected_key, content, f"Missing {expected_key} in farm.yml")
            self.assertIn(expected_secret, content, f"Missing {expected_secret} in farm.yml")

    # --- Task 5: Dead REST Endpoints Optimization ---

    def test_coins_dead_endpoints_cached_across_instances(self):
        CoinsFarmManager.reset_dead_urls()
        client1 = AliClient(cookies={"sid": "test1"})
        mgr1 = CoinsFarmManager(client1)

        err_resp = MagicMock(status_code=404, text="Not Found")
        with patch.object(client1.session, "get", return_value=err_resp) as mock_get:
            tasks = mgr1.fetch_active_tasks()
            self.assertEqual(tasks, [])
            self.assertGreater(mock_get.call_count, 0)

        # Instance 2 for another account: should bypass dead endpoints immediately
        client2 = AliClient(cookies={"sid": "test2"})
        mgr2 = CoinsFarmManager(client2)
        with patch.object(client2.session, "get") as mock_get2:
            tasks2 = mgr2.fetch_active_tasks()
            self.assertEqual(tasks2, [])
            self.assertEqual(mock_get2.call_count, 0, "Second instance must not query dead URLs again!")

    def test_coupon_collector_skips_when_hub_unavailable(self):
        CouponCollector.reset_dead_endpoints()
        client1 = AliClient(cookies={"sid": "test1"})
        collector1 = CouponCollector(client1)

        resp_404 = MagicMock(status_code=404, text="Not Found")
        with patch.object(client1.session, "get", return_value=resp_404), \
             patch.object(client1.session, "post") as mock_post:
            count = collector1.collect_available_coupons()
            self.assertEqual(count, 0)
            self.assertEqual(mock_post.call_count, 0, "No candidate claims should be sent when coupon hub is 404!")

        # Second account instance should bypass immediately
        client2 = AliClient(cookies={"sid": "test2"})
        collector2 = CouponCollector(client2)
        with patch.object(client2.session, "get") as mock_get2, \
             patch.object(client2.session, "post") as mock_post2:
            count2 = collector2.collect_available_coupons()
            self.assertEqual(count2, 0)
            self.assertEqual(mock_get2.call_count, 0, "Second instance should skip dead hub GET calls!")
            self.assertEqual(mock_post2.call_count, 0)

    # --- Task 6: Network Resilience with Retry Adapter ---

    def test_client_retry_adapter_configuration(self):
        client = AliClient(cookies={})
        for scheme in ("https://", "http://"):
            adapter = client.session.adapters.get(scheme)
            self.assertIsNotNone(adapter, f"Adapter for {scheme} must be mounted!")
            self.assertIsNotNone(adapter.max_retries, f"max_retries for {scheme} must be set!")
            self.assertEqual(adapter.max_retries.total, 3)
            self.assertEqual(adapter.max_retries.backoff_factor, 1.5)
            for status in (500, 502, 503, 504):
                self.assertIn(status, adapter.max_retries.status_forcelist, f"Status {status} must be in forcelist!")

    # --- Task 7: WAF / Captcha Detection ---

    def test_is_captcha_required_detection(self):
        from src.checkin import is_captcha_required
        self.assertTrue(is_captcha_required({"ret": ["RGV587_RUN_SCRIPT::Security verification required"]}))
        self.assertTrue(is_captcha_required({"ret": ["FAIL_SYS_USER_VALIDATE::Need verification"]}))
        self.assertTrue(is_captcha_required("_____tmd_____/punish?x=1"))
        self.assertTrue(is_captcha_required({"data": {"url": "https://sec.aliexpress.com/_____tmd_____/punish"}}))
        self.assertFalse(is_captcha_required({"ret": ["SUCCESS::ok"]}))
        self.assertFalse(is_captcha_required(None))
        self.assertFalse(is_captcha_required({}))
        self.assertFalse(is_captcha_required(""))

    def test_checkin_sets_captcha_required_status(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        captcha_resp = {"ret": ["RGV587_RUN_SCRIPT::Alibaba WAF Block"]}
        with patch.object(mgr, "_call_mtop", return_value=captcha_resp), \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")

    def test_checkin_sets_captcha_required_on_user_validate(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        validate_resp = {"ret": ["FAIL_SYS_USER_VALIDATE::User validation required"]}
        with patch.object(mgr, "_call_mtop", return_value=validate_resp), \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")

    def test_captcha_not_treated_as_session_expired(self):
        from src.checkin import is_session_expired
        captcha_payload = {"ret": ["FAIL_SYS_USER_VALIDATE::User verification needed"]}
        self.assertFalse(is_session_expired(captcha_payload), "Captcha must NOT be classified as SESSION_EXPIRED!")

    def test_captcha_required_notification_badge_and_instructions(self):
        import tempfile
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "token123"
        dispatcher.telegram_chat_id = "chat456"

        summary = AccountSummary(
            name="Buyer_Captcha",
            checkin_status="CAPTCHA_REQUIRED",
            session_valid=True
        )
        dispatcher.add_summary(summary)

        # 1. Icon mapping
        self.assertEqual(dispatcher._checkin_icon("CAPTCHA_REQUIRED"), "🧩 Капча")

        # 2. Telegram message formatting
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            dispatcher.send_telegram_notification()
            sent_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("🧩 <b>Buyer_Captcha</b>: 🧩 Капча", sent_text)
            self.assertIn("Виявлено захист Alibaba WAF!", sent_text)

        # 3. GitHub step summary
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIn("Alibaba WAF Captcha", md)
                self.assertIn("🧩 Капча", md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_is_captcha_required_with_requests_response(self):
        import requests
        from src.checkin import is_captcha_required

        # 403 Forbidden with WAF challenge script
        resp_403 = requests.Response()
        resp_403.status_code = 403
        resp_403._content = b"<html><script>RGV587_RUN_SCRIPT</script></html>"
        self.assertTrue(is_captcha_required(resp_403), "403 Response with RGV587 must be detected as CAPTCHA!")

        # 302 Redirect to punishment slider
        resp_302 = requests.Response()
        resp_302.status_code = 302
        resp_302.url = "https://sec.aliexpress.com/_____tmd_____/punish?x=abc123"
        self.assertTrue(is_captcha_required(resp_302), "302 Response with punish URL must be detected as CAPTCHA!")

        # 200 OK Response with JSON payload
        resp_200 = requests.Response()
        resp_200.status_code = 200
        resp_200._content = b'{"ret": ["FAIL_SYS_USER_VALIDATE::User verification needed"]}'
        self.assertTrue(is_captcha_required(resp_200), "200 Response with FAIL_SYS_USER_VALIDATE must be detected as CAPTCHA!")

        # Clean 200 Response
        resp_clean = requests.Response()
        resp_clean.status_code = 200
        resp_clean._content = b'{"ret": ["SUCCESS::ok"], "data": {"signToday": true}}'
        self.assertFalse(is_captcha_required(resp_clean), "Clean Response must not trigger captcha detection!")

    def test_is_session_expired_with_requests_response_401(self):
        import requests
        from src.checkin import is_session_expired

        resp_401 = requests.Response()
        resp_401.status_code = 401
        resp_401._content = b'{"ret": ["FAIL_SYS_USER_NOT_LOGIN::Not logged in"]}'
        self.assertTrue(is_session_expired(resp_401), "401 Response with FAIL_SYS_USER_NOT_LOGIN must be detected as expired!")

    def test_is_already_checked_in_respects_explicit_sign_today_false(self):
        from src.checkin import is_already_checked_in
        payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signToday": False,
                "msg": "Notice: user already checked in on another promotion"
            }
        }
        self.assertFalse(is_already_checked_in(payload), "signToday=False must never be classified as ALREADY_CHECKED_IN!")

    def test_extract_telemetry_guards_against_sign_today_false(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        payload = {
            "data": {
                "signToday": False,
                "signDays": 3,
                "coins": 50,
                "awardCoins": 50
            }
        }
        # Direct call to _extract_telemetry without extract_coins argument
        mgr._extract_telemetry(payload)
        self.assertEqual(mgr.streak_days, 3)
        self.assertEqual(mgr.coins_earned, 0, "coins_earned must NOT be extracted when signToday is False!")

    def test_checkin_success_fallback_uses_sign_list_not_yesterday_top_level_coins(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 4,
                    "coins": 15,  # yesterday's coins
                    "signList": [{"days": 4, "coins": 15}, {"days": 5, "coins": 35}]
                }
            }
        }
        sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 5
                    # No top-level coins in sign response
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return sign_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.streak_days, 5)
            self.assertEqual(mgr.coins_earned, 35, "Must extract day 5 coins (35) from signList, NOT yesterday's coins (15)!")

    def test_coins_dead_endpoints_cached_on_none_and_504(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        mgr = CoinsFarmManager(client)

        # 1. Connection error returning None
        with patch.object(client, "get", return_value=None):
            tasks = mgr.fetch_active_tasks()
            self.assertEqual(tasks, [])
            self.assertIn("https://coins.aliexpress.com/api/tasks", mgr._dead_urls)
            self.assertIn("https://coins.aliexpress.com/api/task/list", mgr._dead_urls)

        # 2. 504 Gateway Timeout on report
        CoinsFarmManager.reset_dead_urls()
        resp_504 = MagicMock(status_code=504, text="Gateway Timeout")
        with patch.object(client, "post", return_value=resp_504):
            reported = mgr._report_task_complete("BROWSE_ITEM", "task1")
            self.assertFalse(reported)
            self.assertIn("https://coins.aliexpress.com/api/finishTask", mgr._dead_urls)
            self.assertIn("https://coins.aliexpress.com/api/task/finish", mgr._dead_urls)

    def test_coupons_dead_endpoints_cached_on_none_and_504(self):
        CouponCollector.reset_dead_endpoints()
        client = AliClient(cookies={"sid": "test"})
        collector = CouponCollector(client)

        # 1. Hub returning None (network timeout)
        with patch.object(client, "get", return_value=None):
            count = collector.collect_available_coupons()
            self.assertEqual(count, 0)
            self.assertIn("https://www.aliexpress.com/coupon-center.html", collector._dead_endpoints)
            self.assertIn("https://campaign.aliexpress.com/wow/gcp/ae/channel/ae/coupon-center/index", collector._dead_endpoints)

        # 2. Claim endpoint returning 504
        CouponCollector.reset_dead_endpoints()
        resp_hub_ok = MagicMock(status_code=200, text="<html>activityId='test_act'</html>")
        resp_claim_504 = MagicMock(status_code=504, text="Gateway Timeout")
        with patch.object(client, "get", return_value=resp_hub_ok), \
             patch.object(client, "post", return_value=resp_claim_504):
            claimed = collector.collect_available_coupons()
            self.assertEqual(claimed, 0)
            self.assertIn("https://campaign.aliexpress.com/coupon/claim", collector._dead_endpoints)
            self.assertIn("https://coupon.aliexpress.com/buyer/coupon/applyCoupon.htm", collector._dead_endpoints)

    def test_client_retry_adapter_raise_on_status_false(self):
        client = AliClient(cookies={})
        for scheme in ("https://", "http://"):
            adapter = client.session.adapters.get(scheme)
            self.assertFalse(adapter.max_retries.raise_on_status, "raise_on_status must be False so status responses are returned!")

    def test_step_summary_escapes_table_pipes_in_account_name(self):
        import tempfile
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Buyer|With|Pipes",
            checkin_status="SUCCESS",
            session_valid=True
        ))
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIn("Buyer\\|With\\|Pipes", md, "Markdown table pipe delimiter in account name must be escaped!")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_run_checkin_aborts_immediately_on_sign_list_captcha(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    mgr.status = "CAPTCHA_REQUIRED"
                    return None
                if api == "mtop.aliexpress.coin.pc.sign":
                    self.fail("mtop.aliexpress.coin.pc.sign must NOT be called when sign.list triggered CAPTCHA!")
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")

    def test_checkin_success_fallback_advances_streak_and_extracts_day5_coins_when_sign_omits_signdays(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 4,
                    "coins": 15,  # yesterday's coins
                    "signList": [{"days": 4, "coins": 15}, {"days": 5, "coins": 35}]
                }
            }
        }
        # sign_res does not provide signDays or coins
        sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return sign_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.streak_days, 5, "Streak must advance to 5 when sign_res omits signDays!")
            self.assertEqual(mgr.coins_earned, 35, "Must extract Day 5 coins (35) from signList fallback, NOT yesterday's Day 4 coins (15)!")

    def test_extract_coins_from_sign_list_stringified_days(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        mgr.streak_days = 5

        data = {
            "data": {
                "signList": [
                    {"days": "4", "coins": "20"},
                    {"days": "5", "coins": "50"},
                    {"days": "6", "coins": "70"}
                ]
            }
        }
        mgr._extract_coins_from_sign_list(data)
        self.assertEqual(mgr.coins_earned, 50, "Stringified 'days': '5' must match integer streak_days 5!")

        # Also test with 'day' key and float string
        mgr.coins_earned = 0
        mgr.streak_days = 6
        data_float = {
            "data": {
                "signList": [
                    {"day": "6.0", "coins": " 75 "}
                ]
            }
        }
        mgr._extract_coins_from_sign_list(data_float)
        self.assertEqual(mgr.coins_earned, 75, "Stringified 'day': '6.0' with spaces must match integer streak_days 6!")

    def test_run_checkin_aborts_immediately_on_sign_captcha_without_fetching_balance(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 2
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(mgr, "_fetch_coin_balance") as mock_fetch_balance, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    mgr.status = "CAPTCHA_REQUIRED"
                    return None
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
            mock_fetch_balance.assert_not_called()

    def test_is_captcha_required_inspects_response_url_and_aborts_checkin(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        resp = MagicMock()
        resp.text = ""  # empty body
        resp.url = "https://acs.aliexpress.com/_____tmd_____/punish?x=123"
        resp.status_code = 302
        resp.headers = {}

        # Direct verification of is_captcha_required with Response object
        self.assertTrue(is_captcha_required(resp), "Must detect captcha from redirect URL even with empty text!")

        # Verification that run_checkin detects it and aborts
        with patch.object(client, "get", return_value=resp), \
             patch.object(mgr, "_call_mtop") as mock_mtop:
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
            mock_mtop.assert_not_called()

    def test_farm_browse_tasks_skips_catalog_when_all_report_urls_are_dead(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        mgr = CoinsFarmManager(client)

        for u in REPORT_TASK_URLS:
            mgr._dead_urls.add(u)

        with patch.object(client, "get") as mock_get:
            res = mgr.farm_browse_tasks(count=4)
            self.assertEqual(res, 0)
            mock_get.assert_not_called()


    def test_checkin_success_updates_streak_when_sign_res_resets_streak_to_1(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 4,
                    "coins": 15
                }
            }
        }
        # Server resets streak to 1 after missed day or new cycle
        sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 1,
                    "coins": 10
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return sign_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.streak_days, 1, "Explicit signDays: 1 from sign_res must overwrite pre-checkin signDays: 4!")
            self.assertEqual(mgr.coins_earned, 10)

    def test_extract_coins_from_sign_list_accepts_response_object_and_json_str(self):
        import json
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        mgr.streak_days = 3

        # 1. requests.Response object
        resp = MagicMock()
        resp.text = json.dumps({"data": {"signList": [{"days": 3, "coins": 45}]}})
        resp.json.return_value = {"data": {"signList": [{"days": 3, "coins": 45}]}}
        mgr._extract_coins_from_sign_list(resp)
        self.assertEqual(mgr.coins_earned, 45, "Must extract coins from requests.Response object!")

        # 2. JSON string
        mgr.coins_earned = 0
        mgr.streak_days = 4
        json_str = json.dumps({"data": {"signList": [{"days": 4, "coins": 60}]}})
        mgr._extract_coins_from_sign_list(json_str)
        self.assertEqual(mgr.coins_earned, 60, "Must extract coins from JSON string!")

    def test_extract_coins_from_sign_list_supports_alternative_keys(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        # MTOP dayNum and awardCoins
        mgr.streak_days = 5
        data1 = {"data": {"signList": [{"dayNum": 5, "awardCoins": 55}]}}
        mgr._extract_coins_from_sign_list(data1)
        self.assertEqual(mgr.coins_earned, 55, "Must support 'dayNum' and 'awardCoins' keys!")

        # streak and coinCount
        mgr.coins_earned = 0
        mgr.streak_days = 2
        data2 = {"data": {"signList": [{"streak": 2, "coinCount": 20}]}}
        mgr._extract_coins_from_sign_list(data2)
        self.assertEqual(mgr.coins_earned, 20, "Must support 'streak' and 'coinCount' keys!")

        # signDays and coin
        mgr.coins_earned = 0
        mgr.streak_days = 7
        data3 = {"data": {"signList": [{"signDays": 7, "coin": 70}]}}
        mgr._extract_coins_from_sign_list(data3)
        self.assertEqual(mgr.coins_earned, 70, "Must support 'signDays' and 'coin' keys!")

    def test_extract_coins_from_sign_list_cycle_and_is_today_fallback(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        # Day 8 in a 7-day cyclical calendar -> matches Day 1 (coins: 10)
        mgr.streak_days = 8
        data_7day = {
            "data": {
                "signList": [
                    {"days": 1, "coins": 10},
                    {"days": 2, "coins": 15},
                    {"days": 3, "coins": 20},
                    {"days": 4, "coins": 25},
                    {"days": 5, "coins": 30},
                    {"days": 6, "coins": 40},
                    {"days": 7, "coins": 70}
                ]
            }
        }
        mgr._extract_coins_from_sign_list(data_7day)
        self.assertEqual(mgr.coins_earned, 10, "Streak 8 in 7-day calendar must match day 1 cycle reward!")

        # Fallback to isToday: True when day numbers do not align
        mgr.coins_earned = 0
        mgr.streak_days = 99
        data_today = {
            "data": {
                "signList": [
                    {"name": "Step 1", "coins": 5},
                    {"name": "Step 2", "coins": 50, "isToday": True}
                ]
            }
        }
        mgr._extract_coins_from_sign_list(data_today)
        self.assertEqual(mgr.coins_earned, 50, "Must fallback to element with isToday: True!")

    def test_extract_coins_from_sign_list_graceful_on_empty_or_no_match(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)
        mgr.streak_days = 5

        # Empty signList
        mgr._extract_coins_from_sign_list({"data": {"signList": []}})
        self.assertEqual(mgr.coins_earned, 0)

        # None input
        mgr._extract_coins_from_sign_list(None)
        self.assertEqual(mgr.coins_earned, 0)

        # Invalid structure
        mgr._extract_coins_from_sign_list("Not a JSON")
        self.assertEqual(mgr.coins_earned, 0)

    def test_coins_dead_endpoints_handles_405_method_not_allowed(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        mgr = CoinsFarmManager(client)

        resp_405 = MagicMock(status_code=405, text="Method Not Allowed")
        with patch.object(client, "post", return_value=resp_405):
            reported = mgr._report_task_complete("BROWSE_ITEM", "task1")
            self.assertFalse(reported)
            self.assertIn("https://coins.aliexpress.com/api/finishTask", mgr._dead_urls)


if __name__ == "__main__":
    unittest.main()



