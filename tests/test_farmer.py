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
    CheckinManager
)
from src.coins import CoinsFarmManager
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
        self.assertEqual(params["api"], "mtop.aliexpress.growth.coins.checkin")

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


if __name__ == "__main__":
    unittest.main()

