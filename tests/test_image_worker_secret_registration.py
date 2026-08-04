from pathlib import Path


def test_image_worker_sealed_secret_is_registered_in_prod_kustomization():
    content = Path("secrets/prod/kustomization.yaml").read_text()
    assert "image-worker-credentials.sealedsecret.yaml" in content
