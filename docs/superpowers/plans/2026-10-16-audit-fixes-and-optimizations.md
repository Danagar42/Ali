# План впровадження: Виправлення дефектів та оптимізація AliExpress Farm

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Усунути критичні дефекти маскування збоїв і телеметрії, оптимізувати час виконання в CI (заощадити до 60 с на акаунт), убезпечити Telegram-сповіщення та додати детекцію капчі й стійкість до збоїв мережі (без включення ідей розвитку /boost).

**Architecture:** Корекція логіки визначення успішності акаунта в `main.py` та `notifications.py`; перенесення фіксації зароблених монет у `checkin.py` виключно після успішного чекіну; безпечне екранування рядків через `html.escape` у `notifications.py`; додавання проксі `ALI_PROXY_1..10` у `.github/workflows/farm.yml`; вимкнення холостих пауз на мертві REST-ендпоінти в `coins.py`/`coupons.py`; додавання `HTTPAdapter` із `Retry` у `client.py` та обробка сигнатур капчі (`RGV587_RUN_SCRIPT`) у `checkin.py`.

**Tech Stack:** Python 3.10+, `requests`, `urllib3`, GitHub Actions workflow.

---

### Task 1: Усунення маскування збоїв (Failure Masking Fix)

**Files:**
- Modify: `src/main.py:115-121`
- Modify: `src/notifications.py:61-66`
- Modify: `src/coins.py:143-147`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `CheckinManager.run_checkin() -> bool`
- `AccountSummary.checkin_status: str`
- `AccountSummary.session_valid: bool`

- [ ] **Step 1: Написати failing test для перевірки, що провалений чекін не зараховується як успіх при перегляді сторінок**

Додати в `tests/test_farmer.py`:
```python
    @patch("src.main.initial_random_jitter")
    @patch.dict(os.environ, {"ALI_COOKIES": "token=test"}, clear=True)
    @patch("src.main.CheckinManager")
    @patch("src.main.CoinsFarmManager")
    @patch("src.main.CouponCollector")
    def test_main_exits_with_error_when_checkin_fails_even_if_browse_tasks_done(
        self, mock_coupons, mock_coins, mock_checkin, mock_jitter
    ):
        instance_checkin = MagicMock()
        instance_checkin.run_checkin.return_value = False  # Чекін провалився
        instance_checkin.session_valid = True
        mock_checkin.return_value = instance_checkin

        instance_coins = MagicMock()
        instance_coins.farm_browse_tasks.return_value = 4  # Зараховано перегляди
        mock_coins.return_value = instance_coins

        with patch("sys.exit") as mock_exit:
            main_module.main()
            mock_exit.assert_called_with(1)
```

- [ ] **Step 2: Запустити тест і переконатися, що він падає**

Run: `python -m unittest tests.test_farmer.TestAuth.test_main_exits_with_error_when_checkin_fails_even_if_browse_tasks_done`
Expected: FAIL (зараз `sys.exit(0)` або не викликається `sys.exit(1)`, оскільки `coins_done > 0` зараховується як успіх).

- [ ] **Step 3: Виправити логіку в `src/main.py`, `src/notifications.py` та `src/coins.py`**

У `src/main.py`:
```python
# Зараховуємо успіх тільки якщо підтверджено чекін (новий або вже виконаний)
if checkin_ok and checkin_mgr.session_valid:
    success_count += 1
    logger.info(f"✅ [{account_name}] Чекін успішно підтверджено.")
else:
    failed_count += 1
    logger.warning(f"⚠️ [{account_name}] Чекін не підтверджено (перевірте валідність cookies або наявність капчі).")
```

У `src/notifications.py`:
```python
successful_accounts = sum(
    1 for s in self.summaries
    if s.checkin_status in ("SUCCESS", "ALREADY_DONE") and s.session_valid
)
```

У `src/coins.py`:
Прибрати фейковий інкремент `completed_or_acknowledged += 1` при `elif page_ok:`, залишивши зарахування тільки при реальній відповіді від API:
```python
if reported:
    completed_or_acknowledged += 1
```

- [ ] **Step 4: Запустити тести і переконатися, що вони проходять**

Run: `python -m unittest discover -s tests -p "test_*.py"`
Expected: PASS

- [ ] **Step 5: Зафіксувати зміни**

```bash
git add src/main.py src/notifications.py src/coins.py tests/test_farmer.py
git commit -m "fix: prevent failure masking when checkin fails but browsing succeeds"
```

---

### Task 2: Виправлення передчасного вилучення телеметрії монет (Coin Leakage Bug)

**Files:**
- Modify: `src/checkin.py:440-480`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `CheckinManager._extract_telemetry(data: Union[dict, str], only_balance: bool = False)`
- `CheckinManager.coins_earned: int`

- [ ] **Step 1: Написати failing test на випадок збою чекіну після виклику `pc.sign.list`**

Додати в `tests/test_farmer.py`:
```python
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
```

- [ ] **Step 2: Запустити тест і переконатися, що він падає**

