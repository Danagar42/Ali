import os
import sys
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import requests

from src.config import (
    BROWSER_PROFILES,
    get_random_browser_profile,
    get_client_hints_for_ua,
    get_random_user_agent,
    ASAC_MOBILE_CAMPAIGN,
    ASAC_TASK_CAMPAIGN,
    PROJECT_APP_KEY,
    TASK_SCENE_ID,
    TASK_DELIVERY_QUERY_API,
    TASK_PROCESS_COMPLETE_API,
    TASK_PROCESS_ISSUE_API,
    TASK_LOCALE,
    TASK_LANG,
    TASK_CURRENCY,
    TASK_TTID,
    TASK_PLATFORM,
    TASK_UMID_TOKEN,
    TASK_BX_UMIDTOKEN,
    TASK_MTOP_APP_KEY
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
    CheckinManager,
    MOBILE_CHECKIN_APIS,
    MOBILE_SIGN_LIST_API,
    MOBILE_SIGN_EXECUTE_API,
    MOBILE_SIGN_QUERY_LIST_API,
    MOBILE_RECEIVE_PRIZE_API,
    _find_mobile_daily_sign_node,
    _extract_prize_coins,
    _extract_node_streak
)
from src.coins import CoinsFarmManager, _extract_coins_from_complete_result
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

    def test_parse_cookie_string_strips_bom_and_zero_width_chars(self):
        cookie_str = "\ufeff_m_h5_tk=token123_456; \ufeffaep_usuc_f=region=KR\u200b; foo\u200c=bar"
        parsed = parse_cookie_string(cookie_str)
        self.assertIn("_m_h5_tk", parsed)
        self.assertNotIn("\ufeff_m_h5_tk", parsed)
        self.assertEqual(parsed["_m_h5_tk"], "token123_456")
        self.assertEqual(parsed["aep_usuc_f"], "region=KR")
        self.assertEqual(parsed["foo"], "bar")

    def test_ali_client_strips_bom_from_cookie_dict_and_string(self):
        client = AliClient(cookies="\ufeff_m_h5_tk=token123_456; \ufeffaep_usuc_f=region=KR")
        names = [c.name for c in client.session.cookies]
        self.assertIn("_m_h5_tk", names)
        self.assertNotIn("\ufeff_m_h5_tk", names)
        req = requests.Request("GET", "https://aliexpress.com", cookies=client.session.cookies)
        prep = client.session.prepare_request(req)
        cookie_header = prep.headers.get("Cookie", "")
        encoded = cookie_header.encode("latin-1")
        self.assertIn(b"_m_h5_tk=token123_456", encoded)

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
        # Bare ret success without business payload is not verified business success
        self.assertFalse(is_checkin_success({"ret": ["SUCCESS::success"]}))
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


class TestCoinsTasks(unittest.TestCase):
    @patch("src.client.AliClient.sleep_random")
    def test_coins_farm_browse_tasks(self, mock_sleep):
        client = AliClient(cookies={"sid": "test"})
        checkin_mgr = CheckinManager(client)
        manager = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        mock_task = {
            "taskId": "TASK_1",
            "taskTitle": "Browse Items",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/item/1.html",
            "status": "todo",
            "staySeconds": 15
        }

        with patch.object(manager, "fetch_active_tasks", return_value=[mock_task]), \
             patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(checkin_mgr, "_call_mtop", return_value={"ret": ["SUCCESS::ok"], "data": {"result": {"completeSuccess": True, "instanceStatus": 2, "awardAmount": 5}}}) as mock_mtop:
            done = manager.farm_browse_tasks(count=2)
            self.assertEqual(done, 1)
            self.assertTrue(mock_mtop.called)


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
    @patch("src.client.AliClient.sleep_random")
    def test_main_loop_isolation_one_failing_does_not_stop_next(
        self, mock_sleep_random, mock_coins, mock_checkin, mock_time_sleep, mock_jitter
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
    def test_main_exits_when_zero_successful_actions(self, mock_coins, mock_checkin, mock_jitter):
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
                self.assertIn("CAUTION", summary_md)

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

    def tearDown(self):
        CoinsFarmManager.reset_dead_urls()

    # --- Task 1: Failure Masking Fix ---

    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    def test_main_exits_with_error_when_checkin_fails_even_if_browse_tasks_done(
        self, mock_coins, mock_checkin, mock_jitter
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
    def test_main_succeeds_when_checkin_ok_and_session_valid(
        self, mock_coins, mock_checkin, mock_jitter
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
    def test_main_exits_when_checkin_ok_but_session_invalid(
        self, mock_coins, mock_checkin, mock_jitter
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
        checkin_mgr = CheckinManager(client)
        manager = CoinsFarmManager(client, checkin_mgr=checkin_mgr)
        task = {
            "taskId": "task1",
            "taskTitle": "Browse Items",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/item/1005001.html",
            "status": "todo",
            "staySeconds": 15,
        }

        with patch.object(client, "sleep_random"), \
             patch.object(manager, "fetch_active_tasks", return_value=[task]), \
             patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(manager, "_complete_task", return_value=False):
            done = manager.farm_browse_tasks(count=3)
            self.assertEqual(done, 0, "Browsing HTTP 200 must not increment completed count if completion failed!")

    def test_coins_browse_counts_real_reported_tasks(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        checkin_mgr = CheckinManager(client)
        manager = CoinsFarmManager(client, checkin_mgr=checkin_mgr)
        tasks = [
            {
                "taskId": f"task_{i}",
                "taskTitle": f"Browse Items {i}",
                "taskType": 0,
                "actionUrl": f"https://aliexpress.com/item/{i}.html",
                "status": "todo",
                "staySeconds": 15,
            }
            for i in range(3)
        ]

        reports = [True, False, True]
        def fake_complete(task):
            res = reports.pop(0)
            if res:
                task["completeSuccess"] = True
            return res

        with patch.object(client, "sleep_random"), \
             patch.object(manager, "fetch_active_tasks", return_value=tasks), \
             patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(manager, "_complete_task", side_effect=fake_complete):
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

    def test_workflow_uses_actions_cache_for_state_persistence(self):
        workflow_path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "farm.yml")
        self.assertTrue(os.path.exists(workflow_path), f"Workflow file not found at {workflow_path}")
        with open(workflow_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertTrue(
            "actions/cache@v4" in content or ("actions/cache/restore@v4" in content and "actions/cache/save@v4" in content),
            "Workflow must use actions/cache for state persistence"
        )
        self.assertIn("path: .state", content)
        self.assertIn("key: farmer-history-${{ github.run_id }}", content)
        self.assertIn("farmer-history-", content)
        self.assertNotIn("actions/download-artifact", content)
        self.assertNotIn("actions/upload-artifact", content)

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

        # 2. 504 Gateway Timeout on task query
        CoinsFarmManager.reset_dead_urls()
        resp_504 = MagicMock(status_code=504, text="Gateway Timeout")
        with patch.object(client, "get", return_value=resp_504):
            tasks = mgr.fetch_active_tasks()
            self.assertEqual(tasks, [])
            self.assertIn("https://coins.aliexpress.com/api/tasks", mgr._dead_urls)
            self.assertIn("https://coins.aliexpress.com/api/task/list", mgr._dead_urls)
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

    def test_farm_browse_tasks_skips_when_no_active_tasks(self):
        CoinsFarmManager.reset_dead_urls()
        client = AliClient(cookies={"sid": "test"})
        mgr = CoinsFarmManager(client)

        with patch.object(mgr, "fetch_active_tasks", return_value=[]), \
             patch.object(client, "get") as mock_get:
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
        with patch.object(client, "get", return_value=resp_405):
            tasks = mgr.fetch_active_tasks()
            self.assertEqual(tasks, [])
            self.assertIn("https://coins.aliexpress.com/api/tasks", mgr._dead_urls)


class TestDualChannelCheckin(unittest.TestCase):
    def test_mobile_checkin_success_and_streak_coins_extraction(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_mobile_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": True,
                    "signDays": 3,
                    "signList": [{"days": 3, "coins": 15}]
                }
            }
        }
        mobile_res = {
            "api": "mtop.aliexpress.growth.bonus.daily.signin",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "day": 4,
                    "coins": 25,
                    "amount": 25,
                    "streak": 4
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "ALREADY_DONE")
            self.assertEqual(mgr.coins_earned, 25)  # 25 from fresh Mobile (15 from PC already done must NOT be added!)
            self.assertEqual(mgr.streak_days, 4, "Mobile streak 4 must be prioritized over PC streak 3!")

    def test_mobile_checkin_already_done_scenario(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_mobile_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": True,
                    "signDays": 2
                }
            }
        }
        mobile_res = {
            "api": "mtop.aliexpress.growth.bonus.daily.signin",
            "ret": ["SUCCESS_REPEAT_CHECKIN::already checked"],
            "data": {
                "signToday": True,
                "streak": 5,
                "coins": 0
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api in (MOBILE_SIGN_LIST_API, MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.pc_status, "ALREADY_DONE")
            self.assertEqual(mgr.mobile_status, "ALREADY_DONE")
            self.assertEqual(mgr.streak_days, 5, "Mobile streak 5 must be prioritized when both channels are already done!")

    def test_dual_channel_both_succeed_sums_coins_and_selects_best_streak(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 2
                }
            }
        }
        pc_sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 3,
                    "coins": 20
                }
            }
        }
        mobile_res = {
            "api": "mtop.aliexpress.growth.bonus.daily.signin",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "day": 5,
                    "awardCoins": 30
                }
            }
        }
        balance_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": [{"name": "userCoinsNum", "value": "1,250"}]
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return pc_sign_res
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_res
                if api == "mtop.aliexpress.coin.execute":
                    return balance_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 50, "Coins earned must be sum of PC (20) and Mobile (30)!")
            self.assertEqual(mgr.streak_days, 5, "Streak must be 5 (highest active streak from mobile)!")
            self.assertEqual(mgr.total_coins, 1250, "End-of-run balance must be updated from _fetch_coin_balance!")

    def test_mobile_fallback_across_growth_endpoints(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_fallback_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 1
                }
            }
        }
        pc_sign_res = {
            "ret": ["FAIL_SYS_SERVICE_NOT_AVAILABLE::Not available"]
        }
        # Endpoint 1: Deprecated
        mobile_res_1 = {"ret": ["FAIL_BIZ_API_DEPRECATED::deprecated"]}
        # Endpoint 2: None / Network failure
        mobile_res_2 = None
        # Endpoint 3: Success!
        mobile_res_3 = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "consecutiveDays": 4,
                    "rewardCoins": 35
                }
            }
        }

        calls_made = []
        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                calls_made.append(api)
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return pc_sign_res
                if api in (MOBILE_SIGN_LIST_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_res_1
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.coin.sign.in"):
                    return mobile_res_2
                if api in ("mtop.aliexpress.social.interactive.sign.user.receive.prize", "mtop.aliexpress.growth.coin.signin"):
                    return mobile_res_3
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "FAILED")
            self.assertEqual(mgr.mobile_status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 35)
            self.assertEqual(mgr.streak_days, 4)
            self.assertTrue(
                MOBILE_SIGN_LIST_API in calls_made or "mtop.aliexpress.growth.bonus.daily.signin" in calls_made
            )
            self.assertTrue(
                MOBILE_SIGN_EXECUTE_API in calls_made or "mtop.aliexpress.growth.coin.sign.in" in calls_made
            )
            self.assertTrue(
                "mtop.aliexpress.social.interactive.sign.user.receive.prize" in calls_made or "mtop.aliexpress.growth.coin.signin" in calls_made
            )

    def test_mobile_captcha_handling_aborts_immediately(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_captcha_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["FAIL_SYS_SERVICE_TIMEOUT::Service timeout"]
        }
        mobile_captcha_res = {
            "ret": ["FAIL_SYS_USER_VALIDATE::need validate"],
            "data": {
                "url": "https://acs.aliexpress.com/_____tmd_____/punish?x=captcha_required"
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(mgr, "_fetch_coin_balance") as mock_balance, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return pc_status_res
                if api in (MOBILE_SIGN_LIST_API, MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_captcha_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
            mock_balance.assert_not_called()

    def test_mobile_session_expired_handling(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_expired_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["FAIL_SYS_SERVICE_TIMEOUT::Service timeout"]
        }
        mobile_session_expired_res = {
            "ret": ["FAIL_SYS_USER_NOT_LOGIN::Not logged in"]
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return pc_status_res
                if api in (MOBILE_SIGN_LIST_API, MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_session_expired_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "SESSION_EXPIRED")
            self.assertFalse(mgr.session_valid)

    def test_dual_channel_both_fail_returns_failed(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_both_fail_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_call_mtop", return_value=None), \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "FAILED")
            self.assertEqual(mgr.pc_status, "FAILED")
            self.assertEqual(mgr.mobile_status, "FAILED")
            self.assertEqual(mgr.coins_earned, 0)
            self.assertEqual(mgr.streak_days, 0)

    def test_mobile_extract_data_nested_and_string_values(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_nested_123"})
        mgr = CheckinManager(client)

        nested_response = {
            "data": {
                "signInResult": {
                    "consecutiveDays": " 6 ",
                    "bonusAmount": " 45.0 "
                }
            }
        }
        coins, streak = mgr._extract_mobile_data(nested_response)
        self.assertEqual(coins, 45)
        self.assertEqual(streak, 6)

        # signToday == False prevents coin extraction
        pre_checkin_response = {
            "data": {
                "signToday": False,
                "days": 3,
                "coins": 20
            }
        }
        coins, streak = mgr._extract_mobile_data(pre_checkin_response)
        self.assertEqual(coins, 0, "signToday: False must suppress old coins!")
        self.assertEqual(streak, 3)

    def test_pc_succeeds_and_mobile_fails_still_gives_success(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_pc_ok_123"})
        mgr = CheckinManager(client)

        pc_status_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signToday": False,
                    "signDays": 3
                }
            }
        }
        pc_sign_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "signDays": 4,
                    "coins": 20
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return pc_status_res
                if api == "mtop.aliexpress.coin.pc.sign":
                    return pc_sign_res
                # all mobile endpoints fail
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "FAILED")
            self.assertEqual(mgr.coins_earned, 20)
            self.assertEqual(mgr.streak_days, 4)

    def test_pc_fails_and_mobile_succeeds_gives_success(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_mobile_ok_123"})
        mgr = CheckinManager(client)

        mobile_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "data": {
                    "day": 6,
                    "coins": 35
                }
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_res
                # all PC endpoints fail
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "FAILED")
            self.assertEqual(mgr.mobile_status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 35)
            self.assertEqual(mgr.streak_days, 6)

    def test_mobile_fresh_checkin_with_has_signin_true_and_award_coins_is_success(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_fresh_123"})
        mgr = CheckinManager(client)

        fresh_mobile_res = {
            "api": "mtop.aliexpress.growth.bonus.daily.signin",
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "hasSignIn": True,
                "streak": 4,
                "awardCoins": 25
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return None
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return fresh_mobile_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS", "Fresh checkin with awardCoins must be SUCCESS, not ALREADY_DONE!")
            self.assertEqual(mgr.mobile_status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 25)
            self.assertEqual(mgr.streak_days, 4)

    def test_mobile_negative_has_signin_suppresses_coins_and_triggers_fallback(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_fallback_neg_123"})
        mgr = CheckinManager(client)

        # Endpoint 1 returns preview info with hasSignIn: False and preview coins
        mobile_info_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "hasSignIn": False,
                "streak": 2,
                "coins": 20
            }
        }
        # Endpoint 2 executes real checkin
        mobile_real_checkin_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "hasSignIn": True,
                "streak": 3,
                "awardCoins": 25
            }
        }

        calls_made = []
        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200, text="")):
            def mock_call(api, data=None):
                calls_made.append(api)
                if api == "mtop.aliexpress.coin.pc.sign.list":
                    return None
                if api in (MOBILE_SIGN_LIST_API, "mtop.aliexpress.growth.bonus.daily.signin"):
                    return mobile_info_res
                if api in (MOBILE_SIGN_EXECUTE_API, "mtop.aliexpress.growth.coin.sign.in"):
                    return mobile_real_checkin_res
                return None
            mock_mtop.side_effect = mock_call

            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 25, "Must earn coins from real endpoint 2, not preview coins from endpoint 1!")
            self.assertEqual(mgr.streak_days, 3)
            self.assertTrue(
                MOBILE_SIGN_LIST_API in calls_made or "mtop.aliexpress.growth.bonus.daily.signin" in calls_made
            )
            self.assertTrue(
                MOBILE_SIGN_EXECUTE_API in calls_made or "mtop.aliexpress.growth.coin.sign.in" in calls_made
            )

    def test_mobile_extract_data_list_wrapped_payload(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_list_wrap_123"})
        mgr = CheckinManager(client)

        list_payload = {
            "data": [
                {
                    "day": 5,
                    "awardCoins": 30
                }
            ]
        }
        coins, streak = mgr._extract_mobile_data(list_payload)
        self.assertEqual(coins, 30)
        self.assertEqual(streak, 5)

    def test_mobile_extract_data_prefers_reward_coins_over_total_coin_count(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_coins_pref_123"})
        mgr = CheckinManager(client)

        payload_with_balance = {
            "data": {
                "awardCoins": 25,
                "coinCount": 5420,
                "streak": 4
            }
        }
        coins, streak = mgr._extract_mobile_data(payload_with_balance)
        self.assertEqual(coins, 25, "awardCoins (25) must take precedence over total coinCount balance (5420)!")
        self.assertEqual(streak, 4)

    def test_is_checkin_success_rejects_all_negative_signin_flags(self):
        from src.checkin import is_checkin_success
        negative_flags = [
            {"signToday": False},
            {"hasSignIn": False},
            {"isSignInToday": False},
            {"hasSigned": False},
            {"alreadySign": False},
            {"signed": False}
        ]
        for flag in negative_flags:
            res = {
                "ret": ["SUCCESS::ok"],
                "data": {
                    "success": True,
                    **flag
                }
            }
    def test_is_checkin_success_rejects_nested_has_signin_false_and_error_code(self):
        from src.checkin import is_checkin_success
        payload_nested_false = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "signInResult": {
                    "hasSignIn": False,
                    "errorCode": "USER_NOT_ELIGIBLE"
                }
            }
        }
        self.assertFalse(is_checkin_success(payload_nested_false), "Must reject nested hasSignIn: False and errorCode!")

        payload_nested_err = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "success": True,
                "module": {
                    "errorMsg": "Rate limit exceeded"
                }
            }
        }
        self.assertFalse(is_checkin_success(payload_nested_err), "Must reject nested errorMsg!")

    def test_dual_channel_mobile_captcha_does_not_fail_successful_pc_checkin(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 20, "streak": 5}), \
             patch.object(mgr, "_run_mobile_checkin", return_value={"status": "CAPTCHA_REQUIRED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok, "PC success must not be invalidated by mobile captcha!")
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "CAPTCHA_REQUIRED")
            self.assertEqual(mgr.coins_earned, 20)
            self.assertEqual(mgr.streak_days, 5)

    def test_dual_channel_mobile_session_expired_does_not_invalidate_valid_pc_checkin(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 15, "streak": 4}), \
             patch.object(mgr, "_run_mobile_checkin", return_value={"status": "SESSION_EXPIRED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok, "PC already done must not be invalidated by mobile session expired!")
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertTrue(mgr.session_valid)
            self.assertEqual(mgr.mobile_status, "SESSION_EXPIRED")

    def test_dual_channel_both_already_done_does_not_double_count_coins(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 15, "streak": 3}), \
             patch.object(mgr, "_run_mobile_checkin", return_value={"status": "ALREADY_DONE", "coins": 15, "streak": 3}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.coins_earned, 15, "Coins must not be doubled (15 + 15 = 30) on repeat ALREADY_DONE runs!")
            self.assertEqual(mgr.coins_earned_this_run, 0, "Coins earned this run must be 0 on repeat ALREADY_DONE runs!")
            self.assertEqual(mgr.prior_rewards_today, 15, "Prior rewards today should capture the 15 coins from earlier checkin.")

    def test_warmup_html_already_done_extracts_coins_from_sign_list(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        html_text = '<script>window.runParams = { totalCoins: 500, streak: 3, isSign: true };</script>'
        resp_html = MagicMock(status_code=200, text=html_text, url="https://www.aliexpress.com/p/coin-pc-index/index.html")

        sign_list_data = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signList": [{"days": 3, "coins": 25}]
            }
        }

        with patch.object(client, "get", return_value=resp_html), \
             patch.object(mgr, "_parse_coin_page_html", return_value=True), \
             patch.object(mgr, "_call_mtop", return_value=sign_list_data):
            mgr.streak_days = 3
            res = mgr._run_pc_checkin()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["coins"], 25, "Must extract coins from sign.list on warmup HTML ALREADY_DONE!")

    def test_is_already_checked_in_rejects_captcha_and_session_expired(self):
        from src.checkin import is_already_checked_in
        res_captcha = {
            'ret': ['RGV587_RUN_SCRIPT::Security verification required'], 
            'data': {'url': 'https://sec.aliexpress.com/_____tmd_____/punish', 'signed': True}
        }
        self.assertFalse(is_already_checked_in(res_captcha), "Captcha must not be diagnosed as ALREADY_DONE!")

        res_expired = {
            'ret': ['FAIL_SYS_USER_NOT_LOGIN::Session expired, already checked before'], 
            'data': {}
        }
        self.assertFalse(is_already_checked_in(res_expired), "Expired session must not be diagnosed as ALREADY_DONE!")

