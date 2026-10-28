@echo off
setlocal
chcp 65001 >nul
echo ==============================================
echo    Оновлення кукі AliExpress (Danagar42/Ali)
echo ==============================================
echo.
echo Скопіюйте кукі: Cookie-Editor - Export as Header String.
echo.
echo [1] Оновити перший акаунт - Account_Primary  (секрет ALI_COOKIES)
echo [2] Оновити другий акаунт - Account_1  (секрет ALI_COOKIES_1)
echo.
choice /c 12 /n /m "Оберіть номер акаунта (1 або 2): "
if errorlevel 3 exit /b 1
if errorlevel 2 (
    set "SECRET_NAME=ALI_COOKIES_1"
) else if errorlevel 1 (
    set "SECRET_NAME=ALI_COOKIES"
) else (
    exit /b 1
)

echo.
echo Зчитування буфера та збереження в секрет %SECRET_NAME%...
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; try { Get-Command gh -ErrorAction Stop > $null; $cookies = (Get-Clipboard -Raw -ErrorAction Stop); if ([string]::IsNullOrWhiteSpace($cookies)) { throw 'Clipboard is empty.' }; $cookies = $cookies -replace '[\uFEFF\uFFFE\u200B-\u200F\u2060\u00A0]', ''; $cookies = $cookies.Trim() -replace '(?i)^cookie:\s*', ''; if ($cookies -notmatch '^[^=;\s]+=[^;\r\n]*(;\s*[^=;\s]+=[^;\r\n]*)*;?$' -or $cookies -notmatch '(?i)(^|;\s*)(aep_usuc_f|xman_us_f|_m_h5_tk|xman_t|login_aliyunid|cna|ali_apache_tracktmp|acs_usuc_t)=') { throw 'Expected AliExpress cookies: Cookie-Editor - Export as Header String.' }; $OutputEncoding = [System.Text.UTF8Encoding]::new($false); $cookies | gh secret set $env:SECRET_NAME --repo Danagar42/Ali; exit $LASTEXITCODE } catch { [Console]::Error.WriteLine('Update failed. Check clipboard format, GitHub CLI installation and gh auth login.'); exit 1 }"
if errorlevel 1 (
    echo.
    echo Помилка. Секрет не підтверджено як оновлений.
    pause
    exit /b 1
)

echo.
echo Успішно! Секрет %SECRET_NAME% оновлено.
echo Дійсність сесії буде перевірена під час запуску бота.
echo.
pause
exit /b 0
