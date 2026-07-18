@echo off
setlocal enabledelayedexpansion

echo 🐳 Building optimized freesky Docker container...

REM Build arguments with defaults
set PORT=%PORT%
if "%PORT%"=="" set PORT=3000

set BACKEND_PORT=%BACKEND_PORT%
if "%BACKEND_PORT%"=="" set BACKEND_PORT=8005

set API_URL=%API_URL%
if "%API_URL%"=="" set API_URL=http://0.0.0.0:%PORT%

set DADDYLIVE_URI=%DADDYLIVE_URI%
if "%DADDYLIVE_URI%"=="" set DADDYLIVE_URI=https://dlhd.st

set PROXY_CONTENT=%PROXY_CONTENT%
if "%PROXY_CONTENT%"=="" set PROXY_CONTENT=TRUE

set SOCKS5=%SOCKS5%
if "%SOCKS5%"=="" set SOCKS5=

echo 📦 Building with arguments:
echo   PORT: %PORT%
echo   BACKEND_PORT: %BACKEND_PORT%
echo   API_URL: %API_URL%
echo   DADDYLIVE_URI: %DADDYLIVE_URI%
echo   PROXY_CONTENT: %PROXY_CONTENT%
echo   SOCKS5: %SOCKS5%

REM Build with BuildKit for better performance
set DOCKER_BUILDKIT=1
docker build ^
    --file Dockerfile.optimized ^
    --tag freesky:optimized ^
    --build-arg PORT=%PORT% ^
    --build-arg BACKEND_PORT=%BACKEND_PORT% ^
    --build-arg API_URL=%API_URL% ^
    --build-arg DADDYLIVE_URI=%DADDYLIVE_URI% ^
    --build-arg PROXY_CONTENT=%PROXY_CONTENT% ^
    --build-arg SOCKS5=%SOCKS5% ^
    --progress=plain ^
    .

if %ERRORLEVEL% neq 0 (
    echo ❌ Build failed!
    exit /b 1
)

echo 📊 Build completed! Image size:
docker images freesky:optimized --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"

echo 🔍 Layer information:
docker history freesky:optimized --format "table {{.CreatedBy}}\t{{.Size}}"

echo ✅ Optimized container build completed successfully!
echo 🚀 To run the container:
echo    docker run -p %PORT%:%PORT% -p %BACKEND_PORT%:%BACKEND_PORT% freesky:optimized 