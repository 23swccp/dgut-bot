# 使用 PyInstaller onedir + Velopack 构建 Windows 安装器与更新包。
# 前置要求：Python、Node.js、.NET 8 SDK。
param(
    [switch]$SkipNpmInstall,
    [switch]$SkipVelopack
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

foreach ($name in @("build", "dist", "Releases")) {
    $target = Join-Path $ProjectRoot $name
    if (Test-Path -LiteralPath $target) {
        $resolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $resolved.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝清理工作区外路径：$resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

Push-Location (Join-Path $ProjectRoot "frontend")
try {
    if (-not $SkipNpmInstall) { npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed" } }
    npm test -- --run; if ($LASTEXITCODE -ne 0) { throw "frontend tests failed" }
    npm run build; if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
} finally { Pop-Location }

python -m pip install -r requirements.txt "pyinstaller==6.22.2"
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "python tests failed" }
python -m PyInstaller packaging/dgut-bot.spec --noconfirm --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$smokeData = Join-Path $env:TEMP "dgut-bot-smoke-data"
$env:YXY_SMOKE_DATA_DIR = $smokeData
python scripts/smoke_test.py "dist/dgut-bot"
if ($LASTEXITCODE -ne 0) { throw "packaged smoke test failed" }

if ($SkipVelopack) {
    Write-Host "PyInstaller output: dist/dgut-bot (Velopack packaging skipped)"
    exit 0
}
dotnet --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw ".NET 8 SDK is required for Velopack" }
dotnet tool update --global vpk --version 1.2.0
if ($LASTEXITCODE -ne 0) { dotnet tool install --global vpk --version 1.2.0 }
$version = (python -c "from version import APP_VERSION; print(APP_VERSION)").Trim()
vpk pack --packId DgutBot --packVersion $version --packDir dist/dgut-bot --mainExe dgut-bot.exe --packTitle "莞工小皮卡" --packAuthors "23swccp" --icon assets/dgut-bot.ico --runtime win-x64 --noPortable --outputDir Releases
if ($LASTEXITCODE -ne 0) { throw "Velopack packaging failed" }
Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "Releases") -File | Select-Object Name, Length
