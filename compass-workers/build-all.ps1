$ErrorActionPreference = "Stop"
$env:DOCKER_BUILDKIT = "1"

Write-Host "─── Building compass-worker:base ───" -ForegroundColor Cyan
docker build -f compass-workers/Dockerfile -t compass-worker:base .

Write-Host "─── Building compass-worker:performance ───" -ForegroundColor Cyan
docker build -f compass-workers/Dockerfile.performance -t compass-worker:performance .

Write-Host "─── Building compass-worker:cost ───" -ForegroundColor Cyan
docker build -f compass-workers/Dockerfile.cost -t compass-worker:cost .

Write-Host "─── Building compass-worker:safety ───" -ForegroundColor Cyan
docker build --secret id=hf_token,env=HF_TOKEN `
    -f compass-workers/Dockerfile.safety `
    -t compass-worker:safety .

Write-Host "─── Building compass-worker:quality ───" -ForegroundColor Cyan
# Default Quality models are public on HF; the secret mount is included so a
# private-model swap doesn't require touching this script.
docker build --secret id=hf_token,env=HF_TOKEN `
    -f compass-workers/Dockerfile.quality `
    -t compass-worker:quality .

Write-Host "`n─── All images built ───" -ForegroundColor Green
docker images "compass-worker*" --format "table {{.Repository}}:{{.Tag}}`t{{.Size}}`t{{.CreatedSince}}"