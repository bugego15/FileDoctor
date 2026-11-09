<#
.SYNOPSIS
    创建 NTFS 测试 VHD（需要管理员），写入样本文件并删除一半，用于验证 NTFS 删除文件恢复。

.DESCRIPTION
    1. 用 diskpart 创建动态 VHD（默认 512MB）并挂载为 V:；
    2. 快速格式化为 NTFS；
    3. 写入样本（txt UTF-8/GBK、zip；若装了 python-docx 还会生成 docx）；
    4. 删除一半文件（模拟误删，另一半保留作对照）；
    5. 可选 -Scan：调用 filetool.py ntfs 扫描/恢复（需要已安装 pytsk3）；
    6. 默认结束时卸载 VHD（-KeepMounted 可保持挂载）。

    注意：
    - 创建/挂载 VHD、直接读取 \\.\V: 都需要管理员权限；
    - VHD 是虚拟磁盘文件，TSK 不能直接读 VHD，请扫描挂载后的 \\.\V:（本脚本 -Scan 就是这么做的），
      或者挂载后用 dd/WinHex 把卷导出成 raw 镜像再扫描；
    - 恢复结果默认写到 $env:TEMP\fd_ntfs_recovered。

.EXAMPLE
    # 管理员 PowerShell：
    powershell -ExecutionPolicy Bypass -File tools\make_ntfs_test_vhd.ps1 -Scan
    powershell -ExecutionPolicy Bypass -File tools\make_ntfs_test_vhd.ps1 -Path D:\ntfs_test.vhdx -SizeMB 1024 -KeepMounted
#>
param(
    [string]$Path = (Join-Path $env:TEMP 'fd_ntfs_test.vhdx'),
    [int]$SizeMB = 512,
    [string]$Letter = 'V',
    [string]$SamplesDir = '',
    [string]$OutDir = (Join-Path $env:TEMP 'fd_ntfs_recovered'),
    [string]$PythonPath = 'python',
    [switch]$Scan,
    [switch]$KeepMounted
)

$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error '需要管理员权限：创建/挂载 VHD、读取 \\.\V: 都必须提权运行。'
    exit 1
}
if (Test-Path $Path) {
    Write-Error "目标已存在：$Path（请换路径或先删除）"
    exit 1
}
if ($Letter -match '^[A-Za-z]$') { $Letter = $Letter.ToUpper() } else { Write-Error "盘符不合法：$Letter"; exit 1 }
$volume = "${Letter}:"

function Invoke-DiskPart([string]$commands, [string]$tag) {
    $tmp = Join-Path $env:TEMP ("fd_diskpart_{0}_{1}.txt" -f $tag, [guid]::NewGuid().ToString('N'))
    Set-Content -LiteralPath $tmp -Value $commands -Encoding ASCII
    try { & diskpart /s $tmp | Out-String | Write-Host } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- 1. 创建并挂载 VHD
Write-Host "==> 创建 VHD：$Path（$SizeMB MB）"
Invoke-DiskPart @"
create vdisk file="$Path" maximum=$SizeMB type=expandable
select vdisk file="$Path"
attach vdisk
create partition primary
format fs=ntfs quick label=FDTEST
assign letter=$Letter
exit
"@ 'create'

# ---------------------------------------------------------------- 2. 写入样本
Write-Host "==> 写入样本到 ${volume}\recovery_demo"
$sample = Join-Path $volume 'recovery_demo'
New-Item -ItemType Directory -Force -Path $sample | Out-Null

if ($SamplesDir) {
    if (-not (Test-Path $SamplesDir)) { Write-Error "SamplesDir 不存在：$SamplesDir"; exit 1 }
    Copy-Item (Join-Path $SamplesDir '*') $sample -Recurse -Force
    Write-Host "    已从 $SamplesDir 复制样本"
} else {
    Set-Content -LiteralPath (Join-Path $sample 'hello_utf8.txt') -Value '文件恢复测试 UTF-8 hello' -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $sample 'hello_gbk.txt') -Value '文件恢复测试 GBK hello' -Encoding Default
    Compress-Archive -LiteralPath (Join-Path $sample 'hello_utf8.txt') -DestinationPath (Join-Path $sample 'sample.zip') -Force
    & $PythonPath -c "import sys; import docx; d=docx.Document(); d.add_heading('NTFS 测试文档',1); d.add_paragraph('用于验证 NTFS 删除恢复的 docx 正文 abc 123'); d.save(sys.argv[1])" (Join-Path $sample 'deleted.docx') 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host '    （没有 python-docx，跳过 docx 样本）' }
    Get-ChildItem $sample -File | ForEach-Object { "    样本: {0} ({1} B)" -f $_.Name, $_.Length }
}

# ---------------------------------------------------------------- 3. 删除一半文件（模拟误删）
$files = @(Get-ChildItem $sample -File)
if ($files.Count -eq 0) { Write-Warning '样本目录里没有文件，无法演示删除' }
$drop = $files | Sort-Object Name | Select-Object -First ([math]::Ceiling($files.Count / 2))
foreach ($f in $drop) {
    Remove-Item -LiteralPath $f.FullName -Force
    Write-Host "    已删除: $($f.Name)（$($f.Length) B）"
}
Write-Host "    保留: $(($files | Where-Object { $_.FullName -notin $drop.FullName }).Name -join ', ')"

# ---------------------------------------------------------------- 4. 可选扫描
if ($Scan) {
    Write-Host "==> 用 filetool.py ntfs 扫描 $volume"
    & $PythonPath (Join-Path $PSScriptRoot '..\filetool.py') ntfs "\\\\.\\${Letter}:" --list
    Write-Host "==> 恢复删除文件到 $OutDir"
    & $PythonPath (Join-Path $PSScriptRoot '..\filetool.py') ntfs "\\\\.\\${Letter}:" -o $OutDir --all
}

# ---------------------------------------------------------------- 5. 卸载
if (-not $KeepMounted) {
    Write-Host "==> 卸载 VHD"
    Invoke-DiskPart @"
select vdisk file="$Path"
detach vdisk
exit
"@ 'detach'
} else {
    Write-Host "==> VHD 保持挂载：$volume（VHD 文件：$Path）"
}

Write-Host "完成。"
Write-Host "提示：也可以把 $volume 用 dd/WinHex 导出为 raw 镜像（如 $Path.raw），再用：python filetool.py ntfs $Path.raw --list"
