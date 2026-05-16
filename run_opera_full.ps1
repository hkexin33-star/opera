# 全库批处理脚本（阶段二：整个 opera_dataset）
# 说明：请在《戏考》试点 01000000 效果满意后再执行本脚本。
# 完整文档见 README.md「数据处理两阶段策略」。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
        $k, $v = $_ -split '=', 2
        $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
        if ($k -and -not [Environment]::GetEnvironmentVariable($k)) {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
    Write-Host "[ENV] loaded from .env" -ForegroundColor Green
}

if (-not $env:MINERU_TOKEN) {
    Write-Host "缺少 MINERU_TOKEN（见 .env.example）" -ForegroundColor Yellow
    exit 1
}

# 整个赛题 PDF 根目录（递归所有合集子文件夹）
$InputDir  = Join-Path $Root "opera_dataset"
$OutputDir = Join-Path $Root "opera_output"
$ManifestDir = Join-Path $OutputDir "manifests"
New-Item -ItemType Directory -Force -Path $ManifestDir | Out-Null

Write-Host "`n=== 全库进度（所有合集）===" -ForegroundColor Cyan
python mineru_batch_convert_structured_llm_final_v8.py `
    --status-only `
    --input-dir $InputDir `
    --output-dir $OutputDir

# 取消注释以执行全库批处理（勿加 --collection-prefix，汇总写入 opera_output/all_*.csv）
# python mineru_batch_convert_structured_llm_final_v8.py `
#     --input-dir $InputDir `
#     --output-dir $OutputDir `
#     --llm-enabled `
#     --chunk-size 50 `
#     --manifest (Join-Path $ManifestDir "mineru_manifest_full.csv") `
#     2>&1 | Tee-Object -FilePath (Join-Path $OutputDir "logs\batch_full_run.log") -Append

# 全库汇总（所有 structured.json → opera_output/all_*.csv）
# python mineru_batch_convert_structured_llm_final_v8.py `
#     --combine-only `
#     --output-dir $OutputDir

Write-Host "`n阶段二脚本就绪。默认仅查看进度；取消注释后执行全库处理。" -ForegroundColor Green
