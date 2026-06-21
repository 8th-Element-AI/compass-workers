$ErrorActionPreference = "Stop"
$env:DOCKER_BUILDKIT = "1"

Write-Host "─── Building signal-worker:base ───" -ForegroundColor Cyan
docker build -f signal-workers/Dockerfile -t signal-worker:base .

Write-Host "─── Building signal-worker:performance ───" -ForegroundColor Cyan
docker build -f signal-workers/Dockerfile.performance -t signal-worker:performance .

Write-Host "─── Building signal-worker:cost ───" -ForegroundColor Cyan
docker build -f signal-workers/Dockerfile.cost -t signal-worker:cost .

Write-Host "─── Building signal-worker:safety ───" -ForegroundColor Cyan
docker build --secret id=hf_token,env=HF_TOKEN `
    -f signal-workers/Dockerfile.safety `
    -t signal-worker:safety .

Write-Host "─── Building signal-worker:quality ───" -ForegroundColor Cyan
# Default Quality models are public on HF; the secret mount is included so a
# private-model swap doesn't require touching this script.
docker build --secret id=hf_token,env=HF_TOKEN `
    -f signal-workers/Dockerfile.quality `
    -t signal-worker:quality .

Write-Host "`n─── All images built ───" -ForegroundColor Green
docker images "signal-worker*" --format "table {{.Repository}}:{{.Tag}}`t{{.Size}}`t{{.CreatedSince}}"