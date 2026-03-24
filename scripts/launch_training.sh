#!/usr/bin/env bash
# scripts/launch_training.sh
# Bootstrap script for running train.py on an AWS EC2 GPU instance.
#
# Usage (run this ON the EC2 instance after SSH-ing in):
#   chmod +x scripts/launch_training.sh
#   ./scripts/launch_training.sh
#
# Recommended instance: g4dn.xlarge (16GB VRAM, ~$0.53/hr)
# AMI: Deep Learning OSS Nvidia Driver AMI GPU PyTorch (us-east-1)

set -euo pipefail

REPO_URL="https://github.com/babureddynangi/credit.git"
REPO_DIR="credit"
DATASET_FILE="aws_synthetic_dataset.jsonl"
OUTPUT_DIR="outputs"
LOG_FILE="training.log"

echo "========================================"
echo "  AWS EC2 Training Bootstrap"
echo "========================================"

# ── 1. System deps ────────────────────────────────────────────────────────────
echo "[1/6] Updating system packages..."
sudo apt-get update -q
sudo apt-get install -y -q git python3-pip

# ── 2. Clone repo ─────────────────────────────────────────────────────────────
echo "[2/6] Cloning repository..."
if [ -d "$REPO_DIR" ]; then
    echo "  Repo already exists, pulling latest..."
    cd "$REPO_DIR" && git pull && cd ..
else
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# ── 3. Install Python dependencies ────────────────────────────────────────────
echo "[3/6] Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q \
    "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git" \
    trl \
    transformers \
    datasets \
    torch \
    accelerate \
    bitsandbytes \
    xformers

# ── 4. Generate dataset ───────────────────────────────────────────────────────
echo "[4/6] Generating synthetic dataset..."
if [ ! -f "$DATASET_FILE" ]; then
    python scripts/generate_synthetic_dataset.py
else
    echo "  Dataset already exists, skipping generation."
fi

# ── 5. Verify GPU ─────────────────────────────────────────────────────────────
echo "[5/6] Checking GPU availability..."
python -c "import torch; print(f'  CUDA available: {torch.cuda.is_available()}'); print(f'  GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"None\"}')"

# ── 6. Run training ───────────────────────────────────────────────────────────
echo "[6/6] Starting training (logging to $LOG_FILE)..."
mkdir -p "$OUTPUT_DIR"
python scripts/train.py 2>&1 | tee "$LOG_FILE"

echo ""
echo "========================================"
echo "  Training complete!"
echo "  Outputs: $OUTPUT_DIR/"
echo "  Log:     $LOG_FILE"
echo "========================================"
