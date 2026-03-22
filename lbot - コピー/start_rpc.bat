@echo off
chcp 65001 >nul
echo =========================================
echo Electrum-LTC RPC Server 起動プログラム
echo =========================================

echo 1. デーモン（バックグラウンド通信機能）を起動しています...
start "" "C:\Program Files (x86)\Electrum-LTC\electrum-ltc.exe" daemon

echo 数秒待機します...
timeout /t 5 >nul

echo 2. ウォレットを読み込んでいます...
"C:\Program Files (x86)\Electrum-LTC\electrum-ltc.exe" daemon load_wallet -w "C:\Users\さくたろう\AppData\Roaming\Electrum-LTC\wallets\default_wallet"

echo.
echo =========================================
echo 起動処理が完了しました。
echo この黒い画面は開いたままにするか、最小化しておいてください。
echo （閉じるとBotが通信できなくなります）
echo =========================================
pause