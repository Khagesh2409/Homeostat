#!/bin/bash
# ──────────────────────────────────────────────────────────────
# deploy-observability.sh
# Deploys the full observability stack onto the K3s cluster
# Run this from your LOCAL machine (needs kubectl + helm configured)
#
# Usage:
#   bash scripts/deploy-observability.sh
# ──────────────────────────────────────────────────────────────

set -euo pipefail

NODE_IP="${NODE_IP:-52.70.127.81}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-~/.kube/homeostat-config}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Homeostat — Observability Stack Deployment"
echo "  Node: $NODE_IP"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Step 1: Download kubeconfig from node ─────────────────────
echo "→ Downloading kubeconfig from node..."
mkdir -p ~/.kube
ssh -i ~/.ssh/homeostat-key.pem \
    -o StrictHostKeyChecking=no \
    ubuntu@"$NODE_IP" \
    "sudo cat /etc/rancher/k3s/k3s.yaml" \
  | sed "s/127.0.0.1/$NODE_IP/g" \
  > "$KUBECONFIG_PATH"
chmod 600 "$KUBECONFIG_PATH"
export KUBECONFIG="$KUBECONFIG_PATH"
echo "  ✓ Kubeconfig saved to $KUBECONFIG_PATH"

# ── Step 2: Verify cluster is reachable ──────────────────────
echo "→ Verifying cluster..."
kubectl get nodes
echo ""

# ── Step 3: Create namespaces ─────────────────────────────────
echo "→ Creating namespaces..."
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace homeostat --dry-run=client -o yaml | kubectl apply -f -

# ── Step 4: Add Helm repos ────────────────────────────────────
echo "→ Adding Helm repos..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# ── Step 5: Deploy kube-prometheus-stack ─────────────────────
echo "→ Deploying Prometheus + Alertmanager..."
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values k8s/base/prometheus/helm-values.yml \
  --wait \
  --timeout 5m
echo "  ✓ Prometheus stack deployed"

# ── Step 6: Apply custom manifests (alert rules + event watcher)
echo "→ Applying Kustomize manifests..."
kubectl apply -k k8s/overlays/prod/
echo "  ✓ Custom manifests applied"

# ── Step 7: Wait for pods to be ready ────────────────────────
echo "→ Waiting for Prometheus to be ready..."
kubectl rollout status -n monitoring statefulset/prometheus-monitoring-kube-prometheus-prometheus --timeout=120s
echo "→ Waiting for Alertmanager to be ready..."
kubectl rollout status -n monitoring statefulset/alertmanager-monitoring-kube-prometheus-alertmanager --timeout=60s
echo "→ Waiting for event watcher to be ready..."
kubectl rollout status -n homeostat deployment/event-watcher --timeout=60s

# ── Step 8: Print access info ─────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ Observability stack deployed!"
echo ""
echo "  Port-forward to access UIs locally:"
echo "  Prometheus:   kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090"
echo "  Alertmanager: kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-alertmanager 9093:9093"
echo ""
echo "  Verify alert rules loaded:"
echo "  kubectl get prometheusrule -n monitoring"
echo ""
echo "  Check event watcher logs:"
echo "  kubectl logs -n homeostat -l app=event-watcher -f"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