class TestMobileCoinChannelCheckin(unittest.TestCase):
    """
    Автоматизовані регресійні тести для нового мобільного каналу чекіну:
    - mtop.aliexpress.coin.channel.sign.list (COIN_CHANNEL_2.0)
    - mtop.aliexpress.coin.channel.sign.execute (COIN_CHANNEL_2.0)
    - Dual-channel оркестрація та стійкість до збоїв.
    """

    def test_mobile_channel_happy_path(self):
        """Happy path: sign.list повертає signSuccess: False, sign.execute повертає signSuccess: True з нагородою."""
        client = AliClient(cookies={"_m_h5_tk": "tk_happy_123"})
        mgr = CheckinManager(client)

        list_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": {
                    "signQuerySequenceNodeList": [
                        {
                            "dailySignNodeList": [
                                {
                                    "calendarDayDistance": 0,
                                    "signResultList": [
                                        {
                                            "signSuccess": False,
                                            "sequenceNumber": 4,
                                            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                        }
                                    ]
                                },
                                {
                                    "calendarDayDistance": 1,
                                    "signResultList": [
                                        {
                                            "signSuccess": False,
                                            "sequenceNumber": 5,
                                            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 30}]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        }
        exec_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": {
                    "signSuccess": True,
                    "sequenceNumber": 4,
                    "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                }
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                if api == MOBILE_SIGN_LIST_API:
                    return list_res
                if api == MOBILE_SIGN_EXECUTE_API:
                    return exec_res
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["coins"], 25)
            self.assertEqual(res["streak"], 4)

    def test_mobile_channel_already_done_via_list_query(self):
        """sign.list повертає signSuccess: True на calendarDayDistance == 0 -> статус ALREADY_DONE, execute НЕ викликається."""
        client = AliClient(cookies={"_m_h5_tk": "tk_list_done_123"})
        mgr = CheckinManager(client)

        list_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": {
                    "signQuerySequenceNodeList": [
                        {
                            "dailySignNodeList": [
                                {
                                    "calendarDayDistance": 0,
                                    "signResultList": [
                                        {
                                            "signSuccess": True,
                                            "sequenceNumber": 4,
                                            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                        }
                                    ]
                                },
                                {
                                    "calendarDayDistance": 1,
                                    "signResultList": [
                                        {
                                            "signSuccess": False,
                                            "sequenceNumber": 5,
                                            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 30}]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                if api == MOBILE_SIGN_LIST_API:
                    return list_res
                if api == MOBILE_SIGN_EXECUTE_API:
                    self.fail("MOBILE_SIGN_EXECUTE_API не повинен викликатися, якщо чекін уже виконано за даними списку!")
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["coins"], 25)
            self.assertEqual(res["streak"], 4)
            self.assertTrue(is_already_checked_in(list_res))
            self.assertFalse(is_checkin_success(list_res))

    def test_mobile_channel_already_done_via_execute_today_already_sign(self):
        """sign.execute повертає todayAlreadySign: True -> статус ALREADY_DONE."""
        client = AliClient(cookies={"_m_h5_tk": "tk_exec_done_123"})
        mgr = CheckinManager(client)

        list_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": {
                    "signQuerySequenceNodeList": [
                        {
                            "dailySignNodeList": [
                                {
                                    "calendarDayDistance": 0,
                                    "signResultList": [
                                        {
                                            "signSuccess": False,
                                            "sequenceNumber": 4,
                                            "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        }
        exec_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "data": {
                    "todayAlreadySign": True,
                    "sequenceNumber": 4,
                    "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                }
            }
        }
        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                if api == MOBILE_SIGN_LIST_API:
                    return list_res
                if api == MOBILE_SIGN_EXECUTE_API:
                    return exec_res
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["coins"], 25)
            self.assertEqual(res["streak"], 4)
            self.assertTrue(is_already_checked_in(exec_res))
            self.assertFalse(is_checkin_success(exec_res))

    def test_dual_channel_pc_already_done_mobile_success(self):
        """Dual-channel: PC ALREADY_DONE (streak 1, +0) + Mobile SUCCESS (streak 4, +25) -> overall SUCCESS, streak 4, +25 coins."""
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_done_succ_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 0, "streak": 1}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "SUCCESS", "coins": 25, "streak": 4}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.streak_days, 4)
            self.assertEqual(mgr.coins_earned, 25)

    def test_dual_channel_pc_success_mobile_success(self):
        """Dual-channel: PC SUCCESS (streak 1, +20) + Mobile SUCCESS (streak 4, +25) -> overall SUCCESS, streak 4, +45 coins."""
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_succ_succ_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 20, "streak": 1}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "SUCCESS", "coins": 25, "streak": 4}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.streak_days, 4)
            self.assertEqual(mgr.coins_earned, 45)

    def test_dual_channel_fallback_when_mobile_returns_captcha_while_pc_succeeded(self):
        """Dual-channel fallback: Mobile повертає CAPTCHA_REQUIRED, але PC SUCCESS -> загальний статус залишається SUCCESS."""
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_fallback_cap_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 20, "streak": 2}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "CAPTCHA_REQUIRED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "CAPTCHA_REQUIRED")
            self.assertEqual(mgr.coins_earned, 20)
            self.assertEqual(mgr.streak_days, 2)

    def test_dual_channel_fallback_when_mobile_returns_error_while_pc_already_done(self):
        """Dual-channel fallback: Mobile повертає FAILED, але PC ALREADY_DONE -> загальний статус залишається ALREADY_DONE."""
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_fallback_err_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 15, "streak": 3}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "FAILED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.mobile_status, "FAILED")
            self.assertEqual(mgr.coins_earned, 15)
            self.assertEqual(mgr.streak_days, 3)

    def test_malformed_sign_query_sequence_node_list_empty(self):
        """Edge case: signQuerySequenceNodeList є порожнім списком."""
        payload = {"data": {"signQuerySequenceNodeList": []}}
        today_node, sign_result = _find_mobile_daily_sign_node(payload)
        self.assertIsNone(today_node)
        self.assertIsNone(sign_result)

    def test_malformed_missing_sign_result_list(self):
        """Edge case: у щоденному вузлі відсутній signResultList."""
        payload = {
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {"calendarDayDistance": 0}
                        ]
                    }
                ]
            }
        }
        today_node, sign_result = _find_mobile_daily_sign_node(payload)
        self.assertIsNotNone(today_node)
        self.assertIsNone(sign_result)

    def test_malformed_empty_prize_info_list(self):
        """Edge case: prizeInfoList є порожнім списком."""
        node = {
            "calendarDayDistance": 0,
            "signResultList": [
                {
                    "signSuccess": False,
                    "sequenceNumber": 2,
                    "prizeInfoList": []
                }
            ]
        }
        today_node, sign_result = _find_mobile_daily_sign_node({"data": {"dailySignNodeList": [node]}})
        self.assertEqual(_extract_prize_coins(sign_result), 0)
        self.assertEqual(_extract_node_streak(sign_result), 2)

    def test_malformed_missing_calendar_day_distance(self):
        """Edge case: у щоденному вузлі відсутнє поле calendarDayDistance (fallback на перший вузол)."""
        node = {
            "signResultList": [
                {
                    "signSuccess": True,
                    "sequenceNumber": 5,
                    "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 50}]
                }
            ]
        }
        today_node, sign_result = _find_mobile_daily_sign_node({"data": {"dailySignNodeList": [node]}})
        self.assertIsNotNone(today_node)
        self.assertIsNotNone(sign_result)
        self.assertTrue(sign_result.get("signSuccess"))
        self.assertEqual(_extract_prize_coins(sign_result), 50)
        self.assertEqual(_extract_node_streak(sign_result), 5)

    def test_prepare_mtop_params_for_mobile_endpoints(self):
        """Параметри MTOP для execute містять playCode, signTimeMills, asac, _lang, _currency; для list — без signTimeMills."""
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)

        # Execute payload
        exec_params = mgr._prepare_mtop_params(api=MOBILE_SIGN_EXECUTE_API)
        self.assertIsNotNone(exec_params)
        self.assertEqual(exec_params["api"], MOBILE_SIGN_EXECUTE_API)
        exec_data = json.loads(exec_params["data"])
        self.assertEqual(exec_data["playCode"], "COIN_CHANNEL_2.0")
        self.assertIn("signTimeMills", exec_data)
        self.assertIsInstance(exec_data["signTimeMills"], int)
        self.assertEqual(exec_data["asac"], "2E2280406IM61M8MG1F2FB")
        self.assertEqual(exec_params["asac"], "2E2280406IM61M8MG1F2FB")
        self.assertEqual(exec_data["_lang"], "en_KR")
        self.assertEqual(exec_data["_currency"], "USD")

        # List payload
        list_params = mgr._prepare_mtop_params(api=MOBILE_SIGN_LIST_API)
        self.assertIsNotNone(list_params)
        self.assertEqual(list_params["api"], MOBILE_SIGN_LIST_API)
        list_data = json.loads(list_params["data"])
        self.assertEqual(list_data["playCode"], "COIN_CHANNEL_2.0")
        self.assertNotIn("signTimeMills", list_data)
        self.assertEqual(list_data["asac"], "2E2280406IM61M8MG1F2FB")
        self.assertEqual(list_params["asac"], "2E2280406IM61M8MG1F2FB")
        self.assertEqual(list_data["_lang"], "en_KR")
        self.assertEqual(list_data["_currency"], "USD")

    def test_extract_telemetry_with_mobile_sequence_node_list(self):
        """_extract_telemetry коректно обробляє signQuerySequenceNodeList, sequenceNumber та prizeInfoList."""
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mgr = CheckinManager(client)

        payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [
                                    {
                                        "signSuccess": True,
                                        "sequenceNumber": 7,
                                        "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 70}]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        mgr._extract_telemetry(payload, extract_coins=True)
        self.assertEqual(mgr.streak_days, 7)
        self.assertEqual(mgr.coins_earned, 70)

    def test_dual_channel_pc_success_mobile_session_expired_does_not_fail(self):
        """Dual-channel: PC SUCCESS, але Mobile повертає SESSION_EXPIRED -> загальний статус залишається SUCCESS, session_valid=True."""
        client = AliClient(cookies={"_m_h5_tk": "tk_dual_succ_sess_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 20, "streak": 2}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "SESSION_EXPIRED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "SESSION_EXPIRED")
            self.assertTrue(mgr.session_valid)
            self.assertEqual(mgr.coins_earned, 20)
            self.assertEqual(mgr.streak_days, 2)

    def test_mobile_fallback_when_execute_fails(self):
        """Mobile channel fallback: execute повертає помилку/None, але допоміжний receive.prize успішний -> SUCCESS."""
        client = AliClient(cookies={"_m_h5_tk": "tk_fallback_123"})
        mgr = CheckinManager(client)

        list_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [
                                    {"signSuccess": False, "sequenceNumber": 3, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 20}]}
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        fallback_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signSuccess": True,
                "awardCoins": 20,
                "streak": 3
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                if api == MOBILE_SIGN_LIST_API:
                    return list_res
                if api == MOBILE_SIGN_EXECUTE_API:
                    return None  # Execute fails
                if api == "mtop.aliexpress.social.interactive.sign.user.receive.prize":
                    return fallback_res
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["coins"], 20)
            self.assertEqual(res["streak"], 3)

    def test_find_mobile_daily_sign_node_rejects_non_zero_distance(self):
        """Вузли з явно ненульовим calendarDayDistance (наприклад, +1 чи -1) НЕ вважаються сьогоднішнім днем."""
        payload = {
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 1,
                                "signResultList": [{"signSuccess": False, "sequenceNumber": 5, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 30}]}]
                            },
                            {
                                "calendarDayDistance": 2,
                                "signResultList": [{"signSuccess": False, "sequenceNumber": 6, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 35}]}]
                            }
                        ]
                    }
                ]
            }
        }
        today_node, sign_result = _find_mobile_daily_sign_node(payload)
        self.assertIsNone(today_node, "Вузол з calendarDayDistance=1 не повинен підставлятися як сьогоднішній!")
        self.assertIsNone(sign_result)

    def test_find_mobile_daily_sign_node_across_multiple_sequences(self):
        """Пошук вузла поточного дня працює через декілька блоків signQuerySequenceNodeList, навіть якщо перший порожній/минулий."""
        payload = {
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": []  # Порожній список у першій послідовності
                    },
                    {
                        "dailySignNodeList": [
                            {"calendarDayDistance": -1, "signResultList": [{"signSuccess": True, "sequenceNumber": 3}]},
                            {"calendarDayDistance": 0, "signResultList": [{"signSuccess": True, "sequenceNumber": 4, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]}]}
                        ]
                    }
                ]
            }
        }
        today_node, sign_result = _find_mobile_daily_sign_node(payload)
        self.assertIsNotNone(today_node)
        self.assertIsNotNone(sign_result)
        self.assertEqual(today_node.get("calendarDayDistance"), 0)
        self.assertTrue(sign_result.get("signSuccess"))
        self.assertEqual(_extract_node_streak(sign_result), 4)

    def test_extract_prize_coins_string_and_formatted_amounts(self):
        """_extract_prize_coins коректно розпізнає текстові, форматовані з комами та суфіксами кількості монет, ігноруючи bool."""
        self.assertEqual(_extract_prize_coins({"prizeAmount": "25 coins"}), 25)
        self.assertEqual(_extract_prize_coins({"prizeAmount": "+30"}), 30)
        self.assertEqual(_extract_prize_coins({"prizeAmount": "1,500"}), 1500)
        self.assertEqual(_extract_prize_coins({"prizeAmount": "1 200"}), 1200)
        self.assertEqual(_extract_prize_coins({"prizeAmount": 50.0}), 50)
        self.assertEqual(_extract_prize_coins({"coins": True}), 0, "Булевий True не повинен парситися як 1 монета!")
        self.assertEqual(_extract_prize_coins({"coins": None}), 0)

    def test_sequence_number_on_node_and_prize_on_sign_result(self):
        """_checkin_mobile_channel коректно витягує стрік і монети, якщо sequenceNumber знаходиться на рівні вузла, а нагорода — у signResultList."""
        client = AliClient(cookies={"_m_h5_tk": "tk_split_attrs_123"})
        mgr = CheckinManager(client)

        list_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "sequenceNumber": 6,  # на рівні вузла
                                "signResultList": [
                                    {
                                        "signSuccess": True,
                                        "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 35}]  # у signResultList
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        with patch.object(mgr, "_call_mtop", return_value=list_res):
            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["streak"], 6)
            self.assertEqual(res["coins"], 35)

    def test_is_already_checked_in_with_mobile_today_already_sign_even_if_node_false(self):
        """is_already_checked_in розпізнає todayAlreadySign: True, навіть якщо у вкладеному вузлі signSuccess: False."""
        payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "todayAlreadySign": True,
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [{"signSuccess": False, "sequenceNumber": 4}]
                            }
                        ]
                    }
                ]
            }
        }
        self.assertTrue(is_already_checked_in(payload))

    def test_is_checkin_success_with_mobile_nodes(self):
        """is_checkin_success розпізнає signSuccess: True на мобільному виконанні, навіть якщо у signQuerySequenceNodeList майбутній день."""
        payload = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "signSuccess": True,
                "awardCoins": 25,
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 1,
                                "signResultList": [{"signSuccess": False, "sequenceNumber": 5}]
                            }
                        ]
                    }
                ]
            }
        }
        self.assertTrue(is_checkin_success(payload))

    def test_mobile_channel_malformed_response_handling(self):
        """_checkin_mobile_channel стійкий до невалідних відповідей: рядок, список, невірні типи тощо."""
        client = AliClient(cookies={"_m_h5_tk": "tk_malformed_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_call_mtop", return_value="invalid string response"):
            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "FAILED")
            self.assertEqual(res["coins"], 0)
            self.assertEqual(res["streak"], 0)

    def test_fresh_execution_with_updated_sequence_nodes_is_success(self):
        """
        Верифікація свіжого виконання чекіну: MOBILE_SIGN_EXECUTE_API повертає оновлені вузли
        послідовності з signSuccess: True на сьогоднішньому вузлі (calendarDayDistance == 0).
        Переконуємося, що статус є SUCCESS (а не помилковий ALREADY_DONE) і нараховуються свіжі монети.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_fresh_nodes_123"})
        mgr = CheckinManager(client)

        list_res = {
            "api": MOBILE_SIGN_LIST_API,
            "ret": ["SUCCESS::ok"],
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [
                                    {
                                        "signSuccess": False,
                                        "sequenceNumber": 4,
                                        "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        exec_res = {
            "api": MOBILE_SIGN_EXECUTE_API,
            "ret": ["SUCCESS::ok"],
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [
                                    {
                                        "signSuccess": True,
                                        "sequenceNumber": 4,
                                        "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        # Перевірка класифікаторів: свіже виконання без штучного верхньорівневого signSuccess не повинно бути ALREADY_DONE
        self.assertFalse(is_already_checked_in(exec_res), "Свіже виконання з signSuccess: True на сьогоднішньому вузлі не повинно бути ALREADY_DONE!")
        self.assertTrue(is_checkin_success(exec_res), "Свіже виконання з signSuccess: True на сьогоднішньому вузлі повинно бути SUCCESS!")

        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                if api == MOBILE_SIGN_LIST_API:
                    return list_res
                if api == MOBILE_SIGN_EXECUTE_API:
                    return exec_res
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["coins"], 25)
            self.assertEqual(res["streak"], 4)

    def test_dual_channel_pc_already_done_coins_not_added_to_mobile_fresh_success(self):
        """
        Верифікація обліку зароблених монет: коли PC є ALREADY_DONE (+15 монет),
        а Mobile є SUCCESS (+25 монет), self.coins_earned повинно бути строго 25 (НЕ 40!).
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_no_double_counting_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 15, "streak": 2}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "SUCCESS", "coins": 25, "streak": 5}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "ALREADY_DONE")
            self.assertEqual(mgr.mobile_status, "SUCCESS")
            self.assertEqual(mgr.coins_earned, 25, "Монети від ALREADY_DONE (+15) не повинні додаватися до свіжого SUCCESS (+25)!")
            self.assertEqual(mgr.streak_days, 5)

    def test_find_mobile_daily_sign_node_fallback_is_today_when_calendar_distance_missing(self):
        """
        Верифікація fallback на isToday/today/current: True, коли calendarDayDistance відсутнє в даних.
        """
        payload = {
            "data": {
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "sequenceNumber": 1,
                                "isToday": False,
                                "signResultList": [{"signSuccess": True, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 10}]}]
                            },
                            {
                                "sequenceNumber": 2,
                                "isToday": True,
                                "signResultList": [{"signSuccess": False, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 20}]}]
                            },
                            {
                                "sequenceNumber": 3,
                                "isToday": False,
                                "signResultList": [{"signSuccess": False, "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 30}]}]
                            }
                        ]
                    }
                ]
            }
        }
        today_node, sign_result = _find_mobile_daily_sign_node(payload)
        self.assertIsNotNone(today_node)
        self.assertIsNotNone(sign_result)
        self.assertEqual(today_node.get("sequenceNumber"), 2)
        self.assertTrue(today_node.get("isToday"))
        self.assertFalse(sign_result.get("signSuccess"))
        self.assertEqual(_extract_prize_coins(sign_result), 20)

    def test_mobile_sign_query_list_auxiliary_fallback(self):
        """
        Верифікація auxiliary fallback: якщо MOBILE_SIGN_LIST_API повертає None або порожню відповідь,
        викликається MOBILE_SIGN_QUERY_LIST_API.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_aux_list_123"})
        mgr = CheckinManager(client)

        query_res = {
            "api": MOBILE_SIGN_QUERY_LIST_API,
            "ret": ["SUCCESS::ok"],
            "data": {
                "dailySignNodeList": [
                    {
                        "calendarDayDistance": 0,
                        "signResultList": [
                            {
                                "signSuccess": True,
                                "sequenceNumber": 6,
                                "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 35}]
                            }
                        ]
                    }
                ]
            }
        }

        calls = []
        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                calls.append(api)
                if api == MOBILE_SIGN_LIST_API:
                    return None  # First call returns None / fails
                if api == MOBILE_SIGN_QUERY_LIST_API:
                    return query_res
                return None
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["coins"], 35)
            self.assertEqual(res["streak"], 6)
            self.assertIn(MOBILE_SIGN_LIST_API, calls)
            self.assertIn(MOBILE_SIGN_QUERY_LIST_API, calls)


    def test_mobile_execute_today_already_sign_takes_precedence_over_award_coins(self):
        """
        Верифікація пріоритету: якщо MOBILE_SIGN_EXECUTE_API повертає todayAlreadySign: True,
        але водночас містить призові монети (серверна аномалія дубліката),
        todayAlreadySign має строгий пріоритет: статус ALREADY_DONE, а не SUCCESS.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_already_sign_123"})
        mgr = CheckinManager(client)

        exec_res = {
            "api": MOBILE_SIGN_EXECUTE_API,
            "ret": ["SUCCESS::ok"],
            "data": {
                "todayAlreadySign": True,
                "awardCoins": 25,
                "signQuerySequenceNodeList": [
                    {
                        "dailySignNodeList": [
                            {
                                "calendarDayDistance": 0,
                                "signResultList": [
                                    {
                                        "signSuccess": True,
                                        "sequenceNumber": 5,
                                        "prizeInfoList": [{"prizeType": "coins", "prizeAmount": 25}]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        self.assertTrue(is_already_checked_in(exec_res), "todayAlreadySign: True повинен безумовно класифікуватися як ALREADY_DONE!")
        self.assertFalse(is_checkin_success(exec_res), "todayAlreadySign: True не повинен бути SUCCESS!")

        with patch.object(mgr, "_call_mtop") as mock_call:
            mock_call.side_effect = [None, exec_res]
            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "ALREADY_DONE")
            self.assertEqual(res["coins"], 25)
            self.assertEqual(res["streak"], 5)

    def test_dual_channel_pc_fresh_success_mobile_already_done_coins_not_mixed(self):
        """
        Коли PC є SUCCESS (+20 монет), а Mobile є ALREADY_DONE (+30 монет),
        загальна сума зароблених монет повинна бути строго 20 (НЕ 50 і НЕ 30!).
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_pc_fresh_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 20, "streak": 3}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "ALREADY_DONE", "coins": 30, "streak": 7}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "SUCCESS")
            self.assertEqual(mgr.pc_status, "SUCCESS")
            self.assertEqual(mgr.mobile_status, "ALREADY_DONE")
            self.assertEqual(mgr.coins_earned, 20, "Монети від ALREADY_DONE (+30) не повинні додаватися до свіжого PC SUCCESS (+20)!")
            self.assertEqual(mgr.streak_days, 7)

    def test_prepare_mtop_params_mobile_query_list_and_receive_prize(self):
        """
        Верифікація використання ASAC_MOBILE_CAMPAIGN для допоміжних ендпоінтів
        MOBILE_SIGN_QUERY_LIST_API та MOBILE_RECEIVE_PRIZE_API.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_asac_aux_123"})
        mgr = CheckinManager(client)

        for api in (MOBILE_SIGN_QUERY_LIST_API, MOBILE_RECEIVE_PRIZE_API):
            params = mgr._prepare_mtop_params(api=api)
            self.assertIsNotNone(params, f"Параметри для {api} не повинні бути None")
            self.assertEqual(params["asac"], ASAC_MOBILE_CAMPAIGN, f"Query asac для {api} має бути {ASAC_MOBILE_CAMPAIGN}")
            data_obj = json.loads(params["data"])
            self.assertEqual(data_obj["asac"], ASAC_MOBILE_CAMPAIGN, f"Data asac для {api} має бути {ASAC_MOBILE_CAMPAIGN}")

    def test_call_mtop_attaches_api_to_result_if_missing(self):
        """
        _call_mtop автоматично додає ключ 'api' у словник результату,
        якщо шлюз MTOP не повернув його в тілі JSON.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_call_mtop_api_123"})
        mgr = CheckinManager(client)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ret": ["SUCCESS::ok"], "data": {"signSuccess": True}}
        mock_resp.text = '{"ret": ["SUCCESS::ok"], "data": {"signSuccess": true}}'

        with patch.object(client, "post", return_value=mock_resp):
            res = mgr._call_mtop(MOBILE_SIGN_EXECUTE_API)
            self.assertIsNotNone(res)
            self.assertIn("api", res)
            self.assertEqual(res["api"], MOBILE_SIGN_EXECUTE_API)

    def test_mobile_sign_list_failure_does_not_call_query_list_if_session_expired(self):
        """
        Якщо MOBILE_SIGN_LIST_API повернув SESSION_EXPIRED, допоміжний запит
        до MOBILE_SIGN_QUERY_LIST_API не повинен виконуватися даремно.
        """
        client = AliClient(cookies={"_m_h5_tk": "tk_dead_session_123"})
        mgr = CheckinManager(client)

        expired_res = {
            "api": MOBILE_SIGN_LIST_API,
            "ret": ["FAIL_SYS_SESSION_EXPIRED::Session expired"],
            "data": {}
        }

        calls = []
        with patch.object(mgr, "_call_mtop") as mock_call:
            def side_effect(api, data=None):
                calls.append(api)
                return expired_res
            mock_call.side_effect = side_effect

            res = mgr._checkin_mobile_channel()
            self.assertEqual(res["status"], "SESSION_EXPIRED")
            self.assertEqual(calls, [MOBILE_SIGN_LIST_API])
            self.assertNotIn(MOBILE_SIGN_QUERY_LIST_API, calls)


class TestSafeCoinTasksAutomation(unittest.TestCase):
    def setUp(self):
        CoinsFarmManager.reset_dead_urls()

    def tearDown(self):
        CoinsFarmManager.reset_dead_urls()

    def test_is_safe_task_accepts_browse_and_claim_tasks(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        # 1. Завдання на перегляд сторінки (browse / view / 15s)
        safe_browse_1 = {
            "taskId": "1001",
            "taskTitle": "Browse items for 15s to get coins",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/category/1000.html",
            "status": "todo",
            "staySeconds": 15
        }
        self.assertTrue(mgr.is_safe_task(safe_browse_1))

        # 2. Завдання з безпечним типом "browse"
        safe_browse_2 = {
            "taskId": "1002",
            "taskTitle": "Daily discovery",
            "taskType": "browse",
            "actionUrl": "https://aliexpress.com/category/2000.html",
            "status": "todo"
        }
        self.assertTrue(mgr.is_safe_task(safe_browse_2))

        # 3. Завдання готове до забирання нагороди (waitForReceive)
        safe_claim_1 = {
            "taskId": "1003",
            "taskTitle": "Order coins reward",  # even if word order is in title, if already done to claim
            "status": "waitForReceive",
            "unIssueRecord": ["rec_123"]
        }
        # Notice: if it has unsafe words in title, safety filter checks UNSAFE_KEYWORDS first:
        # Let's verify a standard clean claim task:
        safe_claim_clean = {
            "taskId": "1004",
            "taskTitle": "Browse reward claim",
            "status": "waitForReceive",
            "unIssueRecord": ["rec_123"]
        }
        self.assertTrue(mgr.is_safe_task(safe_claim_clean))

        # 4. Завдання на пошук (search)
        safe_search = {
            "taskId": "1005",
            "taskTitle": "Search top finds",
            "actionType": "search",
            "actionUrl": "https://aliexpress.com/search",
            "status": "todo"
        }
        self.assertTrue(mgr.is_safe_task(safe_search))

    def test_is_safe_task_strictly_rejects_unsafe_actions(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        # 1. Купівля або замовлення (order, buy, pay, purchase)
        unsafe_order = {
            "taskId": "2001",
            "taskTitle": "Place an order to earn 500 coins",
            "taskType": "order",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_order))

        unsafe_buy = {
            "taskId": "2002",
            "taskTitle": "Buy any item today",
            "actionUrl": "https://aliexpress.com/buy",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_buy))

        # 2. Додавання в кошик (cart)
        unsafe_cart = {
            "taskId": "2003",
            "taskTitle": "Add 2 items to your cart",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_cart))

        # 3. Підписка на магазин (follow)
        unsafe_follow = {
            "taskId": "2004",
            "taskTitle": "Follow official store",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_follow))

        # 4. Прив'язка або зовнішній шер (share, bind)
        unsafe_share = {
            "taskId": "2005",
            "taskTitle": "Share deal with friends",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_share))

        # 5. Уже виконані завдання не беруться в роботу повторно
        completed_task = {
            "taskId": "2006",
            "taskTitle": "Browse items for 15s",
            "taskType": 0,
            "status": "completed"
        }
        self.assertFalse(mgr.is_safe_task(completed_task))

        # 6. Некоректні вхідні дані
        self.assertFalse(mgr.is_safe_task(None))
        self.assertFalse(mgr.is_safe_task("invalid"))

    def test_fetch_active_tasks_mtop_normalization(self):
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        mock_delivery_response = {
            "api": "mtop.aliexpress.interactive.task.delivery.query",
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": [
                    {
                        "taskInstanceId": "inst_999",
                        "taskAppKey": "llcvfnh8tuknrw80cgstljy4h2y7i1rh",
                        "taskSceneId": 149,
                        "materials": [
                            {
                                "taskTitle": "Browse coins catalog 15s",
                                "taskType": 0,
                                "newTaskStatus": "todo",
                                "staySeconds": 15,
                                "actionUrl": "https://www.aliexpress.com/p/coin-pc-index/index.html",
                                "unIssueRecord": None
                            }
                        ]
                    }
                ]
            }
        }

        with patch.object(checkin_mgr, "_call_mtop", return_value=mock_delivery_response) as mock_call:
            tasks = coins_mgr.fetch_active_tasks()
            self.assertEqual(len(tasks), 1)
            t = tasks[0]
            self.assertEqual(t["taskInstanceId"], "inst_999")
            self.assertEqual(t["taskTitle"], "Browse coins catalog 15s")
            self.assertEqual(t["staySeconds"], 15)
            self.assertEqual(t["status"], "todo")
            self.assertEqual(t["actionUrl"], "https://www.aliexpress.com/p/coin-pc-index/index.html")

            mock_call.assert_called_once()
            call_data = mock_call.call_args[1]["data"]
            self.assertEqual(call_data["locale"], "UA")
            self.assertEqual(call_data["_lang"], "en_US")
            self.assertEqual(call_data["_currency"], "UAH")

    def test_farm_browse_tasks_executes_safe_browse_with_15s_and_claims_coins(self):
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        safe_task = {
            "taskInstanceId": "inst_123",
            "taskAppKey": "key_app",
            "taskSceneId": 149,
            "taskType": 0,
            "taskTitle": "Browse items for 15s",
            "status": "todo",
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/item/123.html",
            "unIssueRecord": None,
            "issueHandle": 1
        }

        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[safe_task]), \
             patch.object(client, "get", return_value=MagicMock(status_code=200)) as mock_get, \
             patch.object(client, "sleep_random") as mock_sleep, \
             patch.object(coins_mgr, "_complete_task", return_value=True) as mock_complete, \
             patch.object(coins_mgr, "_claim_task_reward", return_value=20) as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            self.assertEqual(coins_mgr.completed_tasks, 1)
            self.assertEqual(coins_mgr.task_coins_earned, 20)

            mock_get.assert_called_once_with("https://aliexpress.com/item/123.html")
            mock_sleep.assert_called_once()
            # Verify sleep duration was approximately 15 seconds
            args = mock_sleep.call_args[0]
            self.assertGreaterEqual(args[0], 15.0)

            mock_complete.assert_called_once_with(safe_task)
            mock_claim.assert_called_once_with(safe_task)

    def test_farm_browse_tasks_claims_wait_for_receive_directly_without_sleep(self):
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        ready_task = {
            "taskInstanceId": "inst_ready_777",
            "taskTitle": "View items done",
            "status": "waitForReceive",
            "unIssueRecord": ["rec_777"]
        }

        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[ready_task]), \
             patch.object(client, "get") as mock_get, \
             patch.object(client, "sleep_random") as mock_sleep, \
             patch.object(coins_mgr, "_claim_task_reward", return_value=15) as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            self.assertEqual(coins_mgr.completed_tasks, 1)
            self.assertEqual(coins_mgr.task_coins_earned, 15)

            # Direct claim: no unnecessary URL browsing or 15s wait
            mock_get.assert_not_called()
            mock_sleep.assert_not_called()
            mock_claim.assert_called_once_with(ready_task)

    def test_complete_task_payload_and_issue_reward(self):
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        task = {
            "taskInstanceId": "inst_555",
            "taskAppKey": "test_app_key",
            "taskSceneId": 149,
            "unIssueRecord": ["rec_abc"]
        }

        complete_response = {
            "api": "mtop.aliexpress.interactive.task.process.complete",
            "ret": ["SUCCESS::调用成功"],
            "data": {"success": True}
        }
        issue_response = {
            "api": "mtop.aliexpress.interactive.task.process.issue",
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "success": True,
                "awardCoins": 15
            }
        }

        with patch.object(checkin_mgr, "_call_mtop", side_effect=[complete_response, issue_response]) as mock_call:
            ok = coins_mgr._complete_task(task)
            self.assertTrue(ok)
            coins = coins_mgr._claim_task_reward(task)
            self.assertEqual(coins, 15)

            self.assertEqual(mock_call.call_count, 2)
            call1_args = mock_call.call_args_list[0]
            self.assertEqual(call1_args[0][0], "mtop.aliexpress.interactive.task.process.complete")
            self.assertEqual(call1_args[1]["data"]["taskInstanceId"], "inst_555")
            self.assertEqual(call1_args[1]["data"]["locale"], "UA")
            self.assertEqual(call1_args[1]["data"]["_lang"], "en_US")
            self.assertEqual(call1_args[1]["data"]["_currency"], "UAH")

            call2_args = mock_call.call_args_list[1]
            self.assertEqual(call2_args[0][0], "mtop.aliexpress.interactive.task.process.issue")
            self.assertEqual(call2_args[1]["data"]["taskInstanceId"], "inst_555")
            self.assertEqual(call2_args[1]["data"]["locale"], "UA")
            self.assertEqual(call2_args[1]["data"]["_lang"], "en_US")
            self.assertEqual(call2_args[1]["data"]["_currency"], "UAH")
            self.assertIn("rec_abc", call2_args[1]["data"]["detailIdsString"])

    def test_main_accumulates_task_coins_into_summary(self):
        with patch("src.main.initial_random_jitter"), \
             patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True), \
             patch("src.main.AliClient") as mock_client_cls, \
             patch("src.main.CheckinManager") as mock_checkin_cls, \
             patch("src.main.CoinsFarmManager") as mock_coins_cls, \
             patch("src.main.NotificationDispatcher") as mock_disp_cls:

            mock_checkin = mock_checkin_cls.return_value
            mock_checkin.run_checkin.return_value = True
            mock_checkin.session_valid = True
            mock_checkin.status = "SUCCESS"
            mock_checkin.coins_earned = 25
            mock_checkin.streak_days = 4
            mock_checkin.total_coins = 400

            mock_coins = mock_coins_cls.return_value
            mock_coins.farm_browse_tasks.return_value = 2
            mock_coins.task_coins_earned = 30

            disp = MagicMock()
            mock_disp_cls.return_value = disp

            main_module.main()

            disp.add_summary.assert_called_once()
            summary = disp.add_summary.call_args[0][0]
            self.assertEqual(summary.coins_earned, 55)  # 25 (checkin) + 30 (tasks)
            self.assertEqual(summary.total_coins, 400)  # Server balance takes precedence without synthetic addition
            self.assertEqual(summary.tasks_done, 2)

    def test_prepare_mtop_params_interactive_task_endpoints_contain_lang_and_currency(self):
        client = AliClient(cookies={"_m_h5_tk": "testtoken_123"})
        mgr = CheckinManager(client)
        for api in (TASK_DELIVERY_QUERY_API, TASK_PROCESS_COMPLETE_API, TASK_PROCESS_ISSUE_API):
            # Test dict data
            params_dict = mgr._prepare_mtop_params(api=api, data={"taskId": "123"})
            self.assertIsNotNone(params_dict)
            data_obj = json.loads(params_dict["data"])
            self.assertEqual(data_obj.get("locale"), "UA")
            self.assertEqual(data_obj.get("_lang"), "en_US")
            self.assertEqual(data_obj.get("_currency"), "UAH")
            self.assertEqual(data_obj.get("lang"), "en_US")
            self.assertEqual(data_obj.get("currency"), "UAH")
            self.assertEqual(data_obj.get("asac"), ASAC_TASK_CAMPAIGN)
            self.assertEqual(data_obj.get("projectAppKey"), PROJECT_APP_KEY)
            self.assertEqual(data_obj.get("sceneId"), TASK_SCENE_ID)

            # Test string data
            params_str = mgr._prepare_mtop_params(api=api, data=json.dumps({"taskId": "123"}))
            self.assertIsNotNone(params_str)
            data_obj2 = json.loads(params_str["data"])
            self.assertEqual(data_obj2.get("locale"), "UA")
            self.assertEqual(data_obj2.get("_lang"), "en_US")
            self.assertEqual(data_obj2.get("_currency"), "UAH")
            self.assertEqual(data_obj2.get("lang"), "en_US")
            self.assertEqual(data_obj2.get("currency"), "UAH")
            self.assertEqual(data_obj2.get("asac"), ASAC_TASK_CAMPAIGN)
            self.assertEqual(data_obj2.get("projectAppKey"), PROJECT_APP_KEY)
            self.assertEqual(data_obj2.get("sceneId"), TASK_SCENE_ID)

            # Test None data for ALL interactive task APIs
            params_none = mgr._prepare_mtop_params(api=api, data=None)
            self.assertIsNotNone(params_none)
            data_none_obj = json.loads(params_none["data"])
            self.assertEqual(data_none_obj.get("locale"), "UA")
            self.assertEqual(data_none_obj.get("_lang"), "en_US")
            self.assertEqual(data_none_obj.get("_currency"), "UAH")
            self.assertEqual(data_none_obj.get("lang"), "en_US")
            self.assertEqual(data_none_obj.get("currency"), "UAH")
            self.assertEqual(data_none_obj.get("asac"), ASAC_TASK_CAMPAIGN)
            self.assertEqual(data_none_obj.get("projectAppKey"), PROJECT_APP_KEY)
            self.assertEqual(data_none_obj.get("sceneId"), TASK_SCENE_ID)

            # Test empty dict and empty string
            for empty_val in ({}, ""):
                params_empty = mgr._prepare_mtop_params(api=api, data=empty_val)
                self.assertIsNotNone(params_empty)
                data_empty_obj = json.loads(params_empty["data"])
                self.assertEqual(data_empty_obj.get("locale"), "UA")
                self.assertEqual(data_empty_obj.get("_lang"), "en_US")
                self.assertEqual(data_empty_obj.get("_currency"), "UAH")
                self.assertEqual(data_empty_obj.get("lang"), "en_US")
                self.assertEqual(data_empty_obj.get("currency"), "UAH")

    def test_task_delivery_query_includes_required_locale_param(self):
        """Перевіряє, що запити до інтерактивної платформи завдань завжди мають обов'язковий бізнес-параметр locale."""
        client = AliClient(cookies={"_m_h5_tk": "mytoken_123"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        with patch.object(mgr, "_call_mtop", return_value={"data": {"result": []}}) as mock_call:
            coins.fetch_active_tasks()
            mock_call.assert_called_once()
            call_api, call_kwargs = mock_call.call_args[0][0], mock_call.call_args[1]
            self.assertEqual(call_api, TASK_DELIVERY_QUERY_API)
            payload = call_kwargs["data"]
            self.assertEqual(payload["locale"], "UA")
            self.assertEqual(payload["_lang"], "en_US")
            self.assertEqual(payload["_currency"], "UAH")
            self.assertEqual(payload["lang"], "en_US")
            self.assertEqual(payload["currency"], "UAH")
            self.assertEqual(payload["taskSceneId"], TASK_SCENE_ID)
            self.assertEqual(payload["sceneId"], TASK_SCENE_ID)
            self.assertTrue(payload["fromNew"])

    def test_fetch_active_tasks_nested_mtop_structure_and_custom_locale(self):
        """Перевіряє коректний парсинг вкладених структур JSON (data.data.result) та збереження кастомного locale."""
        client = AliClient(cookies={"_m_h5_tk": "mytoken_nested_123"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        mock_nested_res = {
            "api": TASK_DELIVERY_QUERY_API,
            "data": {
                "success": True,
                "data": {
                    "result": [
                        {
                            "taskInstanceId": "inst_custom_777",
                            "taskTitle": "Browse specialized catalog",
                            "locale": "en_KR",
                            "_lang": "en_KR",
                            "materials": [
                                {
                                    "newTaskStatus": "todo",
                                    "staySeconds": 15,
                                    "actionUrl": "https://aliexpress.com/p/special.html"
                                }
                            ]
                        }
                    ]
                }
            }
        }

        with patch.object(mgr, "_call_mtop", return_value=mock_nested_res):
            tasks = coins.fetch_active_tasks()
            self.assertEqual(len(tasks), 1)
            t = tasks[0]
            self.assertEqual(t["taskInstanceId"], "inst_custom_777")
            self.assertEqual(t["locale"], "en_KR")
            self.assertEqual(t["_lang"], "en_KR")

        # Перевірка передачі кастомного locale у _complete_task та _claim_task_reward
        with patch.object(mgr, "_call_mtop", return_value={"data": {"success": True, "awardCoins": 25}}) as mock_call:
            ok = coins._complete_task(t)
            self.assertTrue(ok)
            complete_payload = mock_call.call_args[1]["data"]
            self.assertEqual(complete_payload["locale"], "en_KR")
            self.assertEqual(complete_payload["_lang"], "en_KR")

            claimed = coins._claim_task_reward(t)
            self.assertEqual(claimed, 25)
            claim_payload = mock_call.call_args[1]["data"]
            self.assertEqual(claim_payload["locale"], "en_KR")
            self.assertEqual(claim_payload["_lang"], "en_KR")

    def test_notifications_telegram_includes_tasks_done(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:FAKE_TOKEN", "TELEGRAM_CHAT_ID": "987654"}):
            dispatcher = NotificationDispatcher()
            dispatcher.add_summary(AccountSummary(
                name="Account_1",
                checkin_status="SUCCESS",
                coins_earned=40,
                streak_days=7,
                tasks_done=4
            ))

            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                res = dispatcher.send_telegram_notification()
                self.assertTrue(res)
                mock_post.assert_called_once()
                call_text = mock_post.call_args[1]["json"]["text"]
                self.assertIn("тасок: 4", call_text)
                self.assertIn("+40 монет", call_text)

    def test_task_delivery_query_includes_ttid_and_platform_in_params_and_data_and_sign(self):
        """
        Перевіряє, що для interactive task ендпоінтів:
        1) у URL query params передаються: appKey (TASK_MTOP_APP_KEY=24815441), isSec=1, security=true,
           securityParamsPosition=data, H5Request=true, asac, platform (а ttid відсутній у query params),
        2) всередині JSON-пейлоаду data передаються: ua, umidToken, umidTokenType, ttid, platform, locale, _currency,
        3) у тілі POST передаються data, bx-umidtoken та bx-sys,
        4) підпис sign розраховується суворо з точного рядка data та TASK_MTOP_APP_KEY.
        """
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)

        for api in (TASK_DELIVERY_QUERY_API, TASK_PROCESS_COMPLETE_API, TASK_PROCESS_ISSUE_API):
            params = mgr._prepare_mtop_params(api=api, data={"taskId": "test_task_123"})
            self.assertIsNotNone(params)
            self.assertEqual(params["appKey"], TASK_MTOP_APP_KEY)
            self.assertEqual(params["isSec"], "1")
            self.assertEqual(params["security"], "true")
            self.assertEqual(params["securityParamsPosition"], "data")
            self.assertEqual(params["H5Request"], "true")
            self.assertEqual(params["asac"], ASAC_TASK_CAMPAIGN)
            self.assertEqual(params["platform"], TASK_PLATFORM)
            # ttid не повинен бути у query params
            self.assertNotIn("ttid", params)

            data_obj = json.loads(params["data"])
            self.assertIn("ttid", data_obj)
            self.assertEqual(data_obj["ttid"], TASK_TTID)
            self.assertEqual(data_obj["locale"], "UA")
            self.assertEqual(data_obj["_currency"], "UAH")
            self.assertEqual(data_obj["umidToken"], TASK_UMID_TOKEN)
            self.assertEqual(data_obj["umidTokenType"], "TOKEN")
            self.assertTrue(data_obj["ua"].startswith("defaultUA2_load_failed with timeout@@"))
            self.assertEqual(data_obj["platform"], TASK_PLATFORM)

            # Перевірка строгої валідності MD5-підпису
            expected_sign = hashlib.md5(f"mytoken123&{params['t']}&{params['appKey']}&{params['data']}".encode("utf-8")).hexdigest()
            self.assertEqual(params["sign"], expected_sign)

        # Перевірка відправки headers, query params та post body під час _call_mtop
        with patch.object(client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ret": ["SUCCESS::ok"], "data": {}})
            mgr._call_mtop(TASK_DELIVERY_QUERY_API, data={"taskSceneId": 149})
            mock_post.assert_called_once()
            called_query_params = mock_post.call_args[1]["params"]
            self.assertEqual(called_query_params["appKey"], TASK_MTOP_APP_KEY)
            self.assertEqual(called_query_params["isSec"], "1")
            self.assertEqual(called_query_params["security"], "true")
            self.assertEqual(called_query_params["H5Request"], "true")
            self.assertEqual(called_query_params["platform"], TASK_PLATFORM)
            self.assertNotIn("ttid", called_query_params)

            called_post_data = mock_post.call_args[1]["data"]
            self.assertIn("data", called_post_data)
            self.assertEqual(called_post_data["bx-umidtoken"], TASK_BX_UMIDTOKEN)
            self.assertEqual(called_post_data["bx-sys"], "ua-l:no__um-l:no__js:https://g.alicdn.com/")

            called_headers = mock_post.call_args[1]["headers"]
            self.assertEqual(called_headers["asac"], ASAC_TASK_CAMPAIGN)
            self.assertEqual(called_headers["Referer"], "https://m.aliexpress.com/")

    def test_task_delivery_query_error_sets_failed_status_and_skips_catalog_simulation(self):
        """
        Якщо mtop.aliexpress.interactive.task.delivery.query повертає помилку в ret (наприклад FAIL_SYS_BIZPARAM_MISSED),
        скрипт повинен:
        1) позначити етап як FAILED,
        2) не робити фейковий обхід категорій (women-clothing.html тощо),
        3) повернути 0 виконаних завдань.
        """
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        for error_ret in ("FAIL_SYS_BIZPARAM_MISSED::缺少业务参数ttid", "FAIL_SYS_BIZPARAM_MISSED::缺少业务参数platform"):
            error_mtop_res = {
                "api": TASK_DELIVERY_QUERY_API,
                "data": {},
                "ret": [error_ret],
                "v": "1.0"
            }

            with patch.object(mgr, "_call_mtop", return_value=error_mtop_res), \
                 patch.object(client, "get") as mock_get:
                done = coins.farm_browse_tasks(count=4)

                self.assertEqual(done, 0)
                self.assertEqual(coins.status, "FAILED")
                self.assertIn("FAIL_SYS_BIZPARAM_MISSED", coins.error_message)

                # Жоден GET-запит на каталог товарів/категорій не повинен викликатися!
                for call in mock_get.call_args_list:
                    called_url = call[0][0]
                    self.assertNotIn("category", called_url)
                    self.assertNotIn("women-clothing", called_url)

    def test_task_delivery_query_success_logs_and_executes_task_with_ttid_and_platform(self):
        """
        При успішній відповіді task.delivery.query завдання виконується та передає ttid і platform у complete/claim.
        """
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        success_mtop_res = {
            "api": TASK_DELIVERY_QUERY_API,
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": [
                    {
                        "taskInstanceId": "inst_task_live_1",
                        "taskTitle": "Browse items for 15s",
                        "materials": [
                            {
                                "newTaskStatus": "todo",
                                "staySeconds": 15,
                                "actionUrl": "https://www.aliexpress.com/item/10001.html",
                                "issueHandle": 1
                            }
                        ]
                    }
                ]
            }
        }

        with patch.object(mgr, "_call_mtop") as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(client, "sleep_random"):

            def mtop_side_effect(api, data=None):
                if api == TASK_DELIVERY_QUERY_API:
                    return success_mtop_res
                elif api == TASK_PROCESS_COMPLETE_API:
                    return {"ret": ["SUCCESS::调用成功"], "data": {"success": True}}
                elif api == TASK_PROCESS_ISSUE_API:
                    return {"ret": ["SUCCESS::调用成功"], "data": {"success": True, "awardCoins": 25}}
                return None

            mock_mtop.side_effect = mtop_side_effect

            done = coins.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            self.assertEqual(coins.status, "SUCCESS")
            self.assertEqual(coins.task_coins_earned, 25)

            # Перевіряємо наявність ttid та platform у викликах complete та issue
            complete_calls = [c for c in mock_mtop.call_args_list if c[0][0] == TASK_PROCESS_COMPLETE_API]
            self.assertTrue(len(complete_calls) > 0)
            self.assertEqual(complete_calls[0][1]["data"]["ttid"], TASK_TTID)
            self.assertEqual(complete_calls[0][1]["data"]["platform"], TASK_PLATFORM)

            issue_calls = [c for c in mock_mtop.call_args_list if c[0][0] == TASK_PROCESS_ISSUE_API]
            self.assertTrue(len(issue_calls) > 0)
            self.assertEqual(issue_calls[0][1]["data"]["ttid"], TASK_TTID)
            self.assertEqual(issue_calls[0][1]["data"]["platform"], TASK_PLATFORM)

    def test_task_delivery_query_platform_fallback_succeeds_on_second_candidate(self):
        """
        Перевіряє, що якщо перший platform (наприклад android) повертає E2-14-3::no group match,
        скрипт пробує наступні кандидати (wireless) і при SUCCESS використовує його далі для complete та claim.
        """
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        tried_platforms = []

        def mtop_side_effect(api, data=None):
            if api == TASK_DELIVERY_QUERY_API:
                plat = data.get("platform")
                tried_platforms.append(plat)
                if plat == "android":
                    return {
                        "api": TASK_DELIVERY_QUERY_API,
                        "data": {"errorCode": "E2-14-3", "errorMsg": "no group match."},
                        "ret": ["E2-14-3::no group match."],
                        "v": "1.0"
                    }
                elif plat == "wireless":
                    return {
                        "api": TASK_DELIVERY_QUERY_API,
                        "ret": ["SUCCESS::调用成功"],
                        "data": {
                            "result": [
                                {
                                    "taskInstanceId": "task_wire_1",
                                    "taskTitle": "Browse 15s via wireless",
                                    "materials": [{"newTaskStatus": "todo", "staySeconds": 15, "actionUrl": "https://aliexpress.com/item/1.html", "issueHandle": 1}]
                                }
                            ]
                        }
                    }
            elif api == TASK_PROCESS_COMPLETE_API:
                return {"ret": ["SUCCESS::ok"], "data": {"success": True}}
            elif api == TASK_PROCESS_ISSUE_API:
                return {"ret": ["SUCCESS::ok"], "data": {"success": True, "awardCoins": 15}}
            return None

        with patch.object(mgr, "_call_mtop", side_effect=mtop_side_effect) as mock_mtop, \
             patch.object(client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(client, "sleep_random"):

            done = coins.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            self.assertEqual(coins.status, "SUCCESS")
            self.assertEqual(coins.working_platform, "wireless")
            self.assertIn("android", tried_platforms)
            self.assertIn("wireless", tried_platforms)

            complete_calls = [c for c in mock_mtop.call_args_list if c[0][0] == TASK_PROCESS_COMPLETE_API]
            self.assertTrue(len(complete_calls) > 0)
            self.assertEqual(complete_calls[0][1]["data"]["platform"], "wireless")

            issue_calls = [c for c in mock_mtop.call_args_list if c[0][0] == TASK_PROCESS_ISSUE_API]
            self.assertTrue(len(issue_calls) > 0)
            self.assertEqual(issue_calls[0][1]["data"]["platform"], "wireless")

    def test_task_delivery_query_all_candidates_no_group_match_sets_skipped_status(self):
        """
        Перевіряє, що якщо всі варіанти platform повертають E2-14-3::no group match,
        скрипт встановлює статус SKIPPED (а не FAILED), логує попередження і не робить фейковий перегляд каталогу.
        """
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_1720000000"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        no_group_res = {
            "api": TASK_DELIVERY_QUERY_API,
            "data": {"errorCode": "E2-14-3", "errorMsg": "no group match."},
            "ret": ["E2-14-3::no group match."],
            "v": "1.0"
        }

        with patch.object(mgr, "_call_mtop", return_value=no_group_res) as mock_mtop, \
             patch.object(client, "get") as mock_get:

            done = coins.farm_browse_tasks(count=4)
            self.assertEqual(done, 0)
            self.assertEqual(coins.status, "SKIPPED")

            # Переконуємось, що спробували всі кандидати
            called_platforms = [c[1]["data"]["platform"] for c in mock_mtop.call_args_list if c[0][0] == TASK_DELIVERY_QUERY_API]
            self.assertTrue(len(called_platforms) >= 3)
            self.assertIn("android", called_platforms)
            self.assertIn("wireless", called_platforms)
            self.assertIn("h5", called_platforms)

            # Жоден каталог не відкривався
            for call in mock_get.call_args_list:
                called_url = call[0][0]
                self.assertNotIn("category", called_url)
                self.assertNotIn("women-clothing", called_url)


class TestDetailedFarmerReporting(unittest.TestCase):
    """
    Тести для точного, деталізованого та правильного звіту фермера (GitHub Step Summary та Telegram).
    """

    def setUp(self):
        CoinsFarmManager.reset_dead_urls()

    def tearDown(self):
        CoinsFarmManager.reset_dead_urls()

    def test_account_summary_does_not_synthetically_compute_initial_coins(self):
        from src.notifications import AccountSummary
        summary = AccountSummary(
            name="Acc_Auto",
            checkin_status="SUCCESS",
            coins_earned=50,
            total_coins=1000,
            pc_coins=20,
            mobile_coins=15,
            tasks_coins=15
        )
        self.assertIsNone(summary.initial_coins, "Must not synthetically deduce initial_coins from total_coins - coins_earned")
        self.assertEqual(summary.checkin_coins, 35)

        # When initial_coins is explicitly provided from verified warmup
        summary_explicit = AccountSummary(
            name="Acc_Explicit",
            checkin_status="SUCCESS",
            initial_coins=950,
            coins_earned=50,
            total_coins=1000
        )
        self.assertEqual(summary_explicit.initial_coins, 950)

    def test_account_summary_deduces_checkin_coins_without_channel_breakdown(self):
        from src.notifications import AccountSummary
        summary = AccountSummary(
            name="Acc_Deduce",
            checkin_status="SUCCESS",
            coins_earned=60,
            total_coins=1200,
            tasks_coins=25
        )
        self.assertIsNone(summary.initial_coins)
        self.assertEqual(summary.checkin_coins, 35)

    def test_step_summary_contains_detailed_tables_and_breakdown(self):
        import tempfile
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="VIP_Buyer",
            checkin_status="SUCCESS",
            coins_earned=70,
            streak_days=5,
            total_coins=2070,
            initial_coins=2000,
            pc_status="SUCCESS",
            pc_coins=30,
            mobile_status="SUCCESS",
            mobile_coins=20,
            checkin_coins=50,
            tasks_done=2,
            tasks_coins=20,
            tasks_skipped=1,
            tasks_failed=0,
            task_details=[
                {"task_id": "T1", "title": "Browse 1", "status": "COMPLETED", "coins": 10, "reason": "Переглянуто"},
                {"task_id": "T2", "title": "Browse 2", "status": "COMPLETED", "coins": 10, "reason": "Переглянуто"},
                {"task_id": "T3", "title": "Unsafe Cart", "status": "SKIPPED", "coins": 0, "reason": "Небезпечна дія"}
            ]
        ))

        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIsNotNone(md)
                # Перевірка зведення нагород
                self.assertIn("🪙 Чекін: +50 🪙", md)
                self.assertIn("🛍️ Завдання: +20 🪙 (2 вик.)", md)
                # Перевірка балансу початковий -> фінальний
                self.assertIn("2,000 ➔ 2,070 🪙", md)
                # Перевірка таблиці Dual-Channel
                self.assertIn("Деталізація чекіну по каналах (Dual-Channel)", md)
                self.assertIn("+30 🪙", md)
                self.assertIn("+20 🪙", md)
                # Перевірка таблиці завдань
                self.assertIn("Деталізація інтерактивних завдань (MTOP)", md)
                self.assertIn("Browse 1", md)
                self.assertIn("Browse 2", md)
                self.assertIn("Unsafe Cart", md)
                self.assertIn("Небезпечна дія", md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_telegram_notification_contains_detailed_breakdown(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_bot_token"
        dispatcher.telegram_chat_id = "fake_chat_id"

        dispatcher.add_summary(AccountSummary(
            name="Super_Buyer",
            checkin_status="SUCCESS",
            coins_earned=55,
            streak_days=10,
            total_coins=1555,
            initial_coins=1500,
            pc_status="SUCCESS",
            pc_coins=25,
            mobile_status="SUCCESS",
            mobile_coins=15,
            tasks_done=1,
            tasks_coins=15,
            tasks_skipped=1,
            tasks_failed=0,
            task_details=[
                {"title": "Explore Deals", "status": "COMPLETED", "coins": 15, "reason": "Зараховано"},
                {"title": "Add to Cart", "status": "SKIPPED", "coins": 0, "reason": "Пропущено безпекою"}
            ]
        ))

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            res = dispatcher.send_telegram_notification(detailed=True)
            self.assertTrue(res)
            mock_post.assert_called_once()
            call_text = mock_post.call_args[1]["json"]["text"]

            # Перевірка початкового та фінального балансу
            self.assertIn("1,500", call_text)
            self.assertIn("1,555", call_text)
            self.assertIn("+55", call_text)

            # Перевірка розбивки каналів PC та App
            self.assertIn("🖥️ PC: ✅ Зараховано (+25 🪙)", call_text)
            self.assertIn("📱 App: ✅ Зараховано (+15 🪙)", call_text)

            # Перевірка тасок
            self.assertIn("🛍️ Таски: <b>1</b> вик. (+15 🪙), 1 пропущ.", call_text)
            self.assertIn("Explore Deals", call_text)
            self.assertIn("Add to Cart", call_text)

    def test_telegram_message_truncation_on_extreme_length(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "token"
        dispatcher.telegram_chat_id = "chat"

        # Додаємо велику кількість акаунтів та довгих тасок для перевірки ліміту 4096 символів
        for i in range(25):
            dispatcher.add_summary(AccountSummary(
                name=f"Long_Account_Name_{i}_" + "X" * 40,
                checkin_status="SUCCESS",
                coins_earned=50,
                streak_days=10,
                total_coins=1000 + i,
                tasks_done=3,
                tasks_coins=30,
                task_details=[
                    {"title": f"Very Long Task Description Number {j} for Account {i} " + "Y" * 50, "status": "COMPLETED", "coins": 10, "reason": "Success"}
                    for j in range(3)
                ]
            ))

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            res = dispatcher.send_telegram_notification()
            self.assertTrue(res)
            sent_text = mock_post.call_args[1]["json"]["text"]
            self.assertLessEqual(len(sent_text), 4050)
            self.assertIn("...[текст скорочено через ліміт Telegram]...", sent_text)

    def test_checkin_manager_tracks_per_channel_coins_and_balances(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_detailed_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 25, "streak": 4}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "SUCCESS", "coins": 15, "streak": 4}), \
             patch.object(mgr, "_fetch_coin_balance") as mock_fetch:
            def fake_fetch():
                mgr.total_coins = 500
            mock_fetch.side_effect = fake_fetch

            mgr.initial_coins = 460
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.pc_coins, 25)
            self.assertEqual(mgr.mobile_coins, 15)
            self.assertEqual(mgr.coins_earned, 40)
            self.assertEqual(mgr.final_coins, 500)
            self.assertEqual(mgr.initial_coins, 460)

    def test_coins_farm_manager_records_itemized_tasks(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_tasks_123"})
        mgr = CheckinManager(client)
        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        mock_tasks = [
            {
                "taskId": "task_1",
                "taskTitle": "Browse Deals",
                "taskType": "browse",
                "status": "todo",
                "staySeconds": 15,
                "actionUrl": "https://aliexpress.com/deals",
                "issueHandle": 1
            },
            {
                "taskId": "task_2",
                "taskTitle": "Buy product with credit card",
                "taskType": "buy",
                "status": "todo",
                "actionUrl": "https://aliexpress.com/pay"
            }
        ]

        with patch.object(coins, "fetch_active_tasks", return_value=mock_tasks), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(coins, "_complete_task", return_value=True), \
             patch.object(coins, "_claim_task_reward", return_value=20):

            done = coins.farm_browse_tasks(count=2)
            self.assertEqual(done, 1)
            self.assertEqual(coins.tasks_completed, 1)
            self.assertEqual(coins.tasks_skipped, 1)
            self.assertEqual(coins.task_coins_earned, 20)
            self.assertEqual(len(coins.task_records), 2)
            self.assertEqual(coins.task_records[0]["status"], "SKIPPED")
            self.assertIn("Небезпечна дія", coins.task_records[0]["reason"])
            self.assertEqual(coins.task_records[1]["status"], "COMPLETED")
            self.assertEqual(coins.task_records[1]["coins"], 20)

    def test_telegram_safe_html_truncation_preserves_closed_tags(self):
        from src.notifications import _safe_truncate_html
        import re

        raw = "<b>Header</b>\n" + "<i>Line with <code>code snippet</i> and <b>bold text</b>\n" * 100
        truncated = _safe_truncate_html(raw, max_length=500)
        self.assertLessEqual(len(truncated), 550)
        self.assertIn("...[текст скорочено через ліміт Telegram]...", truncated)

        # Перевірка строгого закриття всіх тегів
        for tag in ["b", "i", "code"]:
            open_count = len(re.findall(rf"<{tag}(?:\s+[^>]*)?>", truncated))
            close_count = len(re.findall(rf"</{tag}>", truncated))
            self.assertEqual(open_count, close_count, f"Тег <{tag}> не збалансовано: відкрито {open_count}, закрито {close_count}!")

    def test_has_captcha_and_has_expired_detected_across_channels(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Acc_Mobile_Captcha",
            checkin_status="SUCCESS",
            pc_status="SUCCESS",
            mobile_status="CAPTCHA_REQUIRED",
            session_valid=True
        ))

        import tempfile
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIn("Виявлено захист від роботів (Alibaba WAF Captcha)", md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_checkin_early_exit_preserves_pc_and_mobile_status(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_exit_123"})
        mgr = CheckinManager(client)

        # Випадок 1: PC повертає CAPTCHA_REQUIRED
        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "CAPTCHA_REQUIRED", "coins": 0, "streak": 0}):
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
            self.assertEqual(mgr.pc_status, "CAPTCHA_REQUIRED")
            self.assertEqual(mgr.mobile_status, "SKIPPED")

        # Випадок 2: PC FAILED, а Mobile повертає CAPTCHA_REQUIRED
        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "FAILED", "coins": 0, "streak": 0}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "CAPTCHA_REQUIRED", "coins": 0, "streak": 0}):
            ok = mgr.run_checkin()
            self.assertFalse(ok)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
            self.assertEqual(mgr.pc_status, "FAILED")
            self.assertEqual(mgr.mobile_status, "CAPTCHA_REQUIRED")

    def test_checkin_manager_preserves_coins_on_already_done(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_already_123"})
        mgr = CheckinManager(client)

        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "ALREADY_DONE", "coins": 20, "streak": 5}), \
             patch.object(mgr, "_checkin_mobile_channel", return_value={"status": "ALREADY_DONE", "coins": 20, "streak": 5}), \
             patch.object(mgr, "_fetch_coin_balance"):
            ok = mgr.run_checkin()
            self.assertTrue(ok)
            self.assertEqual(mgr.status, "ALREADY_DONE")
            self.assertEqual(mgr.pc_coins, 20)
            self.assertEqual(mgr.mobile_coins, 20)
            self.assertEqual(mgr.coins_earned, 20)

    def test_step_summary_includes_error_table_when_account_has_error(self):
        import tempfile
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Broken_Account",
            checkin_status="FAILED",
            session_valid=False,
            error_message="Invalid credentials token signature"
        ))

        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIn("Помилки та попередження", md)
                self.assertIn("Broken_Account", md)
                self.assertIn("Invalid credentials token signature", md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_safe_truncate_html_with_small_max_length_strictly_bounded(self):
        from src.notifications import _safe_truncate_html
        raw = "A" * 300
        truncated = _safe_truncate_html(raw, max_length=100)
        self.assertLessEqual(len(truncated), 100)

        # Перевірка з HTML розміткою
        raw_html = "<b>Header</b>\n<i>" + "word " * 50 + "</i>"
        truncated_html = _safe_truncate_html(raw_html, max_length=120)
        self.assertLessEqual(len(truncated_html), 120)

    def test_safe_truncate_html_avoids_malformed_unclosed_tags_and_void_tags(self):
        from src.notifications import _safe_truncate_html
        # Обрізка на незакритій кутовій дужці
        raw = "<b>Header</b>\n<i>Line with <code"
        truncated = _safe_truncate_html(raw, max_length=25)
        self.assertNotIn("<code</i>", truncated)
        self.assertNotIn("<code", truncated)
        self.assertIn("</i>", truncated)

        # Перевірка void-тегів (не повинні продукувати </br>)
        raw_br = "<b>Line 1</b><br/><b>Line 2</b><br/>" + "long line " * 20
        truncated_br = _safe_truncate_html(raw_br, max_length=90)
        self.assertNotIn("</br>", truncated_br)

    def test_coins_skip_when_checkin_captcha_required(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_captcha_123"})
        mgr = CheckinManager(client)
        mgr.status = "CAPTCHA_REQUIRED"

        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        with patch.object(client, "get") as mock_get, \
             patch.object(client, "post") as mock_post:
            done_coins = coins.farm_browse_tasks(count=4)
            self.assertEqual(done_coins, 0)
            self.assertEqual(coins.status, "SKIPPED")
            self.assertEqual(len(coins.task_records), 1)
            self.assertEqual(coins.task_records[0]["status"], "SKIPPED")
            self.assertIn("капчею WAF", coins.task_records[0]["reason"])

            # Жодних запитів до серверів не надсилалося
            self.assertEqual(mock_get.call_count, 0)
            self.assertEqual(mock_post.call_count, 0)

    def test_coins_skip_when_checkin_session_expired(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_expired_123"})
        mgr = CheckinManager(client)
        mgr.status = "SESSION_EXPIRED"
        mgr.session_valid = False

        coins = CoinsFarmManager(client, checkin_mgr=mgr)

        with patch.object(client, "get") as mock_get, \
             patch.object(client, "post") as mock_post:
            done_coins = coins.farm_browse_tasks(count=4)
            self.assertEqual(done_coins, 0)
            self.assertEqual(coins.status, "SKIPPED")
            self.assertEqual(coins.task_records[0]["status"], "SKIPPED")
            self.assertIn("недійсну сесію", coins.task_records[0]["reason"])

            self.assertEqual(mock_get.call_count, 0)
            self.assertEqual(mock_post.call_count, 0)

    def test_summary_reports_no_accounts_status_when_empty(self):
        from src.notifications import NotificationDispatcher
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "token"
        dispatcher.telegram_chat_id = "chat"

        import tempfile
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                md = dispatcher.generate_step_summary()
                self.assertIn("⚪ Немає акаунтів", md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            dispatcher.send_telegram_notification()
            sent_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("Немає акаунтів", sent_text)

    def test_summary_reports_no_accounts_status_when_empty_under_ci(self):
        """Гарантує, що за відсутності акаунтів сповіщення надсилається і не заглушується як ALREADY_DONE навіть під час CI (push/pull_request)."""
        from src.notifications import NotificationDispatcher
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "token"
        dispatcher.telegram_chat_id = "chat"
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "CI": "true", "GITHUB_EVENT_NAME": "push"}):
            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 200
                res = dispatcher.send_telegram_notification()
                self.assertTrue(res)
                sent_text = mock_post.call_args[1]["json"]["text"]
                self.assertIn("Немає акаунтів", sent_text)

    def test_materials_parsing_extracts_main_title_behavior_config_and_real_url(self):
        """Перевіряє коректний парсинг mainTitle з видаленням \\n, behaviorConfig та реального URL без fallback."""
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        mock_res = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "result": [
                    {
                        "groupId": 548001,
                        "materials": [
                            {
                                "mainTitle": "Browse specialized deals\n",
                                "instanceStatus": 0,
                                "interestCode": "5PX4SF8J",
                                "taskType": 1,
                                "behaviorConfig": json.dumps({"duration": 20, "url": "https://aliexpress.com/deals/special.html"}),
                                "actionUrl": ""
                            },
                            {
                                "mainTitle": "Daily check-in\n",
                                "instanceStatus": 0,
                                "interestCode": "CHECKIN_CODE",
                                "taskType": 0,
                                "actionUrl": ""
                            }
                        ]
                    }
                ]
            }
        }

        with patch.object(checkin_mgr, "_call_mtop", return_value=mock_res):
            tasks = coins_mgr.fetch_active_tasks()
            self.assertEqual(len(tasks), 2)
            t1 = tasks[0]
            self.assertEqual(t1["taskTitle"], "Browse specialized deals")
            self.assertEqual(t1["actionUrl"], "https://aliexpress.com/deals/special.html")
            self.assertEqual(t1["staySeconds"], 20)
            self.assertEqual(t1["groupId"], 548001)
            self.assertEqual(t1["interestCode"], "5PX4SF8J")
            self.assertEqual(t1["instanceStatus"], 0)
            self.assertEqual(t1["taskType"], 1)

            t2 = tasks[1]
            self.assertEqual(t2["taskTitle"], "Daily check-in")
            self.assertEqual(t2["actionUrl"], "")
            self.assertEqual(t2["staySeconds"], 15)

    def test_is_safe_task_allows_type_1_and_3_and_filters_unsafe_and_inactive(self):
        """Перевіряє, що Тип 1 та 3 дозволені, а instanceStatus != 0 та Daily check-in без URL відхиляються."""
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        coins_mgr = CoinsFarmManager(client)

        # 1. Безпечне завдання Type 1 з URL
        task_type_1 = {
            "taskTitle": "Browse items for 15s",
            "taskType": 1,
            "actionUrl": "https://aliexpress.com/category/1000.html",
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertTrue(coins_mgr.is_safe_task(task_type_1))

        # 2. Безпечне завдання Type 3 з URL
        task_type_3 = {
            "taskTitle": "Browse brand channel",
            "taskType": 3,
            "actionUrl": "https://aliexpress.com/channel/top.html",
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertTrue(coins_mgr.is_safe_task(task_type_3))

        # 3. Daily check-in без URL -> має бути відхилено
        daily_checkin = {
            "taskTitle": "Daily check-in",
            "taskType": 0,
            "actionUrl": "",
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(daily_checkin))

        # 4. Неактивне завдання (instanceStatus = 1) -> має бути відхилено
        inactive_task = {
            "taskTitle": "Browse items for 15s",
            "taskType": 1,
            "actionUrl": "https://aliexpress.com/category/1000.html",
            "instanceStatus": 1,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(inactive_task))

        # 5. Завдання з небезпечним ключовим словом (buy / purchase) -> має бути відхилено
        unsafe_task = {
            "taskTitle": "Place order and buy item",
            "taskType": 1,
            "actionUrl": "https://aliexpress.com/item/1.html",
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(unsafe_task))

    def test_complete_and_claim_payloads_include_group_id_and_interest_code(self):
        """Перевіряє включення groupId та interestCode в MTOP-пейлоади complete та claim."""
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        task_data = {
            "taskInstanceId": "inst_task_test",
            "groupId": 548001,
            "interestCode": "5PX4SF8J",
            "taskType": 1,
            "status": "todo"
        }

        with patch.object(checkin_mgr, "_call_mtop", return_value={"ret": ["SUCCESS::ok"], "data": {"success": True, "awardCoins": 15}}) as mock_call:
            ok = coins_mgr._complete_task(task_data)
            self.assertTrue(ok)
            call_kwargs = mock_call.call_args[1]
            self.assertEqual(call_kwargs["data"]["groupId"], 548001)
            self.assertEqual(call_kwargs["data"]["interestCode"], "5PX4SF8J")

            mock_call.reset_mock()
            coins = coins_mgr._claim_task_reward(task_data)
            self.assertEqual(coins, 15)
            call_kwargs_claim = mock_call.call_args[1]
            self.assertEqual(call_kwargs_claim["data"]["groupId"], 548001)
            self.assertEqual(call_kwargs_claim["data"]["interestCode"], "5PX4SF8J")


    def test_extract_coins_from_complete_result_interest_map(self):
        """Перевіряє коректне вилучення монет із реальної структури interestIssueResultMap."""
        # 1. Реальна структура з AliExpress
        res = {
            "api": "mtop.aliexpress.interactive.task.process.complete",
            "data": {
                "completeSuccess": True,
                "instanceStatus": 0,
                "interestIssueResultMap": {
                    "873039490107": {
                        "interestNum": 5,
                        "issueHandleResult": True,
                        "issueObject": '{"fromAccountPointCount":10,"transferPointCount":5}'
                    }
                },
                "timesJoined": 1,
                "timesLimit": 2
            },
            "ret": ["SUCCESS::调用成功"]
        }
        coins = _extract_coins_from_complete_result(res)
        self.assertEqual(coins, 5)

        # 2. Декілька винагород у мапі
        multi_res = {
            "data": {
                "interestIssueResultMap": {
                    "task_1": {"interestNum": 5},
                    "task_2": {"issueObject": '{"transferPointCount": 10}'}
                }
            }
        }
        self.assertEqual(_extract_coins_from_complete_result(multi_res), 15)

        # 3. Fallback структура
        fallback_res = {
            "data": {
                "success": True,
                "awardCoins": 25
            }
        }
        self.assertEqual(_extract_coins_from_complete_result(fallback_res), 25)

    def test_complete_task_auto_extracts_coins_from_interest_map(self):
        """Перевіряє, що _complete_task витягує монети та зберігає результат у task['earnedCoins']."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        task_data = {
            "taskInstanceId": "inst_task_map_1",
            "taskType": 1,
            "status": "todo"
        }
        complete_res = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "completeSuccess": True,
                "instanceStatus": 0,
                "interestIssueResultMap": {
                    "item_1": {
                        "interestNum": 5,
                        "issueHandleResult": True
                    }
                },
                "timesJoined": 1,
                "timesLimit": 2
            }
        }
        with patch.object(checkin_mgr, "_call_mtop", return_value=complete_res):
            ok = coins_mgr._complete_task(task_data)
            self.assertTrue(ok)
            self.assertEqual(task_data.get("earnedCoins"), 5)
            self.assertEqual(coins_mgr.last_complete_coins, 5)

    def test_farm_browse_tasks_auto_claim_skips_process_issue(self):
        """Якщо монети автоматично нараховані у process.complete, _claim_task_reward не викликається."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        safe_task = {
            "taskInstanceId": "inst_auto_claim",
            "taskTitle": "Explore sponsored items",
            "taskType": 1,
            "status": "todo",
            "timesLimit": 1,
            "timesJoined": 0,
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/browse"
        }
        complete_res = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "completeSuccess": True,
                "instanceStatus": 2,
                "interestIssueResultMap": {
                    "item_auto": {
                        "interestNum": 5,
                        "issueHandleResult": True
                    }
                },
                "timesJoined": 1,
                "timesLimit": 1
            }
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[safe_task]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=complete_res), \
             patch.object(coins_mgr, "_claim_task_reward") as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=5)
            self.assertEqual(done, 1)
            self.assertEqual(coins_mgr.completed_tasks, 1)
            self.assertEqual(coins_mgr.task_coins_earned, 5)
            # Переконуємось, що _claim_task_reward НЕ викликався через те, що монети вже нараховані
            mock_claim.assert_not_called()
            self.assertEqual(len(coins_mgr.task_records), 1)
            self.assertEqual(coins_mgr.task_records[0]["coins"], 5)
            self.assertEqual(coins_mgr.task_records[0]["status"], "COMPLETED")

    def test_farm_browse_tasks_multi_run_times_limit(self):
        """Перевіряє багаторазове виконання завдання при timesLimit = 2."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        multi_task = {
            "taskInstanceId": "inst_multi_run",
            "taskTitle": "View Super discounts",
            "taskType": 1,
            "status": "todo",
            "timesLimit": 2,
            "timesJoined": 0,
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/discounts"
        }
        resp1 = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "completeSuccess": True,
                "instanceStatus": 0,
                "interestIssueResultMap": {"item_1": {"interestNum": 5}},
                "timesJoined": 1,
                "timesLimit": 2
            }
        }
        resp2 = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "completeSuccess": True,
                "instanceStatus": 2,
                "interestIssueResultMap": {"item_2": {"interestNum": 5}},
                "timesJoined": 2,
                "timesLimit": 2
            }
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[multi_task]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", side_effect=[resp1, resp2]):

            done = coins_mgr.farm_browse_tasks(count=5)
            self.assertEqual(done, 2)
            self.assertEqual(coins_mgr.completed_tasks, 2)
            self.assertEqual(coins_mgr.task_coins_earned, 10)
            self.assertEqual(len(coins_mgr.task_records), 2)
            self.assertIn("(1/2)", coins_mgr.task_records[0]["title"])
            self.assertEqual(coins_mgr.task_records[0]["coins"], 5)
            self.assertIn("(2/2)", coins_mgr.task_records[1]["title"])
            self.assertEqual(coins_mgr.task_records[1]["coins"], 5)

    def test_main_post_task_balance_refresh(self):
        """Перевіряє оновлення підсумкового балансу монет після виконання тасок."""
        with patch("src.main.initial_random_jitter"), \
             patch("src.main.AliClient") as mock_client_cls, \
             patch("src.main.CheckinManager") as mock_chk_cls, \
             patch("src.main.CoinsFarmManager") as mock_coins_cls, \
             patch("src.main.NotificationDispatcher") as mock_disp_cls, \
             patch("src.main.load_accounts", return_value=[("Acc_1", {"_m_h5_tk": "tk123"})]):

            mock_chk = MagicMock()
            mock_chk.status = "SUCCESS"
            mock_chk.coins_earned = 30
            mock_chk.streak_days = 5
            mock_chk.total_coins = 504
            mock_chk.initial_coins = 474
            mock_chk.session_valid = True
            mock_chk.pc_status = "SUCCESS"
            mock_chk.pc_coins = 0
            mock_chk.mobile_status = "SUCCESS"
            mock_chk.mobile_coins = 30

            def fake_fetch_balance():
                mock_chk.total_coins = 549

            mock_chk._fetch_coin_balance.side_effect = fake_fetch_balance
            mock_chk_cls.return_value = mock_chk

            mock_coins = MagicMock()
            mock_coins.task_coins_earned = 45
            mock_coins.tasks_completed = 9
            mock_coins.tasks_skipped = 2
            mock_coins.tasks_failed = 0
            mock_coins.task_records = []
            mock_coins_cls.return_value = mock_coins

            dispatcher = MagicMock()
            mock_disp_cls.return_value = dispatcher

            main_module.main()

            mock_chk._fetch_coin_balance.assert_called_once()
            self.assertEqual(dispatcher.add_summary.call_count, 1)
            summary_arg = dispatcher.add_summary.call_args[0][0]
            self.assertEqual(summary_arg.initial_coins, 474)
            self.assertEqual(summary_arg.total_coins, 549)
            self.assertEqual(summary_arg.coins_earned, 75)
            self.assertEqual(summary_arg.tasks_coins, 45)

    def test_is_task_success_and_complete_task_reject_illegal_completion(self):
        """Перевіряє, що ILLEGAL_COMPLETION відхиляється як неуспішне і не викликає claim."""
        from src.coins import _is_task_success
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        illegal_res = {
            "api": "mtop.aliexpress.interactive.task.process.complete",
            "data": {
                "errorCode": "ILLEGAL_COMPLETION",
                "errorMsg": "system error,code is :E0-0-1",
                "result": {
                    "errorCode": "ILLEGAL_COMPLETION",
                    "errorMsg": "system error,code is :E0-0-1"
                }
            },
            "ret": ["SUCCESS::调用成功"]
        }
        # 1. _is_task_success обов'язково повертає False
        self.assertFalse(_is_task_success(illegal_res))

        # 2. _complete_task повертає False та позначає illegalCompletion
        task = {
            "taskInstanceId": "inst_game_123",
            "taskTitle": "Items at $0.1 to get",
            "taskType": 3,
            "status": "todo"
        }
        with patch.object(checkin_mgr, "_call_mtop", return_value=illegal_res):
            ok = coins_mgr._complete_task(task)
            self.assertFalse(ok)
            self.assertEqual(task.get("earnedCoins"), 0)
            self.assertTrue(task.get("illegalCompletion"))
            self.assertEqual(task.get("errorCode"), "ILLEGAL_COMPLETION")

        # 3. Прямий виклик _claim_task_reward не надсилає запит, а одразу повертає -1
        with patch.object(checkin_mgr, "_call_mtop") as mock_mtop_claim:
            claimed = coins_mgr._claim_task_reward(task)
            self.assertEqual(claimed, -1)
            mock_mtop_claim.assert_not_called()

    def test_farm_browse_tasks_illegal_completion_never_calls_claim_and_fails_safely(self):
        """При отриманні ILLEGAL_COMPLETION у farm_browse_tasks клейм ніколи не викликається."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        game_task = {
            "taskInstanceId": "inst_game_fail",
            "taskTitle": "Browse items for 15s",
            "taskType": 0,
            "status": "todo",
            "timesLimit": 1,
            "timesJoined": 0,
            "staySeconds": 1,
            "actionUrl": "https://aliexpress.com/game",
            "issueHandle": 1
        }
        illegal_res = {
            "api": "mtop.aliexpress.interactive.task.process.complete",
            "data": {
                "errorCode": "ILLEGAL_COMPLETION",
                "result": {"errorCode": "ILLEGAL_COMPLETION"}
            },
            "ret": ["SUCCESS::调用成功"]
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[game_task]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=illegal_res), \
             patch.object(coins_mgr, "_claim_task_reward") as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 0)
            self.assertEqual(coins_mgr.tasks_failed, 1)
            self.assertEqual(coins_mgr.tasks_completed, 0)
            mock_claim.assert_not_called()
            self.assertEqual(len(coins_mgr.task_records), 1)
            self.assertEqual(coins_mgr.task_records[0]["status"], "FAILED")
            self.assertEqual(coins_mgr.task_records[0]["coins"], 0)

    def test_is_safe_task_rejects_mini_games_with_task_tag_id_or_type_30(self):
        """Перевіряє фільтрацію зовнішніх міні-ігор (taskTagId або rawType=30) та instanceStatus=2."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        coins_mgr = CoinsFarmManager(client)

        # 1. Полив Prize Land (taskTagId: watering2.0)
        task_prize_land = {
            "taskTitle": "Items at $0.1 to get",
            "taskTagId": "watering2.0",
            "behaviorConfig": {"taskTagId": "watering2.0"},
            "rawType": 30,
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(task_prize_land))

        # 2. GoGo Match (taskTagId: GOGOMATCH_WIN_GAME)
        task_gogo = {
            "taskTitle": "Complete 1 level of the GoGo Match game",
            "behaviorConfig": {"taskTagId": "GOGOMATCH_WIN_GAME"},
            "rawType": 30,
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(task_gogo))

        # 3. Daily quiz (taskTagId: coinsQuestionTask)
        task_quiz = {
            "taskTitle": "Daily quiz challenge",
            "behaviorConfig": {"taskTagId": "coinsQuestionTask"},
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(task_quiz))

        # 4. Write reviews (taskTagId: Review_Finish)
        task_reviews = {
            "taskTitle": "Write reviews",
            "behaviorConfig": {"taskTagId": "Review_Finish"},
            "instanceStatus": 0,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(task_reviews))

        # 5. Вже виконане сьогодні раніше завдання (instanceStatus = 2)
        task_already_done = {
            "taskTitle": "Browse recently viewed items",
            "browseZeroFlag": True,
            "instanceStatus": 2,
            "status": "todo"
        }
        self.assertFalse(coins_mgr.is_safe_task(task_already_done))

        # 6. Справжнє завдання перегляду з browseZeroFlag = True
        task_real_browse = {
            "taskTitle": "Explore sponsored items",
            "browseZeroFlag": True,
            "instanceStatus": 0,
            "status": "todo",
            "actionUrl": "https://m.aliexpress.com/recommends"
        }
        self.assertTrue(coins_mgr.is_safe_task(task_real_browse))

    def test_farm_browse_tasks_skips_mini_games_and_records_already_done_accurately(self):
        """Перевіряє коректне маркування пропущених ігор та вже виконаних завдань у task_records."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        sample_tasks = [
            # 1. Гра 1
            {
                "taskId": "game_1",
                "taskTitle": "Items at $0.1 to get",
                "taskTagId": "watering2.0",
                "behaviorConfig": {"taskTagId": "watering2.0"},
                "instanceStatus": 0,
                "status": "todo"
            },
            # 2. Гра 2
            {
                "taskId": "game_2",
                "taskTitle": "GoGo Match game",
                "behaviorConfig": {"taskTagId": "GOGOMATCH_WIN_GAME"},
                "rawType": 30,
                "instanceStatus": 0,
                "status": "todo"
            },
            # 3. Вже виконане раніше (instanceStatus = 2)
            {
                "taskId": "done_1",
                "taskTitle": "Browse recently viewed items",
                "browseZeroFlag": True,
                "instanceStatus": 2,
                "status": "todo"
            },
            # 4. Реальне завдання перегляду
            {
                "taskId": "browse_ok",
                "taskTitle": "Explore sponsored items",
                "browseZeroFlag": True,
                "instanceStatus": 0,
                "status": "todo",
                "timesLimit": 1,
                "timesJoined": 0,
                "staySeconds": 15,
                "actionUrl": "https://m.aliexpress.com/browse"
            }
        ]
        complete_res = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": {
                    "completeSuccess": True,
                    "instanceStatus": 2,
                    "interestIssueResultMap": {
                        "reward_1": {
                            "interestNum": 5,
                            "issueHandleResult": True
                        }
                    }
                }
            }
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=sample_tasks), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=complete_res):

            done = coins_mgr.farm_browse_tasks(count=5)
            self.assertEqual(done, 1)
            self.assertEqual(coins_mgr.tasks_completed, 1)
            self.assertEqual(coins_mgr.tasks_skipped, 2)
            self.assertEqual(coins_mgr.tasks_already_done, 1)
            self.assertEqual(coins_mgr.tasks_failed, 0)
            self.assertEqual(coins_mgr.task_coins_earned, 5)

            # Перевіряємо статус та причину для кожного запису
            records_by_title = {r["title"]: r for r in coins_mgr.task_records}
            self.assertEqual(records_by_title["Items at $0.1 to get"]["status"], "SKIPPED")
            self.assertEqual(records_by_title["Items at $0.1 to get"]["reason"], "Зовнішня дія / міні-гра")

            self.assertEqual(records_by_title["GoGo Match game"]["status"], "SKIPPED")
            self.assertEqual(records_by_title["GoGo Match game"]["reason"], "Зовнішня дія / міні-гра")

            self.assertEqual(records_by_title["Browse recently viewed items"]["status"], "ALREADY_DONE")
            self.assertIn("Вже виконано сьогодні раніше", records_by_title["Browse recently viewed items"]["reason"])

            self.assertEqual(records_by_title["Explore sponsored items"]["status"], "COMPLETED")
            self.assertEqual(records_by_title["Explore sponsored items"]["coins"], 5)

    def test_notifications_display_already_done_tasks(self):
        """Перевіряє, що GITHUB_STEP_SUMMARY відображає іконку ℹ️ Вже виконано для ALREADY_DONE."""
        from src.notifications import NotificationDispatcher, AccountSummary
        import tempfile
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Test_Account",
            checkin_status="SUCCESS",
            session_valid=True,
            task_details=[
                {"task_id": "T1", "title": "Recap", "status": "ALREADY_DONE", "coins": 0, "reason": "Вже виконано сьогодні раніше"},
                {"task_id": "T2", "title": "Mini Game", "status": "SKIPPED", "coins": 0, "reason": "Зовнішня дія / міні-гра"},
                {"task_id": "T3", "title": "Browse Deals", "status": "COMPLETED", "coins": 5, "reason": "Успішно зараховано (+5 🪙)"}
            ]
        ))
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tf:
            temp_path = tf.name
        try:
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": temp_path}):
                summary_md = dispatcher.generate_step_summary()
                self.assertIn("ℹ️ Вже виконано", summary_md)
                self.assertIn("⏭️ Пропущено", summary_md)
                self.assertIn("✅ Виконано", summary_md)
                self.assertIn("Зовнішня дія / міні-гра", summary_md)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestCoinsFarmManagerRefinements(unittest.TestCase):
    def setUp(self):
        CoinsFarmManager.reset_dead_urls()

    def tearDown(self):
        CoinsFarmManager.reset_dead_urls()

    def test_server_instance_status_and_joined_from_result_block(self):
        """Перевіряє коректне зчитування instanceStatus, timesJoined та timesLimit з data.result після _complete_task."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        task = {
            "taskInstanceId": "inst_multi_res",
            "taskTitle": "Browse items for 15s",
            "taskType": 0,
            "status": "todo",
            "timesLimit": 2,
            "timesJoined": 0,
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/browse"
        }
        # Сервер повертає завершення (instanceStatus=2, timesJoined=2, timesLimit=2) усередині data.result
        complete_res = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "result": {
                    "completeSuccess": True,
                    "instanceStatus": 2,
                    "timesJoined": 2,
                    "timesLimit": 2,
                    "interestIssueResultMap": {"item_1": {"interestNum": 5}}
                }
            }
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[task]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=complete_res):

            done = coins_mgr.farm_browse_tasks(count=5)
            self.assertEqual(done, 1)  # Завдання виконалось 1 раз і зупинилось, бо сервер повернув instanceStatus=2
            self.assertEqual(coins_mgr.completed_tasks, 1)
            self.assertEqual(coins_mgr.task_coins_earned, 5)

    def test_complete_task_resets_mutated_fields_and_logs_warning(self):
        """Перевіряє, що _complete_task скидає поля earnedCoins, completeSuccess, errorCode та pop-ить попередні результати."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        task = {
            "taskInstanceId": "inst_reset_test",
            "earnedCoins": 50,
            "completeSuccess": True,
            "illegalCompletion": True,
            "errorCode": "FAIL",
            "completeResult": {"data": "old"}
        }

        with patch.object(checkin_mgr, "_call_mtop", side_effect=Exception("Network error")):
            with patch("src.coins.logger.warning") as mock_warn:
                ok = coins_mgr._complete_task(task)
                self.assertFalse(ok)
                self.assertEqual(task.get("earnedCoins"), 0)
                self.assertFalse(task.get("completeSuccess"))
                self.assertNotIn("illegalCompletion", task)
                self.assertNotIn("errorCode", task)
                self.assertNotIn("completeResult", task)
                mock_warn.assert_called_once()
                self.assertIn("Network error", str(mock_warn.call_args))

    def test_claim_task_reward_only_called_when_manual_claim_needed(self):
        """Перевіряє, що _claim_task_reward викликається лише при issueHandle==1 або unIssueRecord, і не викликається при issueHandle==0."""
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        # 1. Завдання з issueHandle == 0 (автоматичне нарахування) та earned == 0 -> клейм НЕ викликається
        task_auto = {
            "taskInstanceId": "inst_auto",
            "taskTitle": "Browse items for 15s",
            "taskType": 0,
            "status": "todo",
            "timesLimit": 1,
            "timesJoined": 0,
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/browse",
            "issueHandle": 0
        }
        # process.complete повертає успіх, але 0 монет
        res_complete_0 = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"result": {"completeSuccess": True}}
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[task_auto]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=res_complete_0), \
             patch.object(coins_mgr, "_claim_task_reward") as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            mock_claim.assert_not_called()

        # 2. Завдання з issueHandle == 1 -> клейм ВИКЛИКАЄТЬСЯ
        task_manual = {
            "taskInstanceId": "inst_manual",
            "taskTitle": "Browse items for 15s",
            "taskType": 0,
            "status": "todo",
            "timesLimit": 1,
            "timesJoined": 0,
            "staySeconds": 15,
            "actionUrl": "https://aliexpress.com/browse",
            "issueHandle": 1
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=[task_manual]), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=res_complete_0), \
             patch.object(coins_mgr, "_claim_task_reward", return_value=15) as mock_claim:

            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            mock_claim.assert_called_once()
            self.assertEqual(coins_mgr.task_coins_earned, 15)

    def test_is_safe_task_stricter_whitelist_and_string_behavior_config(self):
        """Перевіряє, що тип 1 без safe-ознак відхиляється, behaviorConfig у вигляді str коректно парситься, а таски без URL відхиляються."""
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        # Тип 1 без ключових слів перегляду і без browseZeroFlag -> відхиляється
        task_generic_type1 = {
            "taskTitle": "Mystery Box Activity",
            "taskType": 1,
            "actionUrl": "https://aliexpress.com/mystery",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(task_generic_type1))

        # behaviorConfig як JSON-рядок з taskTagId (міні-гра) -> відхиляється
        task_str_cfg = {
            "taskTitle": "Water the plant",
            "behaviorConfig": '{"taskTagId": "watering2.0"}',
            "actionUrl": "https://aliexpress.com/game",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(task_str_cfg))

        # Завдання без actionUrl (не waitforreceive) -> відхиляється
        task_no_url = {
            "taskTitle": "Browse items for 15s",
            "actionUrl": "",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(task_no_url))

    def test_is_safe_task_word_boundaries_dont_block_safe_urls(self):
        """Перевіряє, що легітимні підрядки (border_radius, data_binding) не блокуються, але роздільники _/- (card_order, add_to_cart, follow_shop) блокуються."""
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        safe_url_task = {
            "taskTitle": "Explore sponsored items",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/category?border_radius=4&binding=data_binding",
            "status": "todo"
        }
        self.assertTrue(mgr.is_safe_task(safe_url_task))

        # Підрядки з роздільниками _ або - (card_order, add_to_cart, follow_shop) блокуються фільтром безпеки
        for unsafe_fragment in ("layout=card_order", "action=add_to_cart", "type=follow_shop"):
            blocked_task = {
                "taskTitle": "Explore items",
                "taskType": 0,
                "actionUrl": f"https://aliexpress.com/category?{unsafe_fragment}",
                "status": "todo"
            }
            self.assertFalse(mgr.is_safe_task(blocked_task), f"{unsafe_fragment} must be blocked by separator-normalized safety filter")

        # Точний збіг забороненого шляху /order або /cart блокується
        unsafe_url_task = {
            "taskTitle": "Explore items",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/order/confirmation",
            "status": "todo"
        }
        self.assertFalse(mgr.is_safe_task(unsafe_url_task))

    def test_farm_browse_tasks_default_count_15_and_session_limit_marks_skipped(self):
        """Перевіряє дефолт count=15 та маркування невиконаних безпечних завдань як SKIPPED при досягненні ліміту."""
        import inspect
        client = AliClient(cookies={"_m_h5_tk": "tk123"})
        checkin_mgr = CheckinManager(client)
        coins_mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        sig = inspect.signature(coins_mgr.farm_browse_tasks)
        self.assertEqual(sig.parameters["count"].default, 15)

        tasks = [
            {
                "taskId": f"t_{i}",
                "taskTitle": f"Browse deal #{i}",
                "taskType": 0,
                "status": "todo",
                "timesLimit": 1,
                "timesJoined": 0,
                "staySeconds": 15,
                "actionUrl": f"https://aliexpress.com/item/{i}.html",
                "issueHandle": 0
            }
            for i in range(1, 4)
        ]
        complete_res = {
            "ret": ["SUCCESS::调用成功"],
            "data": {"result": {"completeSuccess": True, "interestIssueResultMap": {"it": {"interestNum": 5}}}}
        }
        with patch.object(coins_mgr, "fetch_active_tasks", return_value=tasks), \
             patch.object(client, "get"), \
             patch.object(client, "sleep_random"), \
             patch.object(checkin_mgr, "_call_mtop", return_value=complete_res):

            # Встановлюємо count=1, тому t_1 виконається, а t_2 і t_3 мають потрапити у task_records як SKIPPED
            done = coins_mgr.farm_browse_tasks(count=1)
            self.assertEqual(done, 1)
            self.assertEqual(coins_mgr.completed_tasks, 1)
            self.assertEqual(coins_mgr.tasks_completed, 1)
            self.assertEqual(coins_mgr.tasks_skipped, 2)
            self.assertEqual(len(coins_mgr.task_records), 3)

            self.assertEqual(coins_mgr.task_records[0]["task_id"], "t_1")
            self.assertEqual(coins_mgr.task_records[0]["status"], "COMPLETED")

            self.assertEqual(coins_mgr.task_records[1]["task_id"], "t_2")
            self.assertEqual(coins_mgr.task_records[1]["status"], "SKIPPED")
            self.assertEqual(coins_mgr.task_records[1]["reason"], "Перевищено ліміт завдань на сесію")

            self.assertEqual(coins_mgr.task_records[2]["task_id"], "t_3")
            self.assertEqual(coins_mgr.task_records[2]["status"], "SKIPPED")
            self.assertEqual(coins_mgr.task_records[2]["reason"], "Перевищено ліміт завдань на сесію")

    def test_classify_task_non_numeric_task_type_and_raw_type_not_treated_as_zero(self):
        """Перевіряє, що нечислові значення taskType або rawType (наприклад 'quiz', 'custom_action') не вважаються 0."""
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        task_non_numeric = {
            "taskId": "t_custom",
            "taskTitle": "Some Random Action",
            "taskType": "custom_action",
            "rawType": "special",
            "actionUrl": "https://aliexpress.com/custom",
            "status": "todo"
        }
        is_safe, category, reason = mgr.classify_task(task_non_numeric)
        self.assertFalse(is_safe)
        self.assertEqual(category, "SKIPPED")
        self.assertIn("непідтримуваний тип", reason)

        task_safe_zero = {
            "taskId": "t_safe_0",
            "taskTitle": "Daily activity",
            "taskType": 0,
            "actionUrl": "https://aliexpress.com/browse",
            "status": "todo"
        }
        is_safe, category, reason = mgr.classify_task(task_safe_zero)
        self.assertTrue(is_safe)
        self.assertEqual(category, "SAFE_BROWSE")

        task_safe_raw_zero = {
            "taskId": "t_safe_raw_0",
            "taskTitle": "Daily activity",
            "rawType": "0",
            "actionUrl": "https://aliexpress.com/browse",
            "status": "todo"
        }
        is_safe, category, reason = mgr.classify_task(task_safe_raw_zero)
        self.assertTrue(is_safe)
        self.assertEqual(category, "SAFE_BROWSE")


