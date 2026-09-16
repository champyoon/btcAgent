#!/usr/bin/env bash
# BTC DCA 어시스턴트 실행 스크립트.
# 사전 준비: 저장소 루트에 .env (AWS_ACCESS_KEY_ID 등)
set -euo pipefail

cd "$(dirname "$0")"
pip install -r requirements.txt
python app.py