Run: `python -m unittest tests.test_farmer.TestAuth.test_coins_earned_not_set_if_checkin_fails`
Expected: FAIL (зараз `coins_earned` стає 25 ще до виклику `pc.sign`).

- [ ] **Step 3: Перенести вилучення `coins_earned` виключно на етап підтвердженого успіху**

У `src/checkin.py`:
1. У виклику `_extract_telemetry(status_res)` перед перевіркою `signToday` передавати параметр або не витягувати `coins_earned`, якщо `signToday is False`.
2. Витягувати `coins_earned` лише тоді, коли:
   - `signToday is True` (вже був зарахований раніше), АБО
   - `is_checkin_success(sign_res)` повернув `True`.

- [ ] **Step 4: Запустити тест і переконатися, що він проходить**

Run: `python -m unittest tests.test_farmer.TestAuth.test_coins_earned_not_set_if_checkin_fails`
Expected: PASS

- [ ] **Step 5: Зафіксувати зміни**

```bash
git add src/checkin.py tests/test_farmer.py
git commit -m "fix: ensure coins_earned is only populated upon verified checkin success"
```

---

### Task 3: Безпечне екранування HTML у сповіщеннях Telegram

**Files:**
- Modify: `src/notifications.py:140-165`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `NotificationDispatcher.send_telegram_notification() -> bool`

- [ ] **Step 1: Написати failing test для відправки повідомлення з небезпечними спецсимволами (`<`, `>`, `&`)**

Додати в `tests/test_farmer.py`:
```python
    def test_telegram_html_escaping(self):
        import html
        from src.notifications import NotificationDispatcher, AccountSummary
        dispatcher = NotificationDispatcher(telegram_bot_token="test_token", telegram_chat_id="123")
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
```

- [ ] **Step 2: Запустити тест і переконатися, що він падає**

Run: `python -m unittest tests.test_farmer.TestAuth.test_telegram_html_escaping`
Expected: FAIL

- [ ] **Step 3: Застосувати `html.escape` у `src/notifications.py`**

У `src/notifications.py`:
```python
import html

# При генерації рядка акаунта:
safe_name = html.escape(s.name)
msg_lines.append(f"{status_badge} <b>{safe_name}</b>: {html.escape(detail)}")
```

- [ ] **Step 4: Запустити тест і переконатися, що він проходить**

Run: `python -m unittest tests.test_farmer.TestAuth.test_telegram_html_escaping`
Expected: PASS

- [ ] **Step 5: Зафіксувати зміни**

```bash
git add src/notifications.py tests/test_farmer.py
git commit -m "fix: escape HTML special characters in Telegram notification messages"
```

---

### Task 4: Прокидання секретів проксі `ALI_PROXY_1..10` у CI

**Files:**
- Modify: `.github/workflows/farm.yml:50-57`

**Interfaces:**
- `env: ALI_PROXY_1: ${{ secrets.ALI_PROXY_1 }}` ... `ALI_PROXY_10: ${{ secrets.ALI_PROXY_10 }}`

- [ ] **Step 1: Додати змінні `ALI_PROXY_1`...`ALI_PROXY_10` у блок env workflow**

У `.github/workflows/farm.yml`:
```yaml
          # Підтримка проксі (опційно для окремих акаунтів)
          ALI_PROXY: ${{ secrets.ALI_PROXY }}
          ALI_PROXY_1: ${{ secrets.ALI_PROXY_1 }}
          ALI_PROXY_2: ${{ secrets.ALI_PROXY_2 }}
          ALI_PROXY_3: ${{ secrets.ALI_PROXY_3 }}
          ALI_PROXY_4: ${{ secrets.ALI_PROXY_4 }}
          ALI_PROXY_5: ${{ secrets.ALI_PROXY_5 }}
          ALI_PROXY_6: ${{ secrets.ALI_PROXY_6 }}
          ALI_PROXY_7: ${{ secrets.ALI_PROXY_7 }}
          ALI_PROXY_8: ${{ secrets.ALI_PROXY_8 }}
          ALI_PROXY_9: ${{ secrets.ALI_PROXY_9 }}
          ALI_PROXY_10: ${{ secrets.ALI_PROXY_10 }}
```

- [ ] **Step 2: Перевірити синтаксис YAML**

Переконатися у валідності відступів та структури файлу.

- [ ] **Step 3: Зафіксувати зміни**

```bash
git add .github/workflows/farm.yml
git commit -m "ci: expose individual ALI_PROXY_1..10 secrets in GitHub Actions workflow"
```

---

### Task 5: Оптимізація часу CI: вимкнення холостих пауз на мертві REST-ендпоінти

**Files:**
- Modify: `src/coins.py:40-60, 140-170`
- Modify: `src/coupons.py:60-90`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `CoinsFarmManager.collect_all_coins() -> int`
- `CouponCollector.collect_available_coupons() -> int`

- [ ] **Step 1: Написати тест на швидке завершення модуля завдань без довгих пауз при вимкнених/мертвих ендпоінтах**

У `tests/test_farmer.py` перевірити, що при відсутності активних завдань `CoinsFarmManager` та `CouponCollector` не роблять довгих sleep-пауз.