class TestTelegramNotificationRefinements(unittest.TestCase):
    def test_grouping_multi_run_completed_tasks_in_telegram_report(self):
        """Перевіряє групування виконаних раундів завдань (наприклад 1/3, 2/3, 3/3) в один рядок (×3)."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"

        task_records = [
            {"title": "View Super discounts (1/3)", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "View Super discounts (2/3)", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "View Super discounts (3/3)", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "Explore sponsored items (1/2)", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "Explore sponsored items (2/2)", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "Browse Coins deals", "status": "COMPLETED", "coins": 5, "reason": "Зараховано"},
            {"title": "GoGo Match game", "status": "SKIPPED", "coins": 0, "reason": "Зовнішня дія / міні-гра"}
        ]

        dispatcher.add_summary(AccountSummary(
            name="MultiRun_Account",
            checkin_status="SUCCESS",
            coins_earned=30,
            tasks_done=6,
            tasks_coins=30,
            task_details=task_records
        ))

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            ok = dispatcher.send_telegram_notification(detailed=True)
            self.assertTrue(ok)
            call_text = mock_post.call_args[1]["json"]["text"]

            # Перевіряємо компактне групування
            self.assertIn("• ✅ <i>View Super discounts (×3)</i> (+15 🪙)", call_text)
            self.assertIn("• ✅ <i>Explore sponsored items (×2)</i> (+10 🪙)", call_text)
            self.assertIn("• ✅ <i>Browse Coins deals</i> (+5 🪙)", call_text)
            self.assertNotIn("View Super discounts (1/3)", call_text)

    def test_separating_already_done_from_skipped_in_task_summary(self):
        """Перевіряє виведення 'X вже вик.' окремо від 'Y пропущ.' у рядку 🛍️ Таски."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"

        task_records = [
            {"title": "Browse Deals", "status": "COMPLETED", "coins": 5},
            {"title": "View Item 1", "status": "ALREADY_DONE", "coins": 0, "reason": "Вже виконано сьогодні"},
            {"title": "View Item 2", "status": "ALREADY_DONE", "coins": 0, "reason": "Вже виконано сьогодні"},
            {"title": "Quiz Game", "status": "SKIPPED", "coins": 0, "reason": "Зовнішня дія"}
        ]

        dispatcher.add_summary(AccountSummary(
            name="Account_AD",
            checkin_status="SUCCESS",
            coins_earned=5,
            tasks_done=1,
            tasks_coins=5,
            task_details=task_records
        ))

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            dispatcher.send_telegram_notification()
            call_text = mock_post.call_args[1]["json"]["text"]

            # Перевіряємо відокремлення
            self.assertIn("🛍️ Таски: <b>1</b> вик. (+5 🪙), 2 вже вик., 1 пропущ.", call_text)

    def test_balance_change_honest_display_when_diff_differs_from_run_rewards(self):
        """Зміна балансу та підтверджені нагороди за запуск можуть відрізнятися."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"

        # Баланс зріс на 25 (539 -> 564), хоча підтверджені нагороди становлять 55.
        dispatcher.add_summary(AccountSummary(
            name="Repeat_Run_Account",
            checkin_status="SUCCESS",
            coins_earned=55,
            initial_coins=539,
            total_coins=564,
            tasks_done=5,
            tasks_coins=25
        ))

        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            dispatcher.send_telegram_notification()
            call_text = mock_post.call_args[1]["json"]["text"]

            expected_snippet = "💰 Баланс: <code>539</code> ➔ <b>564 🪙</b> (зміна балансу: <b>+25</b>, нагороди за запуск: <b>+55</b>)"
            self.assertIn(expected_snippet, call_text)

    def test_safe_truncate_html_cleans_empty_paired_tags(self):
        """Перевіряє, що _safe_truncate_html очищує порожні парні теги типу <b></b> на межі скорочення."""
        from src.notifications import _safe_truncate_html
        raw = "<b>Header</b>\n" + "<b>Very long text " * 30 + "</b>"
        truncated = _safe_truncate_html(raw, max_length=120)
        self.assertNotIn("<b></b>", truncated)
        self.assertNotIn("<i></i>", truncated)
        self.assertLessEqual(len(truncated), 120)

    def test_telegram_concise_mode_default_omits_bullet_points_and_includes_fiat(self):
        """Перевіряє, що за замовчуванням звіт у Telegram стислий (без переліку тасок) та містить еквівалент у USD і UAH."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"

        task_records = [
            {"title": "Explore Deals", "status": "COMPLETED", "coins": 15, "reason": "Зараховано"},
            {"title": "GoGo Match", "status": "SKIPPED", "coins": 0, "reason": "Міні-гра"}
        ]

        dispatcher.add_summary(AccountSummary(
            name="Primary_Account",
            checkin_status="SUCCESS",
            coins_earned=55,
            streak_days=7,
            total_coins=1555,
            initial_coins=1500,
            pc_status="SUCCESS",
            pc_coins=25,
            mobile_status="SUCCESS",
            mobile_coins=15,
            tasks_done=1,
            tasks_coins=15,
            tasks_skipped=1,
            task_details=task_records
        ))

        with patch.dict(os.environ, {"USD_TO_UAH_RATE": "40.0"}), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            ok = dispatcher.send_telegram_notification()
            self.assertTrue(ok)
            call_text = mock_post.call_args[1]["json"]["text"]

            # 1. Основна інформація присутня
            self.assertIn("🪙 Зароблено за запуск: <b>+55 🪙</b> (<b>+$0.55 ≈ 22.0 грн</b>)", call_text)
            self.assertIn("💰 Баланс: <code>1,500</code> ➔ <b>1,555 🪙</b> (<b>+55</b>) • <b>$15.55 ≈ 622.0 грн</b>", call_text)
            self.assertIn("🛍️ Таски: <b>1</b> вик. (+15 🪙), 1 пропущ.", call_text)

            # 2. Рутинні кулі відсутні (стислий режим)
            self.assertNotIn("• ✅", call_text)
            self.assertNotIn("• ⏭️", call_text)
            self.assertNotIn("Explore Deals", call_text)
            self.assertNotIn("GoGo Match", call_text)

    def test_get_usd_to_uah_rate_env_override_and_fallback(self):
        """Перевіряє роботу отримання курсу НБУ, змінної оточення та аварійного значення 42.0."""
        from src.notifications import get_usd_to_uah_rate, _format_fiat_equiv

        # 1. Перевизначення через змінну оточення
        with patch.dict(os.environ, {"USD_TO_UAH_RATE": "45.5"}):
            self.assertEqual(get_usd_to_uah_rate(), 45.5)
            self.assertEqual(_format_fiat_equiv(100, rate=45.5), "$1.00 ≈ 45.5 грн")
            self.assertEqual(_format_fiat_equiv(100, rate=45.5, show_sign=True), "+$1.00 ≈ 45.5 грн")
            self.assertEqual(_format_fiat_equiv(0, rate=45.5, show_sign=True), "$0.00 ≈ 0.0 грн")

        # 2. Аварійний fallback на 42.0 при збої мережі
        with patch.dict(os.environ, {"USD_TO_UAH_RATE": ""}, clear=False), \
             patch("requests.get", side_effect=Exception("Network timeout")):
            rate = get_usd_to_uah_rate(force_refresh=True)
            self.assertEqual(rate, 42.0)

    def test_telegram_detailed_mode_toggled_via_env_var(self):
        """Перевіряє активацію детального режиму через TELEGRAM_DETAILED_TASKS=1."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"

        dispatcher.add_summary(AccountSummary(
            name="Detailed_Account",
            checkin_status="SUCCESS",
            coins_earned=10,
            tasks_done=1,
            tasks_coins=10,
            task_details=[
                {"title": "Special Deals", "status": "COMPLETED", "coins": 10, "reason": "Зараховано"}
            ]
        ))

        with patch.dict(os.environ, {"TELEGRAM_DETAILED_TASKS": "1"}), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            dispatcher.send_telegram_notification()
            call_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("Special Deals", call_text)
            self.assertIn("• ✅ <i>Special Deals</i> (+10 🪙)", call_text)



class TestRefactoringAndBugfixesRegression(unittest.TestCase):
    """Тести регресії для виправлень та рефакторингу багів."""

    def test_waf_blocked_attribute_and_protection_of_success_status(self):
        client = AliClient(cookies={"_m_h5_tk": "token_123"})
        mgr = CheckinManager(client)
        self.assertFalse(mgr.waf_blocked)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "FAIL_SYS_USER_VALIDATE"
        mock_resp.json.return_value = {"ret": ["FAIL_SYS_USER_VALIDATE::need verify"]}

        # 1. Protection: if status is SUCCESS, WAF response does NOT overwrite status with CAPTCHA_REQUIRED
        mgr.status = "SUCCESS"
        with patch.object(client, "post", return_value=mock_resp):
            mgr._call_mtop("some.api", data={})
        self.assertEqual(mgr.status, "SUCCESS")
        self.assertTrue(mgr.waf_blocked)

        # 2. Protection: if status is ALREADY_DONE, WAF response does NOT overwrite status
        mgr.waf_blocked = False
        mgr.status = "ALREADY_DONE"
        with patch.object(client, "post", return_value=mock_resp):
            mgr._call_mtop("some.api", data={})
        self.assertEqual(mgr.status, "ALREADY_DONE")
        self.assertTrue(mgr.waf_blocked)

        # 3. Non-terminal status gets updated to CAPTCHA_REQUIRED
        mgr.status = "UNKNOWN"
        mgr.waf_blocked = False
        with patch.object(client, "post", return_value=mock_resp):
            mgr._call_mtop("some.api", data={})
        self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")
        self.assertTrue(mgr.waf_blocked)

        # 4. run_checkin resets waf_blocked to False
        with patch.object(mgr, "_run_pc_checkin", return_value={"status": "SUCCESS", "coins": 10, "streak": 1}), \
             patch.object(mgr, "_run_mobile_checkin", return_value={"status": "ALREADY_DONE", "coins": 0, "streak": 1}):
            mgr.waf_blocked = True
            mgr.run_checkin()
            self.assertFalse(mgr.waf_blocked)

    def test_mtop_token_refresh_probe_behavior(self):
        client = AliClient(cookies={})
        mgr = CheckinManager(client)

        # Test _refresh_mtop_token directly
        with patch.object(client.session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mgr._refresh_mtop_token()
            mock_get.assert_called_once()
            self.assertIn("mtop.aliexpress.coin.execute", mock_get.call_args[0][0])

        # Test _call_mtop triggering probe when token is missing
        client_no_token = AliClient(cookies={})
        mgr_no_token = CheckinManager(client_no_token)
        with patch.object(mgr_no_token, "_refresh_mtop_token") as mock_probe, \
             patch.object(client_no_token, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ret": ["SUCCESS::ok"], "data": {"success": True}}
            )

            def side_effect_probe():
                client_no_token.session.cookies.set("_m_h5_tk", "refreshed_probe_token")
            mock_probe.side_effect = side_effect_probe

            mgr_no_token._call_mtop("some.api", data={})
            mock_probe.assert_called_once()

    def test_is_checkin_success_explicit_ret_failures(self):
        # Even if data contains misleading keys, ret with FAIL_SYS_* must return False
        resp_validate = {"ret": ["FAIL_SYS_USER_VALIDATE::need verify"], "data": {"success": True}}
        self.assertFalse(is_checkin_success(resp_validate))

        resp_expired = {"ret": ["FAIL_SYS_SESSION_EXPIRED::expired"], "data": {"success": True}}
        self.assertFalse(is_checkin_success(resp_expired))

        resp_token = {"ret": ["FAIL_SYS_TOKEN_EXPIRED::expired"], "data": {"success": True}}
        self.assertFalse(is_checkin_success(resp_token))

        resp_ok = {"ret": ["SUCCESS::接口调用成功"], "data": {"success": True}}
        self.assertTrue(is_checkin_success(resp_ok))

        resp_bare = {"ret": ["SUCCESS::OK"], "data": {}}
        self.assertFalse(is_checkin_success(resp_bare))

    def test_coins_fetch_active_tasks_does_not_default_type_to_zero(self):
        client = AliClient(cookies={"_m_h5_tk": "tk_123"})
        mock_checkin = MagicMock()
        mgr = CoinsFarmManager(client, checkin_mgr=mock_checkin)
        fake_resp = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "result": [
                    {"taskId": "1", "taskTitle": "Task 1", "behaviorConfig": {}},
                    {"taskId": "2", "taskTitle": "Task 2", "behaviorConfig": {"type": 1}}
                ]
            }
        }
        mock_checkin._call_mtop.return_value = fake_resp
        tasks = mgr.fetch_active_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertIsNone(tasks[0]["rawType"])
        self.assertEqual(tasks[1]["rawType"], 1)

    def test_coins_classify_task_prioritizes_claim_only(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)
        # Task that might match unsafe keywords like 'order' or 'purchase', but is claim only (waitforreceive)
        task = {
            "status": "waitforreceive",
            "taskTitle": "Claim bonus order coins",
            "unIssueRecord": True
        }
        is_safe, action_type, reason = mgr.classify_task(task)
        self.assertTrue(is_safe)
        self.assertEqual(action_type, "CLAIM")

    def test_coins_farm_manager_attributes_and_reset(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)
        self.assertEqual(mgr.last_complete_coins, 0)
        self.assertEqual(mgr.last_complete_data, {})

        mgr.tasks_already_done = 10
        with patch.object(mgr, "fetch_active_tasks", return_value=[]):
            mgr.farm_browse_tasks()
            self.assertEqual(mgr.tasks_already_done, 0)

    def test_waf_blocked_skips_downstream_coins(self):
        client = AliClient(cookies={})
        mock_checkin = MagicMock()
        mock_checkin.waf_blocked = True

        coins_mgr = CoinsFarmManager(client, checkin_mgr=mock_checkin)
        with patch.object(coins_mgr, "fetch_active_tasks") as mock_fetch:
            coins = coins_mgr.farm_browse_tasks()
            self.assertEqual(coins, 0)
            mock_fetch.assert_not_called()

    def test_normalize_task_url_and_passive_wait_for_invalid_url(self):
        from src.coins import _normalize_task_url
        self.assertEqual(_normalize_task_url("//aliexpress.com/item.html"), "https://aliexpress.com/item.html")
        self.assertEqual(_normalize_task_url("https://aliexpress.com/item.html"), "https://aliexpress.com/item.html")
        self.assertEqual(_normalize_task_url("http://aliexpress.com/item.html"), "https://aliexpress.com/item.html")
        self.assertIsNone(_normalize_task_url("aecmd://open?url=test"))
        self.assertIsNone(_normalize_task_url("javascript:alert(1)"))
        self.assertIsNone(_normalize_task_url(""))

        # Test passive wait for invalid URL in farm_browse_tasks
        client = AliClient(cookies={})
        mock_checkin = MagicMock()
        mock_checkin.waf_blocked = False
        mock_checkin.status = "SUCCESS"
        mock_checkin.mobile_status = "SUCCESS"
        mock_checkin.session_valid = True
        mgr = CoinsFarmManager(client, checkin_mgr=mock_checkin)
        task = {
            "taskId": "task_1",
            "taskTitle": "Invalid URL Task",
            "taskType": "browse",
            "actionUrl": "aecmd://channel/coins",
            "staySeconds": 15,
            "timesLimit": 1,
            "timesJoined": 0,
            "completeSuccess": True
        }
        with patch.object(mgr, "fetch_active_tasks", return_value=[task]), \
             patch.object(mgr, "classify_task", return_value=(True, "SAFE_BROWSE", "Ok")), \
             patch.object(mgr, "_complete_task", return_value=True) as mock_comp, \
             patch.object(client, "get") as mock_get, \
             patch.object(client, "sleep_random") as mock_sleep:
            res = mgr.farm_browse_tasks()
            mock_get.assert_not_called()
            mock_sleep.assert_called()
            mock_comp.assert_called_once()

    def test_env_int_helper_and_safe_parsing(self):
        from src.config import _env_int
        with patch.dict(os.environ, {"TEST_INT_VAL": "50"}):
            self.assertEqual(_env_int("TEST_INT_VAL", 10), 50)
        with patch.dict(os.environ, {"TEST_INT_VAL": "invalid"}):
            self.assertEqual(_env_int("TEST_INT_VAL", 10), 10)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_env_int("TEST_INT_VAL", 10), 10)

    def test_client_browser_profile_and_proxy_per_account(self):
        # Deterministic profile for account
        c1 = AliClient(cookies={}, account_name="Account_1")
        c2 = AliClient(cookies={}, account_name="Account_1")
        self.assertEqual(c1.user_agent, c2.user_agent)

        # Proxy support for Account_1 via ALI_PROXY_1
        with patch.dict(os.environ, {"ALI_PROXY_1": "http://127.0.0.1:8888"}):
            c_proxy = AliClient(cookies={}, account_name="Account_1")
            self.assertEqual(c_proxy.session.proxies.get("http"), "http://127.0.0.1:8888")
            self.assertEqual(c_proxy.session.proxies.get("https"), "http://127.0.0.1:8888")

    def test_notifications_caution_alert_and_telegram_token_masking(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.add_summary(AccountSummary(
            name="Acc1",
            checkin_status="SESSION_EXPIRED",
            session_valid=False,
            error_message="Expired"
        ))
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": "dummy_summary.md"}), \
             patch("builtins.open", unittest.mock.mock_open()):
            summary_md = dispatcher.generate_step_summary()
            self.assertIn("> [!CAUTION]", summary_md)
            self.assertNotIn("> [!CRITICAL]", summary_md)

        # Telegram token masking
        dispatcher.telegram_bot_token = "123456:SECRET_BOT_TOKEN_ABC"
        dispatcher.telegram_chat_id = "fake_chat"
        with patch("requests.post", side_effect=Exception("Failed to call https://api.telegram.org/bot123456:SECRET_BOT_TOKEN_ABC/sendMessage")), \
             patch("src.notifications.logger.warning") as mock_log_warn:
            dispatcher.send_telegram_notification()
            mock_log_warn.assert_called()
            logged_msg = mock_log_warn.call_args[0][0]
            self.assertIn("***", logged_msg)
            self.assertNotIn("SECRET_BOT_TOKEN_ABC", logged_msg)

    def test_main_sigterm_and_finally_block(self):
        from src.main import _sigterm_handler, main
        with self.assertRaises(SystemExit) as cm:
            _sigterm_handler(15, None)
        self.assertEqual(cm.exception.code, 143)

        # Test finally block executes report dispatching exactly once
        with patch("src.main.load_accounts", return_value=[("Acc1", {"aep_usuc_f": "region=KR"})]), \
             patch("src.main.initial_random_jitter"), \
             patch("src.main.AliClient.sleep_random"), \
             patch("src.main.CheckinManager") as mock_chk_cls, \
             patch("src.main.CoinsFarmManager") as mock_coins_cls, \
             patch("src.main.NotificationDispatcher") as mock_disp_cls:
            mock_disp = mock_disp_cls.return_value
            mock_chk = mock_chk_cls.return_value
            mock_chk.status = "SUCCESS"
            mock_chk.pc_status = "SUCCESS"
            mock_chk.mobile_status = "SUCCESS"
            mock_chk.coins_earned = 10
            mock_chk.pc_coins = 10
            mock_chk.mobile_coins = 0
            mock_chk.current_streak = 2
            mock_chk.total_coins = 100
            mock_chk.waf_blocked = False
            mock_chk.session_valid = True

            mock_coins = mock_coins_cls.return_value
            mock_coins.tasks_done = 1
            mock_coins.tasks_coins = 5
            mock_coins.task_details = []

            main()
            mock_disp.dispatch.assert_called_once()

    def test_mobile_channel_halts_immediately_on_waf_when_pc_succeeded(self):
        client = AliClient(cookies={"_m_h5_tk": "tk123_456"})
        mgr = CheckinManager(client)

        # 1. PC succeeds
        pc_mock = {"status": "SUCCESS", "coins": 10, "streak": 3}
        with patch.object(mgr, "_run_pc_checkin", return_value=pc_mock):
            # 2. In Mobile channel, MOBILE_SIGN_LIST_API hits WAF slider challenge
            waf_mtop_resp = {
                "ret": ["FAIL_SYS_USER_VALIDATE::need verify"],
                "data": {"url": "https://sec.aliexpress.com/verify"}
            }

            mtop_calls = []
            def fake_call_mtop(api, *args, **kwargs):
                mtop_calls.append(api)
                if api == MOBILE_SIGN_LIST_API:
                    mgr.waf_blocked = True
                    return waf_mtop_resp
                return {"ret": ["SUCCESS::ok"], "data": {}}

            with patch.object(mgr, "_call_mtop", side_effect=fake_call_mtop):
                res = mgr.run_checkin()
                self.assertTrue(res)
                # Verify PC status preserved as SUCCESS, mobile status is CAPTCHA_REQUIRED
                self.assertEqual(mgr.status, "SUCCESS")
                self.assertEqual(mgr.pc_status, "SUCCESS")
                self.assertEqual(mgr.mobile_status, "CAPTCHA_REQUIRED")
                self.assertTrue(mgr.waf_blocked)

                # Verify mobile channel halted immediately on MOBILE_SIGN_LIST_API without runaway queries!
                self.assertEqual(mtop_calls, [MOBILE_SIGN_LIST_API])

        # 3. Verify NotificationDispatcher detects CAPTCHA_REQUIRED from mobile_status
        from src.notifications import NotificationDispatcher, AccountSummary
        disp = NotificationDispatcher()
        disp.add_summary(AccountSummary(
            name="Acc1",
            checkin_status=mgr.status,
            pc_status=mgr.pc_status,
            mobile_status=mgr.mobile_status,
            session_valid=True
        ))
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": "dummy.md"}), \
             patch("builtins.open", unittest.mock.mock_open()):
            summary_content = disp.generate_step_summary()
            self.assertIn("Капча", summary_content)

    def test_pc_warmup_sets_waf_blocked(self):
        client = AliClient(cookies={})
        mgr = CheckinManager(client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><script>rgv587_run_script</script></html>"

        with patch.object(client, "get", return_value=mock_resp):
            pc_res = mgr._run_pc_checkin()
            self.assertEqual(pc_res["status"], "CAPTCHA_REQUIRED")
            self.assertTrue(mgr.waf_blocked)
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED")

    def test_fetch_coin_balance_skipped_when_waf_blocked(self):
        client = AliClient(cookies={})
        mgr = CheckinManager(client)
        mgr.waf_blocked = True

        with patch.object(mgr, "_call_mtop") as mock_mtop:
            mgr._fetch_coin_balance()
            mock_mtop.assert_not_called()

    def test_tasks_already_done_not_double_counted_in_tasks_skipped(self):
        client = AliClient(cookies={})
        mock_checkin = MagicMock()
        mock_checkin.waf_blocked = False
        mock_checkin.status = "SUCCESS"
        mock_checkin.mobile_status = "SUCCESS"
        mock_checkin.session_valid = True
        mgr = CoinsFarmManager(client, checkin_mgr=mock_checkin)

        tasks = [
            {"taskId": "t1", "taskTitle": "Done task"},
            {"taskId": "t2", "taskTitle": "Skipped task"}
        ]

        def fake_classify(t):
            if t["taskId"] == "t1":
                return False, "ALREADY_DONE", "Already done"
            return False, "SKIPPED", "Skipped"

        with patch.object(mgr, "fetch_active_tasks", return_value=tasks), \
             patch.object(mgr, "classify_task", side_effect=fake_classify):
            mgr.farm_browse_tasks()
            self.assertEqual(mgr.tasks_already_done, 1)
            self.assertEqual(mgr.tasks_skipped, 1)  # NOT 2!

    def test_telegram_notification_skipped_on_schedule_when_already_done(self):
        """Перевіряє, що фоновий запуск за розкладом не надсилає повідомлення в Telegram, якщо все вже виконано (ALREADY_DONE)."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"
        dispatcher.add_summary(AccountSummary(
            name="Account_1",
            checkin_status="ALREADY_DONE",
            pc_status="ALREADY_DONE",
            mobile_status="ALREADY_DONE",
            coins_earned=50,
            initial_coins=1000,
            total_coins=1000,
            tasks_done=0,
            tasks_coins=0,
            session_valid=True
        ))

        ci_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "SKIP_RANDOM_SLEEP": "0",
            "TELEGRAM_NOTIFY_ALWAYS": "0"
        }
        with patch.dict(os.environ, ci_env), patch("requests.post") as mock_post:
            sent = dispatcher.send_telegram_notification()
            self.assertFalse(sent)
            mock_post.assert_not_called()

    def test_telegram_notification_sent_on_schedule_when_fresh_success(self):
        """Перевіряє, що плановий запуск за розкладом надсилає сповіщення при новому успішному чекіні (SUCCESS)."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher(state_dir=self.enterContext(tempfile.TemporaryDirectory()))
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"
        dispatcher.add_summary(AccountSummary(
            name="Account_1",
            checkin_status="SUCCESS",
            pc_status="SUCCESS",
            mobile_status="SUCCESS",
            coins_earned=50,
            initial_coins=950,
            total_coins=1000,
            tasks_done=1,
            tasks_coins=10,
            session_valid=True
        ))

        ci_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "SKIP_RANDOM_SLEEP": "0",
            "TELEGRAM_NOTIFY_ALWAYS": "0"
        }
        with patch.dict(os.environ, ci_env), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            sent = dispatcher.send_telegram_notification()
            self.assertTrue(sent)
            mock_post.assert_called_once()

    def test_telegram_notification_sent_on_schedule_when_has_problem(self):
        """Перевіряє, що плановий запуск за розкладом надсилає сповіщення, якщо сесія прострочена або потрібна капча."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher(state_dir=self.enterContext(tempfile.TemporaryDirectory()))
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"
        dispatcher.add_summary(AccountSummary(
            name="Account_1",
            checkin_status="SESSION_EXPIRED",
            session_valid=False
        ))

        ci_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "SKIP_RANDOM_SLEEP": "0",
            "TELEGRAM_NOTIFY_ALWAYS": "0"
        }
        with patch.dict(os.environ, ci_env), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            sent = dispatcher.send_telegram_notification()
            self.assertTrue(sent)
            mock_post.assert_called_once()

    def test_telegram_notification_sent_on_manual_dispatch_even_if_already_done(self):
        """Перевіряє, що ручний запуск (workflow_dispatch) завжди надсилає сповіщення в Telegram, навіть якщо ALREADY_DONE."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"
        dispatcher.add_summary(AccountSummary(
            name="Account_1",
            checkin_status="ALREADY_DONE",
            pc_status="ALREADY_DONE",
            mobile_status="ALREADY_DONE",
            coins_earned=50,
            initial_coins=1000,
            total_coins=1000,
            tasks_done=0,
            tasks_coins=0,
            session_valid=True
        ))

        ci_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "SKIP_RANDOM_SLEEP": "1",
            "TELEGRAM_NOTIFY_ALWAYS": "0"
        }
        with patch.dict(os.environ, ci_env), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            sent = dispatcher.send_telegram_notification()
            self.assertTrue(sent)
            mock_post.assert_called_once()

    def test_telegram_notification_sent_when_notify_always_set(self):
        """Перевіряє, що TELEGRAM_NOTIFY_ALWAYS=1 примусово відправляє сповіщення навіть при повторних запусках."""
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "fake_token"
        dispatcher.telegram_chat_id = "fake_chat"
        dispatcher.add_summary(AccountSummary(
            name="Account_1",
            checkin_status="ALREADY_DONE",
            session_valid=True
        ))

        ci_env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "SKIP_RANDOM_SLEEP": "0",
            "TELEGRAM_NOTIFY_ALWAYS": "1"
        }
        with patch.dict(os.environ, ci_env), patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            sent = dispatcher.send_telegram_notification()
            self.assertTrue(sent)
            mock_post.assert_called_once()


class TestMtopTransportModule(unittest.TestCase):
    def test_generate_mtop_sign_calculates_correct_md5(self):
        from src.mtop import generate_mtop_sign
        import hashlib
        token = "test_token_123"
        t = "1670000000000"
        app_key = "12574478"
        data = '{"scene":"UserCoinNum"}'
        expected = hashlib.md5(f"{token}&{t}&{app_key}&{data}".encode("utf-8")).hexdigest()
        self.assertEqual(generate_mtop_sign(token, t, app_key, data), expected)

    def test_mtop_transport_refresh_token(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"aep_usuc_f": "region=KR"})
        transport = MtopTransport(client)
        with patch.object(client.session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            transport.refresh_token()
            mock_get.assert_called_once()
            args, kwargs = mock_get.call_args
            self.assertIn("acs.aliexpress.com", args[0])
            self.assertEqual(kwargs["params"]["api"], "mtop.aliexpress.coin.execute")

    def test_mtop_transport_prepare_params_generates_valid_dict(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        params = transport.prepare_params("mtop.aliexpress.coin.pc.sign.list", data={"test": 1})
        self.assertIsNotNone(params)
        self.assertIn("sign", params)
        self.assertIn("t", params)
        self.assertEqual(params["api"], "mtop.aliexpress.coin.pc.sign.list")
        self.assertEqual(params["v"], "1.0")

    def test_mtop_transport_call_detects_waf(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        waf_resp = MagicMock(status_code=200)
        waf_resp.json.return_value = {"ret": ["FAIL_SYS_USER_VALIDATE::need check"]}
        with patch.object(client, "post", return_value=waf_resp):
            res = transport.call("mtop.aliexpress.coin.pc.sign")
            self.assertTrue(transport.waf_blocked)
            self.assertIsNone(res)

    def test_is_mutation_api_distinguishes_mutations_from_queries(self):
        from src.mtop import is_mutation_api, MOBILE_SIGN_EXECUTE_API, MOBILE_RECEIVE_PRIZE_API, TASK_PROCESS_COMPLETE_API, TASK_PROCESS_ISSUE_API
        # Мутації
        self.assertTrue(is_mutation_api("mtop.aliexpress.coin.pc.sign"))
        self.assertTrue(is_mutation_api("mtop.aliexpress.coin.pc.sign.execute"))
        self.assertTrue(is_mutation_api(MOBILE_SIGN_EXECUTE_API))
        self.assertTrue(is_mutation_api(MOBILE_RECEIVE_PRIZE_API))
        self.assertTrue(is_mutation_api(TASK_PROCESS_COMPLETE_API))
        self.assertTrue(is_mutation_api(TASK_PROCESS_ISSUE_API))

        # Запити на читання (ідемпотентні)
        self.assertFalse(is_mutation_api("mtop.aliexpress.coin.pc.sign.list"))
        self.assertFalse(is_mutation_api("mtop.aliexpress.growth.channel.sign.list"))
        self.assertFalse(is_mutation_api("mtop.aliexpress.growth.channel.sign.querySignList"))
        self.assertFalse(is_mutation_api("mtop.aliexpress.interactive.task.delivery.query"))
        self.assertFalse(is_mutation_api("mtop.aliexpress.coin.execute", data={"scene": "UserCoinNum"}))

    def test_mtop_transport_does_not_retry_mutations_on_http_error(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        with patch.object(client, "post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="Internal Server Error")
            res = transport.call("mtop.aliexpress.coin.pc.sign", max_retries=3)
            self.assertIsNone(res)
            # Для мутації виконується рівно 1 виклик POST, без повторних спроб
            mock_post.assert_called_once()

    def test_mtop_transport_does_not_retry_mutations_on_exception(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        with patch.object(client, "post", side_effect=Exception("Network Timeout")):
            res = transport.call("mtop.aliexpress.coin.pc.sign", max_retries=3)
            self.assertIsNone(res)

    def test_mtop_transport_retries_queries_on_transient_failure(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        fail_resp = MagicMock(status_code=500, text="Temporary Error")
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {"ret": ["SUCCESS::OK"], "data": {}}
        with patch.object(client, "post", side_effect=[fail_resp, ok_resp]) as mock_post:
            res = transport.call("mtop.aliexpress.coin.pc.sign.list", max_retries=2)
            self.assertIsNotNone(res)
            self.assertEqual(mock_post.call_count, 2)

    def test_mtop_transport_retries_mutation_on_gateway_token_expiry(self):
        from src.mtop import MtopTransport
        client = AliClient(cookies={"_m_h5_tk": "mytoken123_456789"})
        transport = MtopTransport(client)
        token_exp_resp = MagicMock(status_code=200)
        token_exp_resp.json.return_value = {"ret": ["FAIL_SYS_TOKEN_EXPIRED::Session token expired"]}
        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {"ret": ["SUCCESS::OK"], "data": {"success": True}}

        with patch.object(client, "post", side_effect=[token_exp_resp, success_resp]) as mock_post, \
             patch.object(transport, "refresh_token") as mock_refresh:
            res = transport.call("mtop.aliexpress.coin.pc.sign", max_retries=2)
            self.assertIsNotNone(res)
            self.assertEqual(mock_post.call_count, 2)
            mock_refresh.assert_called_once()


class TestClientSecurity(unittest.TestCase):
    def test_client_retry_allowed_methods_excludes_post(self):
        client = AliClient(cookies={"test": "val"})
        adapter = client.session.adapters.get("https://")
        self.assertIsNotNone(adapter)
        self.assertIsNotNone(adapter.max_retries)
        allowed = adapter.max_retries.allowed_methods
        self.assertNotIn("POST", allowed)
        self.assertNotIn("PUT", allowed)
        self.assertIn("GET", allowed)
        self.assertIn("HEAD", allowed)
        self.assertIn("OPTIONS", allowed)

    def test_client_cookies_have_secure_flag(self):
        client = AliClient(cookies="test_cookie=secret_value; another_cookie=123")
        for cookie in client.session.cookies:
            self.assertTrue(cookie.secure)


class TestDoctorAndStateHistory(unittest.TestCase):
    def test_run_doctor_succeeds_with_valid_setup(self):
        from src.main import run_doctor
        with patch("src.main.load_accounts", return_value=[("Account_Test", {"_m_h5_tk": "tk123"})]), \
             patch("requests.get") as mock_get, \
             patch("src.notifications.get_usd_to_uah_rate", return_value=41.5):
            mock_get.return_value = MagicMock(status_code=200)
            exit_code = run_doctor()
            self.assertEqual(exit_code, 0)

    def test_save_history_creates_dir_and_prunes(self):
        import tempfile
        import shutil
        from src.main import save_history
        from src.notifications import AccountSummary

        temp_dir = tempfile.mkdtemp()
        try:
            summary = AccountSummary(
                name="Acc1",
                checkin_status="SUCCESS",
                streak_days=5,
                coins_earned=25,
                coins_earned_this_run=25,
                prior_rewards_today=0,
                total_coins=500,
                balance_verified=True,
                tasks_done=3,
                tasks_coins=15,
                session_valid=True
            )
            saved_file = save_history([summary], state_dir=temp_dir)
            self.assertIsNotNone(saved_file)
            self.assertTrue(os.path.exists(saved_file))

            with open(saved_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["accounts"]["Acc1"]["coins_earned"], 25)
            self.assertEqual(data[0]["accounts"]["Acc1"]["total_coins"], 500)
            self.assertTrue(data[0]["accounts"]["Acc1"]["balance_verified"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_save_history_multiple_runs_same_day_preserves_runs_and_accumulates(self):
        """Перевіряє, що декілька запусків в один день зберігають усі записи у 'runs' і коректно накопичують результат."""
        import tempfile
        import shutil
        from src.main import save_history
        from src.notifications import AccountSummary

        temp_dir = tempfile.mkdtemp()
        try:
            # Запуск 1: ранок (+25 монет з чекіну, +10 з тасків, баланс 500)
            summary_run1 = AccountSummary(
                name="Acc1",
                checkin_status="SUCCESS",
                streak_days=5,
                coins_earned=35,
                coins_earned_this_run=35,
                prior_rewards_today=0,
                total_coins=500,
                balance_verified=True,
                tasks_done=2,
                tasks_coins=10,
                session_valid=True
            )
            save_history([summary_run1], state_dir=temp_dir, run_id="run_morning")

            # Запуск 2: день (чекін ALREADY_DONE, +15 монет з нових тасків, баланс 515)
            summary_run2 = AccountSummary(
                name="Acc1",
                checkin_status="ALREADY_DONE",
                streak_days=5,
                coins_earned=35,
                coins_earned_this_run=15,
                prior_rewards_today=35,
                total_coins=515,
                balance_verified=True,
                tasks_done=1,
                tasks_coins=15,
                session_valid=True
            )
            saved_file = save_history([summary_run2], state_dir=temp_dir, run_id="run_afternoon")
            self.assertIsNotNone(saved_file)

            with open(saved_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(len(data), 1)  # 1 день
            today_entry = data[0]
            self.assertEqual(today_entry["runs_count"], 2)
            self.assertEqual(len(today_entry["runs"]), 2)

            # Перевіряємо знімок першого запуску
            run1 = today_entry["runs"][0]
            self.assertEqual(run1["run_id"], "run_morning")
            self.assertEqual(run1["accounts"]["Acc1"]["coins_earned_this_run"], 35)
            self.assertEqual(run1["accounts"]["Acc1"]["total_coins"], 500)

            # Перевіряємо знімок другого запуску
            run2 = today_entry["runs"][1]
            self.assertEqual(run2["run_id"], "run_afternoon")
            self.assertEqual(run2["accounts"]["Acc1"]["coins_earned_this_run"], 15)
            self.assertEqual(run2["accounts"]["Acc1"]["total_coins"], 515)

            # Перевіряємо денний агрегат
            acc = today_entry["accounts"]["Acc1"]
            self.assertEqual(acc["coins_earned"], 50)  # 35 + 15
            self.assertEqual(acc["coins_earned_this_run"], 15)
            self.assertEqual(acc["total_coins"], 515)
            self.assertEqual(acc["tasks_done"], 3)  # 2 + 1
            self.assertEqual(acc["tasks_coins"], 25)  # 10 + 15
            self.assertEqual(acc["checkin_status"], "SUCCESS")
            self.assertEqual(acc["runs_count"], 2)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_farm_workflow_cache_preserves_state_on_failure(self):
        """Перевіряє, що workflow farm.yml використовує actions/cache/save з if: always() для гарантованого збереження."""
        workflow_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".github", "workflows", "farm.yml")
        with open(workflow_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("actions/cache/restore@v4", content)
        self.assertIn("actions/cache/save@v4", content)
        self.assertIn("if: always()", content)


class TestAuditRemediationVerification(unittest.TestCase):
    """
    Верифікаційні тести виправлень аудиту:
    1. Автоматичний апгрейд HTTP до HTTPS у _normalize_task_url()
    2. Агрегований статус акаунта (OK, PARTIAL, ACTION_REQUIRED, FAILED)
    3. Балансова верифікація та попередження про непідтверджений бонус
    4. Суворий структурний whitelist у classify_task() без евристичного підвищення за заголовком
    5. Нормалізація роздільників у is_safe_task()
    """

    def test_normalize_task_url_upgrades_http_and_rejects_unsafe(self):
        from src.coins import _normalize_task_url
        # HTTP автоматично апгрейдиться до HTTPS
        self.assertEqual(_normalize_task_url("http://aliexpress.com/item/1.html"), "https://aliexpress.com/item/1.html")
        # Schemeless URL //aliexpress.com -> https://aliexpress.com
        self.assertEqual(_normalize_task_url("//aliexpress.com/item/2.html"), "https://aliexpress.com/item/2.html")
        # HTTPS залишається HTTPS
        self.assertEqual(_normalize_task_url("https://aliexpress.com/item/3.html"), "https://aliexpress.com/item/3.html")
        # Небезпечні або непідтримувані протоколи відхиляються
        self.assertIsNone(_normalize_task_url("javascript:alert(1)"))
        self.assertIsNone(_normalize_task_url("ftp://aliexpress.com/item"))
        self.assertIsNone(_normalize_task_url(""))
        self.assertIsNone(_normalize_task_url(None))

    def test_account_summary_aggregate_status_and_has_problem(self):
        from src.notifications import AccountSummary

        # 1. OK - все успішно
        acc_ok = AccountSummary(
            name="Acc_OK",
            checkin_status="SUCCESS",
            tasks_status="SUCCESS",
            session_valid=True
        )
        self.assertEqual(acc_ok.aggregate_status, "OK")
        self.assertFalse(acc_ok.has_problem)

        # 2. ACTION_REQUIRED - потрібна капча або логін
        acc_captcha = AccountSummary(
            name="Acc_Captcha",
            checkin_status="CAPTCHA_REQUIRED",
            tasks_status="NONE",
            session_valid=True
        )
        self.assertEqual(acc_captcha.aggregate_status, "ACTION_REQUIRED")
        self.assertTrue(acc_captcha.has_problem)

        acc_invalid_sess = AccountSummary(
            name="Acc_Expired",
            checkin_status="FAILED",
            tasks_status="NONE",
            session_valid=False
        )
        self.assertEqual(acc_invalid_sess.aggregate_status, "ACTION_REQUIRED")
        self.assertTrue(acc_invalid_sess.has_problem)

        # 3. PARTIAL - чекін ок, але таски впали або часткові
        acc_partial = AccountSummary(
            name="Acc_Partial",
            checkin_status="SUCCESS",
            tasks_status="FAILED",
            session_valid=True
        )
        self.assertEqual(acc_partial.aggregate_status, "PARTIAL")
        self.assertTrue(acc_partial.has_problem)

        # 4. FAILED - все зафейлилось без потреби ручної дії
        acc_failed = AccountSummary(
            name="Acc_Failed",
            checkin_status="FAILED",
            tasks_status="FAILED",
            session_valid=True
        )
        self.assertEqual(acc_failed.aggregate_status, "FAILED")
        self.assertTrue(acc_failed.has_problem)

    def test_notifications_display_unverified_balance_warning(self):
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher()
        dispatcher.telegram_bot_token = "mock_token"
        dispatcher.telegram_chat_id = "mock_chat"

        acc = AccountSummary(
            name="Acc_Unverified",
            checkin_status="SUCCESS",
            streak_days=5,
            coins_earned=35,
            coins_earned_this_run=35,
            total_coins=1000,
            tasks_done=2,
            tasks_coins=15,
            balance_verified=False,
            session_valid=True
        )
        dispatcher.add_summary(acc)

        # Перевірка Step Summary Markdown
        summary_md = dispatcher.format_step_summary()
        self.assertIn("(+15 оцінка)", summary_md)
        self.assertIn("Пост-баланс не верифіковано сервером", summary_md)

        # Перевірка Telegram тексту
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            dispatcher.send_telegram_notification()
            sent_text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("(бонус ~+15 🪙 очікує підтвердження)", sent_text)
            self.assertIn("Пост-баланс не верифіковано сервером", sent_text)

    def test_classify_task_strict_structural_whitelist_no_heuristic_promotion(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        # Завдання з невідомим типом (99), але в заголовку "Browse items for 15s" -> ПОВИННО бути відхилено
        unsafe_task = {
            "taskId": "task_trick",
            "taskTitle": "Browse items for 15s and win coins",
            "taskType": 99,
            "actionUrl": "https://aliexpress.com/item/1.html",
            "status": "todo"
        }
        is_safe, status, reason = mgr.classify_task(unsafe_task)
        self.assertFalse(is_safe, "Невідомий taskType не повинен підвищуватись до безпечного за заголовком!")
        self.assertEqual(status, "SKIPPED")
        self.assertIn("непідтримуваний тип", reason)

    def test_is_safe_task_separator_normalization_blocks_dangerous_actions(self):
        client = AliClient(cookies={})
        mgr = CoinsFarmManager(client)

        # Роздільники: _, -, /, ?, &, =, .
        for url in [
            "https://aliexpress.com/item/1.html?action=add_to_cart",
            "https://aliexpress.com/store/123?click=follow_shop",
            "https://aliexpress.com/order-checkout?type=fast_order",
            "https://aliexpress.com/payment.do?step=pay",
            "https://aliexpress.com/buy-now"
        ]:
            task = {
                "taskId": "test_sep",
                "taskTitle": "View special offer",
                "taskType": 0,
                "actionUrl": url,
                "status": "todo"
            }
            self.assertFalse(mgr.is_safe_task(task), f"URL {url} must be blocked by normalized separator regex")

    def test_mtop_and_checkin_import_architecture(self):
        import src.mtop as mtop_mod
        import src.checkin as checkin_mod
        import src.coins as coins_mod

        # Verify relocation of helper functions to src.mtop
        self.assertTrue(callable(getattr(mtop_mod, "_normalize_res", None)))
        self.assertTrue(callable(getattr(mtop_mod, "is_captcha_required", None)))
        self.assertTrue(callable(getattr(mtop_mod, "is_mtop_token_expired", None)))
        self.assertTrue(callable(getattr(mtop_mod, "generate_mtop_sign", None)))

        # Verify backward compatibility re-exports in src.checkin
        self.assertIs(getattr(checkin_mod, "_normalize_res", None), mtop_mod._normalize_res)
        self.assertIs(getattr(checkin_mod, "is_captcha_required", None), mtop_mod.is_captcha_required)
        self.assertIs(getattr(checkin_mod, "is_mtop_token_expired", None), mtop_mod.is_mtop_token_expired)
        self.assertIs(getattr(checkin_mod, "generate_mtop_sign", None), mtop_mod.generate_mtop_sign)

        # Verify import in src.coins
        self.assertIs(getattr(coins_mod, "is_captcha_required", None), mtop_mod.is_captcha_required)

        # Verify shared constants identity between src.mtop and src.checkin
        self.assertIs(getattr(checkin_mod, "MOBILE_CHECKIN_APIS", None), mtop_mod.MOBILE_CHECKIN_APIS)
        self.assertIs(getattr(checkin_mod, "MOBILE_SIGN_EXECUTE_API", None), mtop_mod.MOBILE_SIGN_EXECUTE_API)
        self.assertIs(getattr(checkin_mod, "COIN_MOBILE_REFERER", None), mtop_mod.COIN_MOBILE_REFERER)
        self.assertIs(getattr(checkin_mod, "MOBILE_ORIGIN", None), mtop_mod.MOBILE_ORIGIN)

    def test_main_save_history_annotations_evaluates_without_name_error(self):
        import src.main
        import typing

        # Verify __annotations__ evaluation (NameError prevention on Python 3.11 / PEP 649)
        annotations = src.main.save_history.__annotations__
        self.assertIn("return", annotations)

        hints = typing.get_type_hints(src.main.save_history)
        self.assertIn("return", hints)
        self.assertEqual(hints["state_dir"], str)

    def test_account_summary_preserves_zero_coins_earned_this_run_on_repeat_checkin(self):
        from src.notifications import AccountSummary
        # When checkin is ALREADY_DONE, coins_earned reflects prior reward (e.g. 50),
        # but coins_earned_this_run is explicitly 0 and prior_rewards_today is 50.
        summary = AccountSummary(
            name="Acc_Repeat",
            checkin_status="ALREADY_DONE",
            coins_earned=50,
            coins_earned_this_run=0,
            prior_rewards_today=50
        )
        self.assertEqual(summary.coins_earned_this_run, 0, "Explicit 0 coins_earned_this_run must not be clobbered by coins_earned!")
        self.assertEqual(summary.prior_rewards_today, 50)
        self.assertEqual(summary.coins_earned, 50)

        # When coins_earned_this_run is not provided (None), it falls back properly
        summary_fresh = AccountSummary(
            name="Acc_Fresh",
            checkin_status="SUCCESS",
            coins_earned=25
        )
        self.assertEqual(summary_fresh.coins_earned_this_run, 25)

        summary_already = AccountSummary(
            name="Acc_Prior",
            checkin_status="ALREADY_DONE",
            coins_earned=30,
            prior_rewards_today=30
        )
        self.assertEqual(summary_already.coins_earned_this_run, 0)

    def test_coins_farm_browse_tasks_halts_on_403_nav_response(self):
        client = AliClient(cookies={"sid": "test"})
        checkin_mgr = CheckinManager(client)
        mgr = CoinsFarmManager(client, checkin_mgr=checkin_mgr)

        tasks = [
            {
                "taskId": "task_403_1",
                "taskTitle": "Browse item 1",
                "taskType": 0,
                "actionUrl": "https://aliexpress.com/item/1.html",
                "status": "todo",
                "staySeconds": 15
            },
            {
                "taskId": "task_403_2",
                "taskTitle": "Browse item 2",
                "taskType": 0,
                "actionUrl": "https://aliexpress.com/item/2.html",
                "status": "todo",
                "staySeconds": 15
            }
        ]

        # First task returns HTTP 403 (Alibaba WAF block on navigation)
        resp_403 = MagicMock(status_code=403, text="<html>RGV587_RUN_SCRIPT</html>")
        with patch.object(client, "sleep_random"), \
             patch.object(mgr, "fetch_active_tasks", return_value=tasks), \
             patch.object(client, "get", return_value=resp_403):
            completed = mgr.farm_browse_tasks(count=2)
            self.assertEqual(completed, 0)
            self.assertTrue(checkin_mgr.waf_blocked, "403 on nav_url must activate WAF block")
            self.assertEqual(mgr.status, "CAPTCHA_REQUIRED", "Task status must transition to CAPTCHA_REQUIRED")



class TestNewCampaignTasks2026(unittest.TestCase):
    """Тести для нових акційних завдань кампанії монет (пошук, огляд переглянутих, рекламовані 0/6)."""

    def setUp(self):
        self.client = AliClient(cookies={"_m_h5_tk": "testtoken_12345"})
        self.checkin_mgr = CheckinManager(self.client)
        self.mgr = CoinsFarmManager(self.client, checkin_mgr=self.checkin_mgr)

    def test_classify_find_favorite_search_task(self):
        """'Знайдіть улюблене' (пошук за ключовими словами) класифікується як безпечний пошук."""
        task = {
            "taskId": "task_search_1",
            "taskTitle": "Знайдіть улюблене",
            "taskType": 1,
            "timesLimit": 2,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": ""
        }
        is_safe, status, reason = self.mgr.classify_task(task)
        self.assertTrue(is_safe)
        self.assertEqual(status, "SAFE_SEARCH")
        self.assertTrue(task.get("_fallback_url", "").startswith("https://www.aliexpress.com/w/wholesale-"))

    def test_classify_recently_viewed_items_task(self):
        """'Огляд недавно переглянутих товарів' класифікується як безпечний перегляд навіть при cart в URL."""
        task = {
            "taskId": "task_recent_1",
            "taskTitle": "Огляд недавно переглянутих товарів",
            "taskType": 0,
            "timesLimit": 1,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": "https://m.aliexpress.com/shopcart/recently_viewed.htm"
        }
        is_safe, status, reason = self.mgr.classify_task(task)
        self.assertTrue(is_safe)
        self.assertEqual(status, "SAFE_BROWSE")

    def test_classify_promoted_products_6_repetitions(self):
        """'Перегляньте рекламовані товари' (0/6) класифікується як безпечний перегляд."""
        task = {
            "taskId": "task_promoted_1",
            "taskTitle": "Перегляньте рекламовані товари",
            "taskType": 0,
            "timesLimit": 6,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": "https://www.aliexpress.com/p/coin-index/index.html"
        }
        is_safe, status, reason = self.mgr.classify_task(task)
        self.assertTrue(is_safe)
        self.assertEqual(status, "SAFE_BROWSE")

    def test_classify_add_to_cart_is_unsafe(self):
        """'Додайте 5 товарів до кошика' категоріально відхиляється як небезпечне завдання."""
        task = {
            "taskId": "task_cart_1",
            "taskTitle": "Додайте 5 товарів до кошика",
            "taskType": 1,
            "timesLimit": 5,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": "https://www.aliexpress.com/p/shopcart/index.html"
        }
        is_safe, status, reason = self.mgr.classify_task(task)
        self.assertFalse(is_safe)
        self.assertEqual(status, "SKIPPED")
        self.assertIn("Небезпечна дія", reason)

    def test_classify_watering_game_is_skipped(self):
        """'Поливайте й виграйте' (міні-гра Земля призів) відхиляється як зовнішня міні-гра."""
        task = {
            "taskId": "task_game_1",
            "taskTitle": "Поливайте й виграйте US $0,10",
            "taskType": 30,
            "rawType": 30,
            "status": "todo",
            "actionUrl": "https://games.aliexpress.com/prize-land.html"
        }
        is_safe, status, reason = self.mgr.classify_task(task)
        self.assertFalse(is_safe)
        self.assertEqual(status, "SKIPPED")
        self.assertIn("Зовнішня дія", reason)

    def test_farm_browse_tasks_executes_find_favorite_multi_runs(self):
        """Виконання пошукового завдання 'Знайдіть улюблене' з 2 повторами (0/2)."""
        task = {
            "taskId": "task_search_1",
            "taskInstanceId": "task_search_1",
            "taskTitle": "Знайдіть улюблене",
            "taskType": 1,
            "timesLimit": 2,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": "",
            "staySeconds": 1
        }
        complete_mtop_res = {
            "ret": ["SUCCESS::OK"],
            "data": {
                "success": True,
                "result": {
                    "completeSuccess": True,
                    "interestIssueResultMap": {
                        "1": {"interestNum": 6}
                    }
                }
            }
        }
        with patch.object(self.client, "sleep_random"), \
             patch.object(self.mgr, "fetch_active_tasks", return_value=[task]), \
             patch.object(self.client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(self.checkin_mgr, "_call_mtop", return_value=complete_mtop_res):
            done = self.mgr.farm_browse_tasks(count=5)
            self.assertEqual(done, 2)
            self.assertEqual(self.mgr.task_coins_earned, 12)
            self.assertEqual(self.mgr.tasks_completed, 2)

    def test_farm_browse_tasks_executes_promoted_products_all_6_runs(self):
        """Виконання завдання 'Перегляньте рекламовані товари' на всі 6 повторів (0/6)."""
        task = {
            "taskId": "task_promoted_1",
            "taskInstanceId": "task_promoted_1",
            "taskTitle": "Перегляньте рекламовані товари",
            "taskType": 0,
            "timesLimit": 6,
            "timesJoined": 0,
            "status": "todo",
            "actionUrl": "https://www.aliexpress.com/p/coin-index/index.html",
            "staySeconds": 1
        }
        complete_mtop_res = {
            "ret": ["SUCCESS::OK"],
            "data": {
                "success": True,
                "result": {
                    "completeSuccess": True,
                    "interestIssueResultMap": {
                        "1": {"interestNum": 6}
                    }
                }
            }
        }
        with patch.object(self.client, "sleep_random"), \
             patch.object(self.mgr, "fetch_active_tasks", return_value=[task]), \
             patch.object(self.client, "get", return_value=MagicMock(status_code=200)), \
             patch.object(self.checkin_mgr, "_call_mtop", return_value=complete_mtop_res):
            done = self.mgr.farm_browse_tasks(count=10)
            self.assertEqual(done, 6)
            self.assertEqual(self.mgr.task_coins_earned, 36)
            self.assertEqual(self.mgr.tasks_completed, 6)


    def test_fetch_active_tasks_fallback_on_scene_not_exist(self):
        """Якщо сконфігурована сцена повертає E2-14-1 (scene not exist), скрипт перемикається на кандидата 11.11 (974)."""
        scene_149_error = {
            "ret": ["E2-14-1::scene not exist."],
            "data": {"errorCode": "E2-14-1", "errorMsg": "scene not exist."}
        }
        scene_974_success = {
            "ret": ["SUCCESS::OK"],
            "data": {
                "success": True,
                "result": [
                    {
                        "taskId": "task_1111_promo",
                        "taskTitle": "Перегляньте рекламовані товари",
                        "materials": [{
                            "actionUrl": "https://www.aliexpress.com/p/coin-index/index.html",
                            "staySeconds": 15,
                            "type": 0,
                            "timesLimit": 6,
                            "timesJoined": 0
                        }]
                    }
                ]
            }
        }

        # Перший виклик повертає помилку старої сцени, другий виклик на сцені 974 успішний
        def side_effect(api, data=None):
            if data and data.get("sceneId") == 149:
                return scene_149_error
            return scene_974_success

        with patch("src.coins.TASK_SCENE_ID", 149), \
             patch.object(self.checkin_mgr, "_call_mtop", side_effect=side_effect):
            tasks = self.mgr.fetch_active_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["taskId"], "task_1111_promo")
            self.assertEqual(self.mgr.working_scene_id, 974)
            self.assertEqual(self.mgr.working_asac, "2E269290M9445GTGEDG4ON")


if __name__ == "__main__":
    unittest.main()







