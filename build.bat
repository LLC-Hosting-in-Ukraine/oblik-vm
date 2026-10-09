@echo off
rem Збірка OblikVM.exe у папку dist\
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pytest -q || (echo Тести не пройшли, збірку зупинено. & exit /b 1)
".venv\Scripts\pyinstaller.exe" oblik.spec --noconfirm --clean || exit /b 1
echo.
echo Готово: dist\OblikVM.exe
if exist test_base\ (
    copy /y dist\OblikVM.exe test_base\OblikVM.exe >nul && echo Скопійовано в test_base\ ^(oblik.db не змінювалась^) || echo УВАГА: не вдалося скопіювати в test_base\ - закрийте програму, запущену звідти.
)
