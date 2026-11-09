<#
.SYNOPSIS
    用 PyInstaller 把 FileDoctor GUI 打包成 Windows exe（dist\FileDoctor\FileDoctor.exe）。

.DESCRIPTION
    前置：已安装依赖（PySide6、pytsk3、pyinstaller）
        python -m pip install PySide6 pytsk3 pyinstaller

    产物是 onedir 模式（dist\FileDoctor\ 目录，含 Qt DLL），不要只拷 exe 单文件。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
    powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1 -Python "C:\Python312\python.exe"
#>
param(
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    Write-Host "==> 检查依赖"
    & $Python -c "import PySide6, pytsk3, PyInstaller; print('PySide6', PySide6.__version__); print('pytsk3 ok'); print('PyInstaller', PyInstaller.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "依赖缺失，请先运行：$Python -m pip install PySide6 pytsk3 pyinstaller" }

    Write-Host "==> 开始打包（首次约 1~3 分钟）"
    & $Python -m PyInstaller --noconfirm --clean --windowed --name FileDoctor `
        --collect-all pytsk3 --collect-submodules fdlib gui.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

    $exe = Join-Path $root 'dist\FileDoctor\FileDoctor.exe'
    if (-not (Test-Path $exe)) { throw "没有找到产物：$exe" }
    Write-Host "==> 打包完成：$exe"
    Write-Host "==> 自检（无界面）："
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:FILEDOCTOR_SELFTEST_OUT = Join-Path $env:TEMP 'filedoctor_exe_selftest.json'
    & $exe --self-test
    $code = $LASTEXITCODE
    if (Test-Path $env:FILEDOCTOR_SELFTEST_OUT) {
        Write-Host (Get-Content $env:FILEDOCTOR_SELFTEST_OUT -Raw)
    }
    if ($code -ne 0) { throw "exe 自检失败（exit $code）" }
    Write-Host "==> exe 自检通过"
}
finally {
    Pop-Location
}
