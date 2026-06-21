<#
.SYNOPSIS
    Scale the Quality worker StatefulSet, with automatic slot rebalancing.

.DESCRIPTION
    Atomically patches `spec.replicas` AND the WORKER_PARTITION_COUNT env var
    in one operation. Watches the rollout to completion, then verifies that
    every expected (lens, partition_key) row materializes in worker_checkpoints.

    Slot checkpoints survive pod-count changes — each slot keeps its checkpoint
    in PG independent of who currently owns it. A scale change just reassigns
    ownership; the checkpoints resume from where they were.

    Quality semantic scoring is CPU-heavy (NLI + embedding + relevance models),
    so partitioned scaling matters more here than for Performance/Cost. For
    mechanical-only mode (SIGNAL_QUALITY_SEMANTIC=0), replicas=1 is enough.

.PARAMETER Replicas
    Target replica count. Must be 1 ≤ N ≤ 16 (the partition_id space).
    Power-of-2 values (1, 2, 4, 8, 16) give even slot distribution.

.PARAMETER Namespace
    K8s namespace. Default: signal.

.EXAMPLE
    .\scale-quality.ps1 -Replicas 2
    Scales quality to 2 pods (each owns 8 slots).

.EXAMPLE
    .\scale-quality.ps1 -Replicas 4
    Scales quality to 4 pods (each owns 4 slots).
#>
param(
    [Parameter(Mandatory=$true)]
    [ValidateRange(1, 16)]
    [int]$Replicas,

    [string]$Namespace = "signal",
    [string]$StatefulSet = "signal-worker-quality",
    [int]$TotalSlots = 16,
    [int]$VerifyTimeoutSec = 180   # bigger than safety — quality batches are slower to checkpoint
)

$ErrorActionPreference = "Stop"

# ───────── 1. Sanity warnings ─────────
$evenDivisors = @(1, 2, 4, 8, 16)
if ($Replicas -notin $evenDivisors) {
    Write-Warning ("Replicas=$Replicas doesn't evenly divide $TotalSlots slots. " +
                   "First ($($TotalSlots % $Replicas)) pods will each own one extra slot. " +
                   "Consider 1, 2, 4, 8, or 16 for even distribution.")
}

# ───────── 2. Compute expected partition_keys (for verification) ─────────
function Get-ExpectedSlotKeys {
    param([int]$PodCount, [int]$Slots)
    if ($PodCount -eq 1) { return @("default") }

    $base = [int]($Slots / $PodCount)
    $extra = $Slots - ($base * $PodCount)
    $keys = @()
    for ($i = 0; $i -lt $PodCount; $i++) {
        $size = if ($i -lt $extra) { $base + 1 } else { $base }
        for ($j = 0; $j -lt $size; $j++) {
            # Pod i's slot indices match what signal_worker.partition.compute_slots produces
            if ($i -lt $extra) {
                $slot = $i * ($base + 1) + $j
            } else {
                $slot = $extra * ($base + 1) + ($i - $extra) * $base + $j
            }
            $keys += "slot:$slot"
        }
    }
    return $keys | Sort-Object -Unique
}

$expectedKeys = Get-ExpectedSlotKeys -PodCount $Replicas -Slots $TotalSlots
Write-Host "Target: replicas=$Replicas, expected partition_keys count=$($expectedKeys.Count)" -ForegroundColor Cyan
Write-Host "  $($expectedKeys -join ', ')"

# ───────── 3. Patch the StatefulSet atomically ─────────
Write-Host "`nPatching $StatefulSet → replicas=$Replicas, WORKER_PARTITION_COUNT=$Replicas..." -ForegroundColor Cyan

# JSON patch that updates both replicas and the env var in one API call.
# Note: /env/1 is the WORKER_PARTITION_COUNT entry — POD_NAME is at /env/0.
# If you re-order env in the StatefulSet, update this path.
$patch = @"
[
  { "op": "replace", "path": "/spec/replicas", "value": $Replicas },
  { "op": "replace",
    "path":  "/spec/template/spec/containers/0/env/1/value",
    "value": "$Replicas" }
]
"@

kubectl patch statefulset $StatefulSet -n $Namespace --type=json -p $patch | Out-Host

# ───────── 4. Wait for rollout ─────────
Write-Host "`nWaiting for rollout..." -ForegroundColor Cyan
kubectl rollout status statefulset $StatefulSet -n $Namespace --timeout=10m

# ───────── 5. Verify all pods are Ready ─────────
Write-Host "`nVerifying $Replicas pods are Ready..." -ForegroundColor Cyan
$pods = kubectl get pods -n $Namespace -l "app=signal-worker,lens=quality" `
    -o jsonpath="{.items[*].metadata.name}" --no-headers
$podArr = $pods -split " "
Write-Host "  Pods: $($podArr -join ', ')"

if ($podArr.Count -ne $Replicas) {
    Write-Warning "Expected $Replicas pods, found $($podArr.Count). Rollout may be incomplete."
}

# ───────── 6. Verify expected checkpoint rows appear in PG ─────────
Write-Host "`nVerifying slot checkpoints in worker_checkpoints..." -ForegroundColor Cyan

$deadline = (Get-Date).AddSeconds($VerifyTimeoutSec)
$pgPodName = kubectl get pods -n $Namespace -l "app=postgres" `
    -o jsonpath="{.items[0].metadata.name}" 2>$null
if (-not $pgPodName) {
    $pgPodName = "signal-postgres-0"
}

$missing = $expectedKeys
while ((Get-Date) -lt $deadline -and $missing.Count -gt 0) {
    $actual = kubectl exec -n $Namespace $pgPodName -- `
        psql -U signal -d signal -t -A `
        -c "SELECT partition_key FROM worker_checkpoints WHERE lens='quality' ORDER BY partition_key" 2>$null

    if ($actual) {
        $actualKeys = $actual -split "`n" | Where-Object { $_ } | ForEach-Object { $_.Trim() }
        $missing = @($expectedKeys | Where-Object { $_ -notin $actualKeys })
        if ($missing.Count -eq 0) { break }
    }
    Write-Host "  Still missing $($missing.Count) row(s); polling..."
    Start-Sleep 5
}

# ───────── 7. Report ─────────
if ($missing.Count -eq 0) {
    Write-Host "`n✅ Scaled to $Replicas pods. All $($expectedKeys.Count) slot checkpoints present in PG." `
        -ForegroundColor Green
    kubectl get pods -n $Namespace -l "app=signal-worker,lens=quality"
    exit 0
} else {
    Write-Warning "`nSome expected slots missing after ${VerifyTimeoutSec}s. Missing: $($missing -join ', ')"
    Write-Warning "Likely cause: those slots haven't received any spans yet, OR semantic scoring is"
    Write-Warning "still grinding through the first batch (NLI on CPU is slow — first checkpoint can"
    Write-Warning "take 5-15 min). Check ``kubectl logs`` on the relevant pods; rebalance itself is complete."
    exit 1
}