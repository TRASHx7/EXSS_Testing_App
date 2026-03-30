@echo off
setlocal

echo =========================================================
echo  EXSS Device Test Recorder - Build Script
echo =========================================================
echo.

:: Activate venv if present
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: Install / upgrade dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet
echo.

:: Build with PyInstaller
echo Building executable...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "EXSS_Test" ^
    main.py

echo.
if exist "dist\EXSS_Test.exe" (
    echo Build successful!
    echo.
    echo IMPORTANT: Copy  db_config.b64  into the same folder as the .exe
    echo            before distributing or running the application.
    echo.
    echo Output: dist\EXSS_Test_Recorder.exe
) else (
    echo Build FAILED. Check the output above for errors.
)

pause
endlocal
