param(
    [string]$ClusterName = "aegis-demo"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Setting up Kind Cluster: $ClusterName" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Check if kind cluster already exists
$existing = kind.exe get clusters
if ($existing -contains $ClusterName) {
    Write-Host "[1/5] Kind cluster '$ClusterName' already exists." -ForegroundColor Yellow
} else {
    Write-Host "[1/5] Creating Kind cluster '$ClusterName'..." -ForegroundColor Yellow
    kind.exe create cluster --name $ClusterName --config D:\k8s-lab\kind\kind-config.yaml
}

# 2. Switch context
kubectl.exe config use-context "kind-$ClusterName"
Write-Host "[2/5] Context switched to kind-$ClusterName." -ForegroundColor Green

# 3. Create ServiceAccount & Export Internal Kubeconfig for Aegis Docker Containers
Write-Host "[3/5] Configuring Aegis ServiceAccount and internal kubeconfig..." -ForegroundColor Yellow
kubectl.exe create serviceaccount aegis-admin -n default --dry-run=client -o yaml | kubectl.exe apply -f -
kubectl.exe create clusterrolebinding aegis-admin --clusterrole=cluster-admin --serviceaccount=default:aegis-admin --dry-run=client -o yaml | kubectl.exe apply -f -

$Token = (kubectl.exe create token aegis-admin -n default --duration=87600h).Trim()

$KubeconfigPath = Join-Path $ProjectRoot "deploy\kubeconfig"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") -c @"
import subprocess, yaml
raw = subprocess.check_output('kind.exe get kubeconfig --name $ClusterName --internal', shell=True)
cfg = yaml.safe_load(raw)
cfg['users'][0]['user']['token'] = '$Token'
with open(r'$KubeconfigPath', 'w', encoding='utf-8') as f:
    yaml.safe_dump(cfg, f)
"@
Write-Host "  -> Internal kubeconfig generated at $KubeconfigPath" -ForegroundColor Green

# 4. Install Chaos Mesh
Write-Host "[4/5] Installing Chaos Mesh on Kind with containerd socket..." -ForegroundColor Yellow
helm.exe repo add chaos-mesh https://charts.chaos-mesh.org 2>$null
helm.exe repo update chaos-mesh
helm.exe upgrade --install chaos-mesh chaos-mesh/chaos-mesh `
    -n chaos-testing `
    --create-namespace `
    --set chaosDaemon.runtime=containerd `
    --set chaosDaemon.socketPath=/run/containerd/containerd.sock

Write-Host "  -> Waiting for Chaos Mesh controller manager..." -ForegroundColor DarkGray
kubectl.exe wait --for=condition=available deployment/chaos-controller-manager -n chaos-testing --timeout=120s

# 5. Connect Docker Network
Write-Host "[5/5] Bridging Aegis containers to Kind cluster network..." -ForegroundColor Yellow
$aegisContainers = @("aegis-mcp", "aegis-api", "aegis-prometheus")
foreach ($container in $aegisContainers) {
    try {
        docker.exe network connect kind $container 2>$null
        Write-Host "  -> Connected $container to kind network" -ForegroundColor DarkGray
    } catch {
        # Already connected or container not started yet
    }
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "[SUCCESS] Kind cluster '$ClusterName' is fully configured and ready!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