- [ ] **Step 2: Оптимізувати `src/coins.py` та `src/coupons.py`**

1. У `src/coins.py`: зробити збереження мертвого статусу ендпоінтів на рівні класу або конфігу, щоб не опитувати мертві URL повторно для кожного акаунта.
2. У `src/coupons.py`: перевіряти доступність купонного хабу одним легким HEAD/GET запитом і при отриманні 404/502 одразу повертати `0`, не виконуючи циклічні запити та паузи.

- [ ] **Step 3: Запустити тести**

Run: `python -m unittest discover -s tests -p "test_*.py"`
Expected: PASS, час виконання тестів не збільшується.

- [ ] **Step 4: Зафіксувати зміни**

```bash
git add src/coins.py src/coupons.py tests/test_farmer.py
git commit -m "perf: eliminate unnecessary anti-detect delays on dead REST endpoints"
```

---

### Task 6: Мережева стійкість: налаштування Retry Adapter у AliClient

**Files:**
- Modify: `src/client.py:40-60`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `AliClient.session: requests.Session`
- `HTTPAdapter(max_retries=Retry(...))`

- [ ] **Step 1: Написати test на автоматичний повтор запиту при 502/503/504 статусах**

Додати в `tests/test_farmer.py`:
```python
    def test_client_retry_adapter_on_502(self):
        client = AliClient(cookies={})
        # Перевірити, що adapter змонтовано на http:// та https://
        adapter = client.session.adapters.get("https://")
        self.assertIsNotNone(adapter)
        self.assertIsNotNone(adapter.max_retries)
        self.assertIn(502, adapter.max_retries.status_forcelist)
```

- [ ] **Step 2: Запустити тест і переконатися, що він падає**

Run: `python -m unittest tests.test_farmer.TestAuth.test_client_retry_adapter_on_502`
Expected: FAIL

- [ ] **Step 3: Налаштувати `HTTPAdapter` з `urllib3.util.Retry` у `src/client.py`**

У `src/client.py`:
```python
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

retry_strategy = Retry(
    total=3,
    backoff_factor=1.5,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
self.session.mount("https://", adapter)
self.session.mount("http://", adapter)
```

- [ ] **Step 4: Запустити тест і переконатися, що він проходить**

Run: `python -m unittest tests.test_farmer.TestAuth.test_client_retry_adapter_on_502`
Expected: PASS

- [ ] **Step 5: Зафіксувати зміни**

```bash
git add src/client.py tests/test_farmer.py
git commit -m "feat: add automatic HTTP retry adapter for resilient network connections"
```

---

### Task 7: Детекція слайдера капчі Alibaba WAF

**Files:**
- Modify: `src/checkin.py:85-115`
- Modify: `src/notifications.py:45-55`
- Test: `tests/test_farmer.py`

**Interfaces:**
- `is_captcha_required(response_data: Union[dict, str]) -> bool`
- `CheckinManager.status = "CAPTCHA_REQUIRED"`

- [ ] **Step 1: Написати тест для розпізнавання кодів капчі `RGV587_RUN_SCRIPT` та `FAIL_SYS_USER_VALIDATE`**

Додати в `tests/test_farmer.py`:
```python
    def test_is_captcha_required_detection(self):
        from src.checkin import is_captcha_required
        self.assertTrue(is_captcha_required({"ret": ["RGV587_RUN_SCRIPT::Security verification required"]}))
        self.assertTrue(is_captcha_required({"ret": ["FAIL_SYS_USER_VALIDATE::Need verification"]}))
        self.assertTrue(is_captcha_required("_____tmd_____/punish"))
        self.assertFalse(is_captcha_required({"ret": ["SUCCESS::ok"]}))
```

- [ ] **Step 2: Запустити тест і переконатися, що він падає**

Run: `python -m unittest tests.test_farmer.TestAuth.test_is_captcha_required_detection`
Expected: FAIL

- [ ] **Step 3: Реалізувати функцію `is_captcha_required` та інтегрувати у статус чекіну**

У `src/checkin.py`:
1. Додати функцію `is_captcha_required(res)`.
2. У методі `_call_mtop` та `run_checkin`: якщо виявлено капчу, виставляти `self.status = "CAPTCHA_REQUIRED"`.
3. У `src/notifications.py`: додати іконку `🧩 Капча (потрібен клік у браузері)` для статусу `CAPTCHA_REQUIRED`.

- [ ] **Step 4: Запустити тести**

Run: `python -m unittest discover -s tests -p "test_*.py"`
Expected: PASS

- [ ] **Step 5: Зафіксувати зміни**

```bash
git add src/checkin.py src/notifications.py tests/test_farmer.py
git commit -m "feat: detect Alibaba WAF captcha challenges and report actionable alert"
```

---

## План виконання (Execution Handoff)

План повністю сформовано та збережено у [docs/superpowers/plans/2026-10-16-audit-fixes-and-optimizations.md](file:///d:/Розробка/Аліекспрес/docs/superpowers/plans/2026-10-16-audit-fixes-and-optimizations.md).
