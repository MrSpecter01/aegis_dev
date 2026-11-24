# Helm Charts Directory

This directory contains target application Helm charts (e.g., Google Online Boutique) parsed by Aegis during topology initialization:
`aegis init --helm-dir ./charts/online-boutique --namespace boutique`

When running in Docker Compose, this directory is bind-mounted into `/app/charts` in the `aegis-api` container.

## Online Boutique Chart (`charts/online-boutique`)
Derived from the official [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) repository:
- `Chart.yaml`: Helm chart metadata (`name: onlineboutique`, `version: 0.10.7`)
- `values.yaml`: Default values and image configurations
- `templates/`: Manifest templates for all 11 microservices (`adservice`, `cartservice`, `checkoutservice`, `currencyservice`, `emailservice`, `frontend`, `paymentservice`, `productcatalogservice`, `recommendationservice`, `redis-cart`, `shippingservice`).

### Deploying to a Test Cluster (`kind` / `minikube`)
```bash
helm install boutique ./charts/online-boutique -n boutique --create-namespace
```

