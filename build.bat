@echo off
echo === WoT Mod Installer Build Script ===
echo.

echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Building executable...
pyinstaller --onefile --windowed --name "WoT_Mod_Installer" --clean mod_installer.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo === Build complete! ===
echo Executable: dist\WoT_Mod_Installer.exe
pause
